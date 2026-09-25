"""Single-instance background bridge and optional Windows login startup."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

BASE = Path(__file__).resolve().parents[1]


class InstanceLock:
    """OS-owned locks release on crashes; no stale PID files."""
    def __init__(self, *identities):
        self.identities, self.handles = identities, []

    def acquire(self):
        for identity in sorted(set(str(Path(p).resolve()).casefold() for p in self.identities)):
            digest = hashlib.sha256(identity.encode()).hexdigest()
            if os.name == 'nt':
                api = ctypes.WinDLL('kernel32', use_last_error=True)
                api.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
                api.CreateMutexW.restype = ctypes.c_void_p
                handle = api.CreateMutexW(None, False, 'Local\\AzerothCodex-' + digest)
                if not handle:
                    self.close();raise ctypes.WinError(ctypes.get_last_error())
                exists = ctypes.get_last_error() == 183
                self.handles.append(handle)
                if exists:
                    self.close();return False
            else:
                import fcntl
                import tempfile
                handle = open(Path(tempfile.gettempdir())/('azeroth-' + digest + '.lock'), 'a+b')
                self.handles.append(handle)
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    self.close();return False
        return True

    def close(self):
        for handle in self.handles:
            if os.name == 'nt':
                api = ctypes.WinDLL('kernel32', use_last_error=True)
                api.CloseHandle.argtypes = (ctypes.c_void_p,)
                api.CloseHandle(handle)
            else:
                handle.close()
        self.handles.clear()


def atomic_json(path, value):
    path = Path(path);path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value), encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def request(state, action):
    if action not in ('show', 'stop'):
        raise ValueError('Invalid bridge control')
    atomic_json(Path(state)/'bridge-control.json', dict(action=action, time=time.time()))


def startup_path():
    if os.name != 'nt' or not os.environ.get('APPDATA'):
        raise OSError('Automatic login startup requires Windows')
    return Path(os.environ['APPDATA'])/'Microsoft/Windows/Start Menu/Programs/Startup/Azeroth Codex.vbs'


def install_startup(enable=True, destination=None):
    path = Path(destination) if destination else startup_path()
    if not enable:
        path.unlink(missing_ok=True);return path
    python = Path(sys.executable).with_name('pythonw.exe')
    if not python.is_file():
        raise ValueError('Use the project Python environment with pythonw.exe')
    command = subprocess.list2cmdline([str(python), str(BASE/'Launch Companion.pyw'), '--background'])
    body = ('Set shell = CreateObject("WScript.Shell")\r\n'
            'shell.Run "' + command.replace('"', '""') + '", 0, False\r\n')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding='utf-16')
    return path


def supervise(state):
    state = Path(state).resolve();state.mkdir(parents=True, exist_ok=True)
    lock = InstanceLock(state/'supervisor')
    if not lock.acquire(): return
    try:
        while True:
            log = state/'background.log'
            if log.exists() and log.stat().st_size > 1_000_000:
                log.replace(state/'background.previous.log')
            with log.open('ab') as output:
                code = subprocess.call([sys.executable, '-m', 'companion.app', '--state', str(state),
                                        '--saved-config', '--background', '--start-capture'],
                                       cwd=BASE, stdout=output, stderr=output,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if code == 0: return
            time.sleep(5)
    finally:
        lock.close()


def start(state):
    python = Path(sys.executable).with_name('pythonw.exe')
    if not python.is_file(): python = Path(sys.executable)
    return subprocess.Popen([str(python), '-m', 'companion.background', '--state', str(Path(state).resolve())],
                            cwd=BASE, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=BASE/'state')
    supervise(parser.parse_args().state)
