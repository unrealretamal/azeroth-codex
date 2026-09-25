"""Desktop capture + durable inbox + local agent adapter. Never sends input to WoW."""
import argparse
import copy
import ctypes
import json
import re
from pathlib import Path
import queue
import sqlite3
import subprocess
import threading
import time
import tkinter as tk
from tkinter import scrolledtext, filedialog, simpledialog
from PIL import ImageGrab
from .protocol import Assembler, decode_image
from .notifications import ReplyBanner
from .visual import VisualBridge, parse_control
from .agent_stream import command_for, run_stream
from .chats import ChatStore
from .native import NativeBridge
from .slots import SlotBridge
from .conversation import ConversationContext
from .calibration import find_strip

def save_preferences(state, **changes):
    path = state/'launcher.json'
    try:
        settings = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        settings = {}
    settings.update(changes)
    state.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2), encoding='utf-8')

class Inbox:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=5)
        self.db.execute('PRAGMA busy_timeout=5000')
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, prompt TEXT, state TEXT, reply TEXT, chat TEXT NOT NULL DEFAULT "default")')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(jobs)')}
        if 'chat' not in columns:
            self.db.execute('ALTER TABLE jobs ADD COLUMN chat TEXT NOT NULL DEFAULT "default"')
        # A crash may have happened after the agent ran. Never automatically replay it.
        self.db.execute("UPDATE jobs SET state='interrupted' WHERE state IN ('queued','working','streaming')")
        self.db.commit()

    def add(self, key, prompt, chat='default'):
        with self.db:
            result = self.db.execute('INSERT OR IGNORE INTO jobs (id,prompt,state,reply,chat) VALUES (?, ?, ?, ?, ?)',
                                      (key, prompt, 'queued', '', chat))
        return result.rowcount == 1

    def update(self, key, state, reply=''):
        with self.db:
            self.db.execute('UPDATE jobs SET state=?,reply=? WHERE id=?', (state,reply,key))


CHAT_PREFIX = re.compile(r'^\x1echat=([a-z0-9][a-z0-9_-]{0,31})\x1f', re.I)


def unpack_chat(prompt):
    """Strip trusted addon metadata; ordinary user text remains verbatim."""
    match = CHAT_PREFIX.match(prompt)
    if not match:
        return 'default', prompt
    return match.group(1).lower(), prompt[match.end():]

