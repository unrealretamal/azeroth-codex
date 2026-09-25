import argparse
import json
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import patch

from companion.app import App, Inbox, unpack_request
from companion.chats import ChatStore
from companion.scheduler import ChatScheduler
from companion.slots import make_inbox
from companion.visual import encode_control, parse_control
from tests import test_bridge

ROOT = Path(__file__).resolve().parents[1]


class ParallelChatTests(unittest.TestCase):
    def test_distinct_chats_overlap_and_followup_resolves_fresh_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            app=App.__new__(App)
            app.args=argparse.Namespace(state=root,project=root,backend='codex',sandbox='read-only')
            inbox=Inbox(root/'inbox.sqlite3');app.chats=ChatStore(root/'inbox.sqlite3');app.events=queue.Queue()
            start_a,start_b,release=threading.Event(),threading.Event(),threading.Event()
            calls=[]
            def agent(args,prompt,on_update,on_session,session_id):
                current=prompt if session_id else json.loads(prompt.split('\n\n',1)[1])['latest_user_message']
                calls.append((current,session_id))
                if current=='a1':
                    start_a.set();self.assertTrue(start_b.wait(3));self.assertTrue(release.wait(3));on_session('thread-a')
                elif current=='b1':
                    start_b.set();self.assertTrue(start_a.wait(3));release.set();on_session('thread-b')
                return 'done','reply '+current
            for n,chat,prompt in [(1,'a','a1'),(2,'a','a2'),(3,'b','b1')]:
                inbox.add('1234567890abcdef:'+str(n),prompt,chat)
            with patch('companion.app.run_agent',side_effect=agent):
                scheduler=ChatScheduler(app.process_job,parallel=2)
                try:
                    for n,chat,prompt in [(1,'a','a1'),(2,'a','a2'),(3,'b','b1')]:
                        scheduler.put_nowait(('1234567890abcdef:'+str(n),prompt,chat,'send'))
                    scheduler.join()
                finally:
                    release.set();scheduler.shutdown()
            self.assertIn(('a2','thread-a'),calls)
            self.assertLess(calls.index(('b1',None)),calls.index(('a2','thread-a')))
            self.assertEqual(inbox.db.execute("SELECT count(*) FROM jobs WHERE state='done'").fetchone()[0],3)
            inbox.db.close()

    def test_settings_are_ordered_with_turns_and_not_agent_prompts(self):
        self.assertEqual(unpack_request('\x1eac2\x1fraids\x1ffolder\x1fC:\\Work Space'),
                         ('raids','folder','C:\\Work Space'))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);app=App.__new__(App)
            app.args=argparse.Namespace(state=root,project=root,backend='mock',sandbox='read-only')
            inbox=Inbox(root/'inbox.sqlite3');app.chats=ChatStore(root/'inbox.sqlite3');app.events=queue.Queue()
            inbox.add('1234567890abcdef:1','Raid planning','raids','rename')
            with patch('companion.app.run_agent') as agent:
                app.process_job(('1234567890abcdef:1','Raid planning','raids','rename'))
                agent.assert_not_called()
            chats,receipts=app.chats.snapshot('1234567890abcdef')
            self.assertEqual(chats[0]['title'],'Raid planning')
            self.assertEqual(chats[0]['messages'],[])
            self.assertEqual(receipts[0]['state'],'done')
            inbox.db.close()


class InGameChatTests(unittest.TestCase):
    def setUp(self):
        fixture=test_bridge.LuaTests()
        fixture.setUp();fixture.test_addon_load_and_submit_with_ui_stubs()
        self.lua,self.ns=fixture.lua,fixture.ns
        for name in ('Slot.lua','ReplyLinks.lua','Chats.lua'):
            self.lua.execute((ROOT/'addon/CodexPixelBridge'/name).read_bytes(),b'CodexPixelBridge',self.ns)
        self.lua.globals()[b'NS']=self.ns
        self.lua.execute(b'''
        CodexPixelBridgeState={nextAddonSlotV1=201,nextFontSlotV2=168}
        for _,w in ipairs(widgets) do
          if w.scripts.OnEvent then w.scripts.OnEvent(w,'ADDON_LOADED','CodexPixelBridge') end
        end
        ''')
        self.session=bytes.fromhex(self.ns[b'SessionHex'].decode())

    def control(self):
        return parse_control(self.ns[b'VisualControl'](self.session))

    def submit(self,chat,text):
        self.ns[b'SetActiveChat'](chat.encode())
        self.ns[b'PromptEditBox'][b'SetText'](self.ns[b'PromptEditBox'],text.encode())
        self.ns[b'PromptEditBox'][b'scripts'][b'OnEnterPressed']()
        return self.session.hex()+':'+str(self.control().request)

    def bundle(self,ids,states):
        chats=[];receipts=[]
        for chat,key,state in zip(('raids','code'),ids,states):
            chats.append(dict(id=chat,title=chat,project='C:\\work',backend='mock',sandbox='read-only',
                              messages=[dict(id=key,prompt='prompt '+chat,state=state,text='reply '+chat)]))
            receipts.append(dict(id=key,state=state,text=''))
        chunk=make_inbox(dict(id=ids[-1],state=states[-1],reply='reply code',chats=chats,receipts=receipts),self.control())
        self.lua.execute(chunk)
        return self.lua.globals()[b'CodexPixelBridgeSlotData'][b'bundle']

    def test_hidden_chat_reply_and_drafts_survive_switching(self):
        ids=[self.submit('raids','raid question'),self.submit('code','code question')]
        self.assertTrue(self.ns[b'ReceiveChats'](self.bundle(ids,('working','done'))))
        self.assertIn(b'reply code',self.ns[b'LastNativeReply'])
        self.assertNotIn(b'reply raids',self.ns[b'LastNativeReply'])
        self.lua.execute(b"NS.PromptEditBox:SetText('unfinished code draft')")
        self.ns[b'SetActiveChat'](b'raids')
        self.assertIn(b'reply raids',self.ns[b'LastNativeReply'])
        self.ns[b'SetActiveChat'](b'code')
        self.assertEqual(self.ns[b'PromptEditBox'][b'GetText'](self.ns[b'PromptEditBox']),b'unfinished code draft')
        self.assertFalse(self.ns[b'ReceiveChats'](self.bundle(ids,('done','done'))))
        chats,_=self.ns[b'ChatState']()
        self.assertTrue(chats[b'raids'][b'unread'])

    def test_corrupt_bundle_never_overwrites_chat_and_slots_reset_only_ui_bank(self):
        self.assertEqual(self.control().slot,1)
        self.assertEqual(self.lua.globals()[b'CodexPixelBridgeState'][b'nextFontSlotV2'],168)
        ids=[self.submit('raids','raid question'),self.submit('code','code question')]
        bundle=self.bundle(ids,('done','done'))
        bundle[b'rows'][2][6]=b'tampered'
        self.assertIsNone(self.ns[b'ReceiveChats'](bundle))
        self.assertNotIn(b'tampered',self.ns[b'LastNativeReply'])

    def test_clean_game_state_recovers_transcripts_from_bridge(self):
        ids=[self.session.hex()+':1',self.session.hex()+':2']
        self.assertFalse(self.ns[b'ReceiveChats'](self.bundle(ids,('done','done'))))
        self.ns[b'SetActiveChat'](b'raids')
        self.assertIn(b'prompt raids',self.ns[b'LastNativeReply'])
        self.assertIn(b'reply raids',self.ns[b'LastNativeReply'])


if __name__=='__main__': unittest.main()
