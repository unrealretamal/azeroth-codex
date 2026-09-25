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
            db.execute('INSERT INTO chat_profiles VALUES (?, ?, ?, ?, NULL, ?)',
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
                'INSERT INTO chat_profiles VALUES (?, ?, ?, ?, ?, ?) '
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
