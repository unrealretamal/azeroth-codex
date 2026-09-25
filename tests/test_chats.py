import argparse
from pathlib import Path
import tempfile
import unittest

from companion.chats import ChatStore


class ChatStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.defaults = argparse.Namespace(project=self.project, backend='codex', sandbox='read-only')
        self.store = ChatStore(self.root / 'inbox.sqlite3')

    def tearDown(self):
        self.temp.cleanup()

    def test_profiles_keep_independent_execution_settings_and_threads(self):
        default = self.store.resolve('default', self.defaults)
        self.assertEqual((default.project, default.backend, default.sandbox, default.codex_session),
                         (self.project, 'codex', 'read-only', None))
        self.store.set_session('default', '12345678-aaaa-bbbb-cccc-123456789abc')
        other = self.root / 'other'; other.mkdir()
        raids = self.store.configure('raids', other, 'mock', 'workspace-write')
        self.assertEqual((raids.project, raids.backend, raids.sandbox, raids.codex_session),
                         (other, 'mock', 'workspace-write', None))
        self.assertEqual(self.store.resolve('default', self.defaults).codex_session,
                         '12345678-aaaa-bbbb-cccc-123456789abc')

    def test_changed_execution_settings_reset_only_that_chat_thread(self):
        self.store.resolve('raids', self.defaults)
        self.store.set_session('raids', '12345678-aaaa-bbbb-cccc-123456789abc')
        changed = self.store.configure('raids', self.project, 'codex', 'workspace-write')
        self.assertIsNone(changed.codex_session)
        with self.assertRaises(ValueError):
            self.store.resolve('not valid!', self.defaults)
        with self.assertRaises(ValueError):
            self.store.set_session('raids', '../not-a-thread')


if __name__ == '__main__':
    unittest.main()
