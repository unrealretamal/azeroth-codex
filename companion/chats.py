"""Durable per-chat execution settings and Codex thread identities."""
from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
import re
import sqlite3
import time

CHAT_ID = re.compile(r'^[a-z0-9][a-z0-9_-]{0,31}$')
SESSION_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$')
BACKENDS = {'mock', 'codex'}
SANDBOXES = {'read-only', 'workspace-write'}


@dataclass(frozen=True)
class ChatProfile:
    id: str
    project: Path
    backend: str
    sandbox: str
    codex_session: str | None


def _chat_id(value):
    value = str(value).lower()
    if not CHAT_ID.fullmatch(value):
        raise ValueError('Chat names use 1–32 lowercase letters, numbers, hyphens or underscores')
    return value


def _project(value):
    path = Path(value).resolve()
    if not path.is_dir():
        raise ValueError('Chat work folder is unavailable')
    return path


class ChatStore:
    """Store profiles in the inbox database; worker access uses its own connection."""
    def __init__(self, database):
        self.database = Path(database)
        with self._database() as db:
            db.execute(
                'CREATE TABLE IF NOT EXISTS chat_profiles ('
                'id TEXT PRIMARY KEY, project TEXT NOT NULL, backend TEXT NOT NULL, '
                'sandbox TEXT NOT NULL, codex_session TEXT, updated_at REAL NOT NULL)'
            )
            columns = {row[1] for row in db.execute('PRAGMA table_info(chat_profiles)')}
            if 'title' not in columns:
                db.execute("ALTER TABLE chat_profiles ADD COLUMN title TEXT NOT NULL DEFAULT ''")
            if 'archived' not in columns:
                db.execute('ALTER TABLE chat_profiles ADD COLUMN archived INTEGER NOT NULL DEFAULT 0')

    def _connect(self):
        connection = sqlite3.connect(self.database, timeout=5)
        connection.execute('PRAGMA busy_timeout=5000')
        return connection

    @contextmanager
    def _database(self):
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _profile(row):
        return ChatProfile(row[0], Path(row[1]), row[2], row[3], row[4])

    def resolve(self, chat, defaults):
        chat = _chat_id(chat)
        with self._database() as db:
            row = db.execute(
                'SELECT id,project,backend,sandbox,codex_session FROM chat_profiles WHERE id=?', (chat,)
            ).fetchone()
            if row:
                return self._profile(row)
            project = _project(defaults.project)
            backend, sandbox = defaults.backend, defaults.sandbox
            if backend not in BACKENDS or sandbox not in SANDBOXES:
                raise ValueError('Invalid default agent settings')
            if db.execute('SELECT count(*) FROM chat_profiles WHERE archived=0').fetchone()[0] >= 16:
                raise ValueError('Maximum 16 active chats. Archive a chat first.')
            db.execute('INSERT INTO chat_profiles (id,project,backend,sandbox,codex_session,updated_at) VALUES (?, ?, ?, ?, NULL, ?)',
                       (chat, str(project), backend, sandbox, time.time()))
            return ChatProfile(chat, project, backend, sandbox, None)

    def configure(self, chat, project, backend, sandbox):
        chat, project = _chat_id(chat), _project(project)
        if backend not in BACKENDS or sandbox not in SANDBOXES:
            raise ValueError('Invalid chat agent settings')
        with self._database() as db:
            previous = db.execute('SELECT project,backend,sandbox FROM chat_profiles WHERE id=?', (chat,)).fetchone()
            session = None if not previous or previous != (str(project), backend, sandbox) else db.execute(
                'SELECT codex_session FROM chat_profiles WHERE id=?', (chat,)
            ).fetchone()[0]
            db.execute(
                'INSERT INTO chat_profiles (id,project,backend,sandbox,codex_session,updated_at) VALUES (?, ?, ?, ?, ?, ?) '
                'ON CONFLICT(id) DO UPDATE SET project=excluded.project, backend=excluded.backend, '
                'sandbox=excluded.sandbox, codex_session=excluded.codex_session, updated_at=excluded.updated_at',
                (chat, str(project), backend, sandbox, session, time.time()),
            )
        return ChatProfile(chat, project, backend, sandbox, session)

    def set_session(self, chat, session):
        chat = _chat_id(chat)
        if not isinstance(session, str) or not SESSION_ID.fullmatch(session):
            raise ValueError('Invalid Codex session ID')
        with self._database() as db:
            result = db.execute('UPDATE chat_profiles SET codex_session=?,updated_at=? WHERE id=?',
                                (session, time.time(), chat))
            if not result.rowcount:
                raise ValueError('Unknown chat profile')

    def clear_session(self, chat):
        chat = _chat_id(chat)
        with self._database() as db:
            db.execute('UPDATE chat_profiles SET codex_session=NULL,updated_at=? WHERE id=?', (time.time(), chat))

    def profiles(self):
        with self._database() as db:
            rows = db.execute(
                'SELECT id,project,backend,sandbox,codex_session FROM chat_profiles ORDER BY id'
            ).fetchall()
        return [self._profile(row) for row in rows]

    def command(self, chat, operation, value, defaults):
        profile = self.resolve(chat, defaults)
        if operation == 'folder':
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = profile.project / path
            self.configure(chat, path, profile.backend, profile.sandbox)
        elif operation == 'backend':
            self.configure(chat, profile.project, value, profile.sandbox)
        elif operation == 'reset':
            self.clear_session(chat)
        elif operation == 'rename':
            if not value.strip() or len(value.encode('utf-8')) > 80:
                raise ValueError('Chat title must contain 1–80 UTF-8 bytes')
            with self._database() as db:
                db.execute('UPDATE chat_profiles SET title=? WHERE id=?', (value.strip(), chat))
        elif operation == 'archive':
            if chat == 'default':
                raise ValueError('Default chat cannot be archived')
            with self._database() as db:
                db.execute('UPDATE chat_profiles SET archived=1 WHERE id=?', (chat,))
        elif operation in ('sync', 'new'):
            with self._database() as db:
                archived=db.execute('SELECT archived FROM chat_profiles WHERE id=?',(chat,)).fetchone()[0]
                if archived and db.execute('SELECT count(*) FROM chat_profiles WHERE archived=0').fetchone()[0]>=16:
                    raise ValueError('Maximum 16 active chats. Archive a chat first.')
                db.execute('UPDATE chat_profiles SET archived=0 WHERE id=?', (chat,))
        else:
            raise ValueError('Unknown chat operation')
        return 'Chat settings saved.' if operation != 'sync' else 'Bridge connected.'

    def update_job(self, key, state, reply):
        with self._database() as db:
            db.execute('UPDATE jobs SET state=?,reply=? WHERE id=?', (state, reply, key))

    def snapshot(self, session):
        """Bounded recovery snapshot. Full transcripts stay in SQLite."""
        with self._database() as db:
            profiles = db.execute(
                'SELECT id,title,project,backend,sandbox FROM chat_profiles WHERE archived=0 ORDER BY id LIMIT 16'
            ).fetchall()
            chats = []
            for chat, title, project, backend, sandbox in profiles:
                rows = db.execute(
                    "SELECT id,prompt,state,reply FROM jobs WHERE chat=? AND operation='send' ORDER BY rowid DESC LIMIT 8",
                    (chat,)).fetchall()
                chats.append(dict(id=chat, title=title or chat, project=project, backend=backend, sandbox=sandbox,
                                  messages=[dict(zip(('id','prompt','state','text'), row)) for row in reversed(rows)]))
            receipts = db.execute(
                'SELECT id,state,reply,operation FROM jobs WHERE id LIKE ? ORDER BY rowid DESC LIMIT 32',
                (session + ':%',)).fetchall()
        return chats, [dict(id=row[0], state=row[1], text=row[2] if row[3] != 'send' else '') for row in receipts]
