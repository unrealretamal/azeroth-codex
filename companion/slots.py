"""Load-on-demand addon slots for checked native-text replies.

Each slot is an existing, single-use addon.  The companion replaces its data
file before WoW loads that addon, avoiding font-metric decoding entirely.
"""
from pathlib import Path
import time
import zlib

from .limits import ADDON_SLOT_COUNT
from .visual import validate_addon

SLOT_COUNT = ADDON_SLOT_COUNT
SLOT_PREFIX = 'CodexPixelBridgeSlot'
MAX_TEXT = 60000
STATES = {'waiting', 'queued', 'working', 'streaming', 'done', 'failed', 'interrupted'}


def slot_name(slot):
    if not 1 <= slot <= SLOT_COUNT:
        raise ValueError('Addon slot exhausted')
    return f'{SLOT_PREFIX}{slot:03d}'


def slot_directory(addon, slot):
    addon = validate_addon(addon)
    return addon.parent / slot_name(slot)


def inbox_path(addon, slot):
    path = slot_directory(addon, slot) / 'Inbox.lua'
    if path.resolve().parent != slot_directory(addon, slot).resolve():
        raise ValueError('Unexpected addon slot path')
    return path


def _lua_string(value):
    """Encode exact UTF-8 bytes as a non-executable Lua 5.1 string literal."""
    raw = value.encode('utf-8')
    literal = []
    for byte in raw:
        if 32 <= byte <= 126 and byte not in (34, 92):
            literal.append(chr(byte))
        else:
            literal.append(f'\\{byte:03d}')
    return '"' + ''.join(literal) + '"'


def _reply(snapshot, control):
    key = f'{control.session}:{control.request}'
    if snapshot.get('id') != key:
        state, text = 'waiting', 'Waiting for the companion to receive your prompt.'
    else:
        state = snapshot.get('state') if snapshot.get('state') in STATES else 'waiting'
        text = snapshot.get('reply') or {
            'waiting': 'Waiting for the companion to receive your prompt.',
            'queued': 'Your prompt is queued.',
            'working': 'Codex is working.',
            'streaming': 'Codex is writing.',
        }.get(state, 'Completed without response text.')
    text = str(text).encode('utf-8')[:MAX_TEXT].decode('utf-8', errors='ignore')
    encoded = text.encode('utf-8')
    revision = zlib.adler32(state.encode('ascii') + b'\0' + encoded)
    return state, text, revision


def make_inbox(snapshot, control):
    """Return a data-only Lua chunk; reply text is never executable Lua."""
    state, text, revision = _reply(snapshot, control)
    return (
        'CodexPixelBridgeSlotData = {\n'
        '  version = 1,\n'
        f'  session = {_lua_string(control.session)},\n'
        f'  request = {int(control.request)},\n'
        f'  slot = {int(control.slot)},\n'
        f'  state = {_lua_string(state)},\n'
        f'  revision = {int(revision)},\n'
        f'  text = {_lua_string(text)},\n'
        '}\n'
    ).encode('utf-8')


def prepare_slots(addon, count=SLOT_COUNT):
    """Create missing load-on-demand slot addons without replacing used data."""
    addon = validate_addon(addon)
    if type(count) is not int or not 1 <= count <= SLOT_COUNT:
        raise ValueError('Invalid addon slot count')
    interface = '16001'
    for line in (addon / 'CodexPixelBridge.toc').read_text(encoding='utf-8').splitlines():
        if line.startswith('## Interface:'):
            interface = line.split(':', 1)[1].strip()
            break
    created = kept = 0
    for slot in range(1, count + 1):
        name = slot_name(slot)
        directory = addon.parent / name
        directory.mkdir(exist_ok=True)
        toc = directory / f'{name}.toc'
        inbox = directory / 'Inbox.lua'
        if not toc.exists():
            toc.write_text(
                f'## Interface: {interface}\n'
                f'## Title: Azeroth Codex reply slot {slot:03d}\n'
                '## Notes: Load-on-demand reply data; leave enabled.\n'
                '## LoadOnDemand: 1\n'
                '## Dependencies: CodexPixelBridge\n\n'
                'Inbox.lua\n', encoding='utf-8')
            created += 1
        else:
            kept += 1
        if not inbox.exists():
            inbox.write_text('CodexPixelBridgeSlotData = nil\n', encoding='utf-8')
            created += 1
        else:
            kept += 1
    return {'slots': count, 'created': created, 'preserved': kept}


class SlotBridge:
    """Publish one frozen data chunk into the slot WoW advertised."""
    def __init__(self, addon, clock=time.monotonic):
        self.addon = validate_addon(addon)
        self.clock = clock
        for slot in (1, SLOT_COUNT):
            if not inbox_path(self.addon, slot).is_file():
                raise ValueError('Install load-on-demand reply slots before starting the companion')
        self.session = None
        self.slot = 0
        self.request = 0
        self.deadline = 0
        self.was_active = False
        self.written = None
        self.pending_slot = 0
        self.pending_bytes = None

    def accept(self, control, snapshot):
        if control.kind != 'slot':
            raise ValueError('Expected addon-slot control')
        if control.session != self.session:
            self.session = control.session
            self.slot = 0
            self.request = 0
            self.was_active = False
            self.written = None
            self.pending_slot = 0
            self.pending_bytes = None
        if control.slot < self.slot:
            return False
        if control.slot == self.slot and self.request and control.request != self.request:
            raise ValueError('Addon slot cannot be reused for a different request')
        now = self.clock()
        deadline = now + control.remaining_ms / 1000 - .75
        if control.slot != self.slot or (control.active and not self.was_active):
            self.deadline = deadline
        else:
            self.deadline = min(self.deadline, deadline)
        self.slot = control.slot
        self.request = control.request
        self.was_active = control.active
        if not control.active or control.remaining_ms < 1000 or now >= self.deadline:
            return False
        destination = inbox_path(self.addon, control.slot)
        if self.pending_slot != control.slot:
            self.pending_slot = control.slot
            self.pending_bytes = make_inbox(snapshot, control)
        identity = (control.session, control.slot, self.pending_bytes)
        if identity == self.written:
            return False
        temporary = destination.with_suffix('.next.lua')
        temporary.write_bytes(self.pending_bytes)
        if self.clock() >= self.deadline:
            temporary.unlink(missing_ok=True)
            return False
        temporary.replace(destination)
        self.written = identity
        return True
