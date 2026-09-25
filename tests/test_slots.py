from pathlib import Path
import tempfile
import unittest
import zlib

from lupa.lua51 import LuaRuntime

from companion.slots import SLOT_COUNT, SlotBridge, inbox_path, make_inbox, prepare_slots, slot_name
from companion.visual import encode_control, parse_control


class AddonSlotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addon = Path(self.temp.name) / 'CodexPixelBridge'
        self.addon.mkdir()
        (self.addon / 'CodexPixelBridge.toc').write_text('## Interface: 16001\n')
        prepare_slots(self.addon)
        self.now = 100.
        self.bridge = SlotBridge(self.addon, clock=lambda: self.now)

    def tearDown(self):
        self.temp.cleanup()

    def control(self, **kwargs):
        return parse_control(encode_control(kind='slot', **kwargs))

    def test_slots_are_precreated_load_on_demand_addons(self):
        self.assertEqual(slot_name(1), 'CodexPixelBridgeSlot001')
        self.assertEqual(slot_name(SLOT_COUNT), 'CodexPixelBridgeSlot200')
        for slot in (1, SLOT_COUNT):
            directory = inbox_path(self.addon, slot).parent
            self.assertTrue((directory / f'{slot_name(slot)}.toc').is_file())
            self.assertEqual(inbox_path(self.addon, slot).read_text(), 'CodexPixelBridgeSlotData = nil\n')

    def test_slot_data_is_frozen_checked_and_uses_plain_data_only(self):
        control = self.control()
        snapshot = {'id': '3132333435363738:1', 'state': 'done', 'reply': '\nCafé ]=] text'}
        self.assertTrue(self.bridge.accept(control, snapshot))
        path = inbox_path(self.addon, 1)
        saved = path.read_text(encoding='utf-8')
        self.assertIn('CodexPixelBridgeSlotData = {', saved)
        self.assertIn('session = "3132333435363738"', saved)
        self.assertIn('\\010Caf\\195\\169 ]=] text', saved)
        self.assertFalse(self.bridge.accept(control, dict(snapshot, reply='replacement')))
        self.assertEqual(path.read_text(encoding='utf-8'), saved)
        self.now += 6
        self.assertTrue(self.bridge.accept(self.control(slot=2), snapshot))

    def test_lua_literal_roundtrips_exact_bytes_and_checked_revision(self):
        snapshot = {'id': '3132333435363738:1', 'state': 'done', 'reply': '\nCafé\0 ]=]'}
        lua = LuaRuntime(encoding=None)
        lua.execute(make_inbox(snapshot, self.control()))
        data = lua.globals()[b'CodexPixelBridgeSlotData']
        self.assertEqual(data[b'text'], b'\nCaf\xc3\xa9\0 ]=]')
        self.assertEqual(data[b'revision'], zlib.adler32(b'done\0\nCaf\xc3\xa9\0 ]=]'))

    def test_unknown_request_becomes_non_executable_waiting_status(self):
        chunk = make_inbox({}, self.control())
        self.assertIn(b'state = "waiting"', chunk)
        self.assertIn(b'Waiting for the companion', chunk)

    def test_reused_slot_cannot_replace_data_for_another_request(self):
        self.assertTrue(self.bridge.accept(self.control(request=1), {'id': '3132333435363738:1', 'state': 'done', 'reply': 'one'}))
        with self.assertRaises(ValueError):
            self.bridge.accept(self.control(request=2), {'id': '3132333435363738:2', 'state': 'done', 'reply': 'two'})


if __name__ == '__main__':
    unittest.main()
