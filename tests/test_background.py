import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from companion.background import InstanceLock, install_startup, request


class BackgroundTests(unittest.TestCase):
    def test_single_instance_lock_releases_and_does_not_use_stale_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            first,second=InstanceLock(directory),InstanceLock(directory)
            try:
                self.assertTrue(first.acquire());self.assertFalse(second.acquire())
                first.close();self.assertTrue(second.acquire())
            finally:
                first.close();second.close()

    def test_control_commands_are_bounded_and_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            request(directory,'show')
            value=json.loads((Path(directory)/'bridge-control.json').read_text())
            self.assertEqual(value['action'],'show')
            with self.assertRaises(ValueError): request(directory,'run prompt')
            self.assertEqual(list(Path(directory).glob('*.tmp')),[])

    @unittest.skipUnless(os.name=='nt','Windows login startup')
    def test_startup_is_hidden_quoted_and_removable(self):
        with tempfile.TemporaryDirectory() as directory:
            destination=Path(directory)/'Azeroth Codex.vbs'
            install_startup(destination=destination)
            text=destination.read_text(encoding='utf-16')
            self.assertIn('pythonw.exe',text);self.assertIn('--background',text)
            self.assertIn(', 0, False',text)
            install_startup(False,destination)
            self.assertFalse(destination.exists())

    def test_real_hidden_process_reuses_instance_without_interrupting_inbox(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            state=Path(directory)
            (state/'launcher.json').write_text(json.dumps(dict(project=directory,backend='mock',sandbox='read-only')))
            argv=[sys.executable,'-m','companion.app','--state',directory,'--saved-config','--background']
            process=subprocess.Popen(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                deadline=time.monotonic()+10
                while not (state/'bridge-status.json').exists():
                    if process.poll() is not None:
                        self.fail(process.communicate()[1].decode(errors='replace'))
                    self.assertLess(time.monotonic(),deadline)
                    time.sleep(.05)
                status=json.loads((state/'bridge-status.json').read_text())
                self.assertFalse(status['visible']);self.assertFalse(status['capture'])
                connection=sqlite3.connect(state/'inbox.sqlite3')
                try:
                    connection.execute("INSERT INTO jobs(id,prompt,state,reply) VALUES ('1234567890abcdef:1','pending','working','')")
                    connection.commit()
                    duplicate=subprocess.run(argv,capture_output=True,timeout=10)
                    self.assertEqual(duplicate.returncode,0,duplicate.stderr)
                    self.assertEqual(connection.execute('SELECT state FROM jobs').fetchone()[0],'working')
                finally:
                    connection.close()
                request(state,'stop')
                out,err=process.communicate(timeout=10)
                self.assertEqual(process.returncode,0,err)
            finally:
                if process.poll() is None: process.kill()
                process.communicate(timeout=5)


if __name__=='__main__': unittest.main()