def run_agent(args, prompt, on_update=None, on_session=None, session_id=None):
    if args.backend == 'mock':
        return 'done', 'Mock agent received: ' + prompt
    if on_update is not None:
        return run_stream(args, prompt, on_update, on_session, session_id)
    command = command_for(args, session_id)
    # Prompt is stdin, never interpolated into a shell command. Each job is independent.
    try:
        result = subprocess.run(command, input=prompt, text=True, encoding='utf-8',
                                errors='replace', capture_output=True, timeout=args.timeout,
                                shell=False, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except subprocess.TimeoutExpired:
        return 'failed', 'Agent timed out. Check its workspace before submitting again.'
    except OSError as exc:
        return 'failed', str(exc)
    replies, failures, completed = [], [], False
    for line in result.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get('type') == 'turn.completed':
            completed = True
        if event.get('type') in ('error','turn.failed'):
            failures.append(str(event.get('message') or event.get('error') or event))
        item = event.get('item') or {}
        if isinstance(item,dict) and event.get('type') == 'item.completed' and item.get('type') == 'agent_message':
            replies.append(str(item.get('text','')))
    if result.returncode or failures or not completed:
        return 'failed', '\n'.join(failures+replies) or result.stderr[-4000:] or 'No completion event'
    return 'done', '\n'.join(replies) or '(Agent completed without a text response)'

class App:
    def __init__(self, root, args):
        self.root, self.args = root, args
        args.state.mkdir(parents=True, exist_ok=True)
        self.inbox = Inbox(args.state/'inbox.sqlite3')
        self.chats = ChatStore(args.state/'inbox.sqlite3')
        self.assembler = Assembler()
        self.jobs, self.events = queue.Queue(maxsize=8), queue.Queue()
        self.active, self.closed = False, False
        self.valid_frames = 0
        self.return_retry_at = 0
        self.strip_visible = None
        self.banner=ReplyBanner(root,self.open_reply)
        root.title('Azeroth Codex — companion replies')
        self.job_status = tk.StringVar(value='Ready for a prompt')
        tk.Label(root,textvariable=self.job_status,font=('Segoe UI',14,'bold')).pack(anchor='w',padx=12,pady=(10,0))
        self.status = tk.StringVar(value='Capture paused.')
        tk.Label(root,textvariable=self.status).pack(anchor='w',padx=12,pady=8)
        self.project_label=tk.StringVar()
        tk.Label(root,textvariable=self.project_label,wraplength=720,justify='left').pack(anchor='w',padx=12)
        self.project_button=tk.Button(root,text='Change work folder…',command=self.choose_project)
        self.project_button.pack(anchor='w',padx=12,pady=4)
        self.chat_button=tk.Button(root,text='Configure chat…',command=self.configure_chat)
        self.chat_button.pack(anchor='w',padx=12,pady=(0,4))
        self.reset_chat_button=tk.Button(root,text='Reset chat Codex session…',command=self.reset_chat_session)
        self.reset_chat_button.pack(anchor='w',padx=12,pady=(0,4))
        self.project_hint=tk.StringVar()
        tk.Label(root,textvariable=self.project_hint,fg='#a34200').pack(anchor='w',padx=12)
        self.refresh_project_label()
        try:
            preferences=json.loads((args.state/'launcher.json').read_text(encoding='utf-8'))
        except (OSError,ValueError):
            preferences={}
        self.notify_enabled=tk.BooleanVar(value=preferences.get('notifications',True))
        notification_controls=tk.Frame(root)
        notification_controls.pack(anchor='w',padx=12,pady=4)
        tk.Checkbutton(notification_controls,text='Reply banner and sound',variable=self.notify_enabled,
                       command=self.save_notification_preference).pack(side='left')
        tk.Button(notification_controls,text='Test notification',command=lambda:self.notify('done','Test notification — reply alerts are ready.')).pack(side='left',padx=8)
        tk.Label(root,text='Exact strip crop in desktop pixels: left, top, width, height').pack(anchor='w',padx=12)
        self.region=tk.StringVar(value=','.join(map(str,args.region)))
        tk.Entry(root,textvariable=self.region,width=48).pack(anchor='w',padx=12)
        tk.Button(root,text='Auto-detect strip',command=self.auto_calibrate).pack(anchor='w',padx=12,pady=(4,0))
        tk.Button(root,text='Start / pause capture',command=self.toggle).pack(anchor='w',padx=12,pady=8)
        self.log=scrolledtext.ScrolledText(root,width=94,height=25,state='disabled',wrap='word')
        self.log.pack(fill='both',expand=True,padx=12,pady=8)
        self.visual = None
        self.native = None
        self.slots = None
        addon = getattr(args, 'addon', None) or preferences.get('addon')
        if addon:
            if (Path(addon)/'fontreply0001.ttf').is_file():
                try:
                    self.native = NativeBridge(addon)
                    self.write('Native in-game text return enabled using checked font packets.\n')
                except (OSError,ValueError) as exc:
                    self.write('Native text return unavailable: '+str(exc))
            try:
                self.slots = SlotBridge(addon)
                self.write('Load-on-demand addon-slot return enabled.\n')
            except (OSError, ValueError) as exc:
                self.write('Addon-slot return unavailable: ' + str(exc))
            try:
                self.visual = VisualBridge(addon, budget_path=args.state/'visual-budget.json')
                self.write('In-game image preview enabled. This uses a finite bank of first-use assets.\n')
            except (OSError, ValueError) as exc:
                self.write('Reply preview unavailable: ' + str(exc))
        for key,prompt,state,reply,chat in self.inbox.db.execute('SELECT id,prompt,state,reply,chat FROM jobs ORDER BY rowid DESC LIMIT 20').fetchall()[::-1]:
            self.write(f'[{state}] {key}  chat={chat}\nYou: {prompt}\n{reply}\n')
            self.show_job_status(state,key)
        self.write('Only the selected strip is captured. Captures are not saved. Prompts/replies are stored locally.\n')
        self.write('Chats persist by /codex chat <name>; Codex threads resume per configured chat.\n')
        threading.Thread(target=self.worker,daemon=True).start()
        root.protocol('WM_DELETE_WINDOW',self.close)
        if getattr(args, 'start_capture', False):
            self.toggle()
        if getattr(args, 'minimized', False):
            root.after(100, root.iconify)
        root.after(70,self.tick)

    def write(self,text):
        self.log.configure(state='normal'); self.log.insert('end',text+'\n')
        self.log.see('end'); self.log.configure(state='disabled')

    def open_reply(self):
        self.root.deiconify()
        self.root.lift()

    def save_notification_preference(self):
        save_preferences(self.args.state,notifications=self.notify_enabled.get())
        if not self.notify_enabled.get():
            self.banner.dismiss()

    def notify(self,state,reply):
        if self.notify_enabled.get():
            self.root.bell()
            self.banner.show(state,reply)

    def refresh_project_label(self):
        self.project_label.set(f'Default chat folder: {self.args.project}  |  {self.args.sandbox}')
        try:
            empty=not any(self.args.project.iterdir())
            self.project_hint.set('This folder is empty. Choose the folder containing your work.' if empty else '')
        except OSError:
            self.project_hint.set('This work folder is unavailable. Choose another folder.')

    def set_project(self, directory):
        if self.jobs.unfinished_tasks:
            self.write('Finish the queued/running requests before changing work folders.')
            return False
        project=Path(directory).resolve()
        if not project.is_dir():
            self.write('That work folder is unavailable.')
            return False
        save_preferences(self.args.state, project=str(project))
        self.args.project=project
        self.chats.configure('default', project, self.args.backend, self.args.sandbox)
        self.refresh_project_label()
        self.write(f'Work folder changed to {project}. Previous prompts will not be replayed.')
        return True

    def choose_project(self):
        if self.jobs.unfinished_tasks:
            return
        was_active=self.active
        self.active=False
        try:
            directory=filedialog.askdirectory(parent=self.root,title='Choose the folder containing your work',initialdir=str(self.args.project))
            if directory:
                self.set_project(directory)
        finally:
            self.active=was_active

    def configure_chat(self):
        """Set durable project/agent/sandbox for a named `/codex chat` channel."""
        if self.jobs.unfinished_tasks:
            self.write('Finish queued/running requests before changing chat settings.')
            return
        names = ', '.join(profile.id for profile in self.chats.profiles()) or 'default'
        chat = simpledialog.askstring('Configure chat', 'Chat name (known: ' + names + '):',
                                      initialvalue='default', parent=self.root)
        if chat is None:
            return
        try:
            profile = self.chats.resolve(chat, self.args)
        except ValueError as exc:
            self.write(str(exc))
            return
        was_active = self.active
        self.active = False
        try:
            directory = filedialog.askdirectory(parent=self.root, title='Choose work folder for ' + profile.id,
                                                 initialdir=str(profile.project))
        finally:
            self.active = was_active
        if not directory:
            return
        backend = simpledialog.askstring('Configure chat', 'Agent backend: mock or codex',
                                         initialvalue=profile.backend, parent=self.root)
        sandbox = simpledialog.askstring('Configure chat', 'Sandbox: read-only or workspace-write',
                                         initialvalue=profile.sandbox, parent=self.root)
        if backend is None or sandbox is None:
            return
        try:
            configured = self.chats.configure(profile.id, directory, backend.lower(), sandbox.lower())
        except ValueError as exc:
            self.write(str(exc))
            return
        self.write(f'Chat {configured.id} configured: {configured.project} | '
                   f'{configured.backend} | {configured.sandbox}. Changed settings start a new Codex thread.')

    def reset_chat_session(self):
        if self.jobs.unfinished_tasks:
            self.write('Finish queued/running requests before resetting a chat session.')
            return
        chat = simpledialog.askstring('Reset Codex session', 'Chat name:', initialvalue='default', parent=self.root)
        if chat is None:
            return
        try:
            self.chats.resolve(chat, self.args)
            self.chats.clear_session(chat)
        except ValueError as exc:
            self.write(str(exc))
            return
        self.write('Codex session reset for chat ' + chat.lower() + '. Its next prompt starts a new thread.')

    def show_job_status(self,state,key):
        labels={'queued':'Queued','working':'Working','streaming':'Writing','done':'Done — reply below',
                'failed':'Failed — details below','interrupted':'Interrupted — review before retrying'}
        label=labels.get(state,state)
        self.job_status.set(f'{label}  (prompt {key.rsplit(":",1)[-1]})')
        self.root.title(f'Azeroth Codex — {label}')

    def toggle(self):
        try:
            x,y,w,h = map(int,self.region.get().split(','))
            if w<128 or h<4 or w>4096 or h>512: raise ValueError()
            self.bbox=(x,y,x+w,y+h)
        except ValueError:
            self.status.set('Invalid crop. Enter left,top,width,height.'); return
        self.active=not self.active
        if self.active:
            (self.args.state/'capture.json').write_text(
                json.dumps({'region':[x,y,w,h]}), encoding='utf-8')
        save_preferences(self.args.state, start_capture=self.active)
        self.status.set('Capturing; waiting for valid strip' if self.active else 'Capture paused; queued jobs may still run')

    def auto_calibrate(self):
        if self.active:
            self.status.set('Pause capture before auto-detecting the strip.')
            return
        try:
            region = find_strip(ImageGrab.grab(all_screens=True))
        except (OSError, ValueError) as exc:
            self.status.set('Auto-detect failed: ' + str(exc))
            return
        self.region.set(','.join(map(str, region)))
        self.status.set('Found checked strip at ' + ','.join(map(str, region)) + '. Start capture to save it.')

    def worker(self):
        context=ConversationContext(self.args.state/'inbox.sqlite3')
        while True:
            key,prompt,profile=self.jobs.get()
            self.events.put((key,'working',''))
            try:
                settings=copy.copy(self.args)
                settings.project=Path(profile.project)
                settings.backend,settings.sandbox=profile.backend,profile.sandbox
                # A Codex resume contains its own durable conversation. Fallback
                # history remains for new, mock, or unavailable sessions.
                agent_prompt=prompt if profile.codex_session and profile.backend=='codex' else context.prompt(key,prompt,profile.id)
                def save_session(value):
                    try:
                        self.chats.set_session(profile.id,value)
                    except (OSError, sqlite3.Error, ValueError) as exc:
                        self.record_transport('chat_session_save_failed', chat=profile.id, error=str(exc))
                state,reply=run_agent(settings,agent_prompt,
                                      on_update=lambda text:self.events.put((key,'streaming',text)),
                                      on_session=save_session,session_id=profile.codex_session)
                if state=='done': context.remember(key,reply)
            except Exception as exc:
                state,reply='failed',str(exc)
            self.events.put((key,state,reply))
            self.jobs.task_done()

    def record_transport(self, event, **details):
        # Bounded metadata only: no screenshots, prompt text or reply text.
        path=self.args.state/'transport.jsonl'
        try:
            if path.exists() and path.stat().st_size>1_000_000:
                path.replace(path.with_name('transport.previous.jsonl'))
            with path.open('a',encoding='utf-8') as log:
                log.write(json.dumps({'time':time.time(),'event':event,**details})+'\n')
        except OSError:
            pass

    def publish_reply(self, control, snapshot):
        publisher={
            'font': getattr(self, 'native', None),
            'image': getattr(self, 'visual', None),
            'slot': getattr(self, 'slots', None),
        }.get(control.kind)
        if publisher is None or time.monotonic()<self.return_retry_at:
            return
        try:
            if publisher.accept(control,snapshot):
                self.record_transport('written',kind=control.kind,session=control.session,
                                      request=control.request,slot=control.slot,page=control.page)
        except OSError as exc:
            # A transient file sharing/write failure must not disable replies
            # until the companion restarts. The publisher still enforces its
            # first-load deadline and freezes the bytes for this slot.
            self.return_retry_at=time.monotonic()+1
            self.record_transport('write_retry',slot=control.slot,error=str(exc))
        except ValueError as exc:
            self.write(control.kind+' return disabled: '+str(exc)+'. Prompts and companion replies still work.')
            self.record_transport('publisher_disabled',kind=control.kind,error=str(exc))
            if control.kind=='font': self.native=None
            elif control.kind=='slot': self.slots=None
            else: self.visual=None

    def tick(self):
        while not self.events.empty():
            key,state,reply=self.events.get_nowait()
            self.inbox.update(key,state,reply)
            self.show_job_status(state,key)
            self.write(f'[{state}] {key}\n{reply}')
            if state in ('done','failed'):
                self.notify(state,reply)
        if self.active:
            try:
                frame=decode_image(ImageGrab.grab(bbox=self.bbox,all_screens=True))
                if self.strip_visible is not True:
                    self.record_transport('strip_visible');self.strip_visible=True
                if frame[:4] in (b'CPBC',b'CPBN',b'CPBS'):
                    control = parse_control(frame)
                    key = f'{control.session}:{control.request}'
                    row = self.inbox.db.execute('SELECT id,prompt,state,reply FROM jobs WHERE id=?', (key,)).fetchone()
                    snapshot = dict(zip(('id','prompt','state','reply'), row)) if row else {'id':key,'state':'waiting'}
                    self.publish_reply(control,snapshot)
                    result = None
                else:
                    result=self.assembler.accept(frame)
                self.valid_frames += 1
                self.status.set(f'Receiving strip ({self.valid_frames} frames). Repeated messages are ignored.')
                if result and not self.jobs.full():
                    key,prompt=result
                    chat,prompt = unpack_chat(prompt)
                    if self.inbox.add(key,prompt,chat):
                        self.write(f'[queued] {key}  chat={chat}\nYou: {prompt}')
                        if not self.jobs.unfinished_tasks:
                            self.show_job_status('queued',key)
                        self.jobs.put_nowait((key,prompt,self.chats.resolve(chat,self.args)))
            except (ValueError,UnicodeError):
                if self.strip_visible is not False:
                    self.record_transport('strip_unreadable');self.strip_visible=False
                self.status.set('Waiting for valid strip: check crop, visibility and display scaling')
            except OSError as exc:
                self.active=False; self.status.set('Capture stopped: '+str(exc))
        self.project_button.configure(state='disabled' if self.jobs.unfinished_tasks else 'normal')
        self.chat_button.configure(state='disabled' if self.jobs.unfinished_tasks else 'normal')
        self.reset_chat_button.configure(state='disabled' if self.jobs.unfinished_tasks else 'normal')
        if not self.closed: self.root.after(70,self.tick)

    def close(self):
        if self.jobs.unfinished_tasks:
            self.active=False
            self.status.set('Capture paused. Wait for queued/running jobs before closing.')
            return
        self.closed=True; self.banner.dismiss(); self.inbox.db.close(); self.root.destroy()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend',choices=['mock','codex'],default='mock')
    parser.add_argument('--codex',default='codex',help='Path to native Codex executable')
    parser.add_argument('--project',type=Path,default=Path.cwd())
    parser.add_argument('--state',type=Path,default=Path('state'))
    parser.add_argument('--sandbox',choices=['read-only','workspace-write'],default='read-only')
    parser.add_argument('--timeout',type=int,default=600)
    parser.add_argument('--region',type=int,nargs=4,metavar=('X','Y','W','H'))
    parser.add_argument('--start-capture',action='store_true')
    parser.add_argument('--minimized',action='store_true')
    parser.add_argument('--addon',type=Path,help='Installed CodexPixelBridge directory with its prepared image bank')
    args=parser.parse_args()
    if args.region is None:
        try:
            region=json.loads((args.state/'capture.json').read_text(encoding='utf-8'))['region']
            if len(region)!=4 or not all(isinstance(n,int) for n in region):
                raise ValueError('Invalid saved region')
            args.region=region
        except (OSError,ValueError,KeyError,TypeError):
            args.region=[8,8,512,16]
    args.project=args.project.resolve()
    if not args.project.is_dir(): parser.error('--project must be an existing directory')
    if args.timeout <= 0: parser.error('--timeout must be positive')
    if hasattr(ctypes,'windll'):
        try: ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except OSError: pass
    App(tk.Tk(),args).root.mainloop()

if __name__=='__main__': main()
