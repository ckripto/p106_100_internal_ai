"""SQLite persistence for sessions and the durable task queue."""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from agents.shared import clip


class APIError(Exception):
    def __init__(self, message, status=400):
        self.message = message
        self.status = status


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.executescript("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    created REAL NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    request_id TEXT NOT NULL UNIQUE, prompt TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued', progress TEXT NOT NULL DEFAULT '',
                    result TEXT, created REAL NOT NULL, started REAL, finished REAL);
                CREATE INDEX IF NOT EXISTS tasks_session ON tasks(session_id, id);
                CREATE INDEX IF NOT EXISTS tasks_queue ON tasks(status, id);
                CREATE TABLE IF NOT EXISTS agent_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    attempt INTEGER NOT NULL,
                    step INTEGER,
                    sender TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created REAL NOT NULL,
                    response_seconds REAL);
                CREATE INDEX IF NOT EXISTS agent_messages_task
                    ON agent_messages(task_id, id);
            """)
            columns = {
                row["name"] for row in database.execute("PRAGMA table_info(agent_messages)")
            }
            if "step" not in columns:
                database.execute("ALTER TABLE agent_messages ADD COLUMN step INTEGER")

    @contextmanager
    def connect(self):
        database = sqlite3.connect(self.path, timeout=15)
        database.row_factory = sqlite3.Row
        database.execute("PRAGMA foreign_keys=ON")
        try:
            with database:
                yield database
        finally:
            database.close()

    @staticmethod
    def _task(row):
        data = dict(row)
        data["result"] = json.loads(data["result"]) if data["result"] else None
        data["agent_messages"] = []
        return data

    def create_session(self):
        now = time.time()
        session_id = uuid.uuid4().hex
        with self.connect() as database:
            database.execute(
                "INSERT INTO sessions VALUES (?,?,?,?)",
                (session_id, "Новая сессия", now, now),
            )
        return {"id": session_id, "title": "Новая сессия", "created": now, "updated": now}

    def sessions(self):
        with self.connect() as database:
            return [dict(row) for row in database.execute("""
                SELECT s.*,
                  (SELECT status FROM tasks WHERE session_id=s.id ORDER BY id DESC LIMIT 1) AS status,
                  (SELECT count(*) FROM tasks WHERE session_id=s.id AND status IN ('queued','running')) AS pending
                FROM sessions s ORDER BY updated DESC, id
            """)]

    def session_detail(self, session_id, before=None):
        with self.connect() as database:
            session = database.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            if session is None:
                raise APIError("Сессия не найдена", 404)
            rows = database.execute(
                "SELECT * FROM tasks WHERE session_id=? AND id<? ORDER BY id DESC LIMIT 51",
                (session_id, before or 9223372036854775807),
            ).fetchall()
            visible_rows = rows[:50]
            messages = []
            if visible_rows:
                placeholders = ",".join("?" for _ in visible_rows)
                messages = database.execute(
                    f"SELECT * FROM agent_messages WHERE task_id IN ({placeholders}) ORDER BY id",
                    [row["id"] for row in visible_rows],
                ).fetchall()
        tasks = {row["id"]: self._task(row) for row in visible_rows}
        for message in messages:
            tasks[message["task_id"]]["agent_messages"].append(dict(message))
        return {
            **dict(session),
            "tasks": [tasks[row["id"]] for row in reversed(visible_rows)],
            "has_more": len(rows) > 50,
        }

    def submit(self, session_id, prompt, request_id):
        if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 1800:
            raise APIError("Задача должна содержать от 1 до 1800 символов")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 100:
            raise APIError("Неверный идентификатор запроса")
        prompt = prompt.strip()
        with self.connect() as database:
            database.execute("BEGIN IMMEDIATE")
            old = database.execute("SELECT * FROM tasks WHERE request_id=?", (request_id,)).fetchone()
            if old:
                if old["session_id"] != session_id or old["prompt"] != prompt:
                    raise APIError("Идентификатор запроса уже использован", 409)
                return self._task(old)
            if not database.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone():
                raise APIError("Сессия не найдена", 404)
            now = time.time()
            first_task = not database.execute(
                "SELECT 1 FROM tasks WHERE session_id=?", (session_id,)
            ).fetchone()
            cursor = database.execute(
                "INSERT INTO tasks(session_id,request_id,prompt,created) VALUES (?,?,?,?)",
                (session_id, request_id, prompt, now),
            )
            if first_task:
                database.execute(
                    "UPDATE sessions SET title=?,updated=? WHERE id=?",
                    (" ".join(prompt.split())[:60], now, session_id),
                )
            else:
                database.execute(
                    "UPDATE sessions SET updated=? WHERE id=?", (now, session_id)
                )
            row = database.execute("SELECT * FROM tasks WHERE id=?", (cursor.lastrowid,)).fetchone()
            return self._task(row)

    def delete_session(self, session_id):
        with self.connect() as database:
            database.execute("BEGIN IMMEDIATE")
            if database.execute(
                "SELECT 1 FROM tasks WHERE session_id=? AND status='running'", (session_id,)
            ).fetchone():
                raise APIError("Дождитесь завершения текущей задачи перед удалением сессии", 409)
            if not database.execute("DELETE FROM sessions WHERE id=?", (session_id,)).rowcount:
                raise APIError("Сессия не найдена", 404)

    def recover_interrupted(self):
        result = json.dumps({
            "type": "final",
            "status": "failed",
            "summary": (
                "Выполнение прервано перезапуском сервиса. Некоторые действия могли "
                "выполниться. Проверьте результат перед повтором."
            ),
        }, ensure_ascii=False)
        with self.connect() as database:
            database.execute(
                "UPDATE tasks SET status='interrupted',progress='',result=?,finished=? "
                "WHERE status='running'",
                (result, time.time()),
            )

    def claim(self):
        with self.connect() as database:
            database.execute("BEGIN IMMEDIATE")
            if database.execute("SELECT 1 FROM tasks WHERE status='running'").fetchone():
                return None
            row = database.execute(
                "SELECT * FROM tasks WHERE status='queued' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            database.execute(
                "UPDATE tasks SET status='running',started=?,progress='Координатор принимает задачу' "
                "WHERE id=?",
                (time.time(), row["id"]),
            )
            return dict(row)

    def history(self, task):
        with self.connect() as database:
            rows = database.execute(
                "SELECT prompt,result FROM tasks WHERE session_id=? AND id<? "
                "AND status IN ('success','failed','interrupted') ORDER BY id DESC LIMIT 2",
                (task["session_id"], task["id"]),
            ).fetchall()
        history = []
        for row in reversed(rows):
            history.extend([
                {"role": "user", "content": clip(row["prompt"], 500)},
                {"role": "assistant", "content": row["result"]},
            ])
        return history

    def update_progress(self, task_id, text):
        with self.connect() as database:
            database.execute(
                "UPDATE tasks SET progress=? WHERE id=? AND status='running'",
                (clip(text, 240), task_id),
            )

    def append_agent_message(self, task_id, message):
        actors = {"coordinator", "executor", "developer", "llm"}
        if not isinstance(message, dict):
            raise ValueError("Agent message must be an object")
        attempt = message.get("attempt")
        step = message.get("step")
        sender = message.get("sender")
        recipient = message.get("recipient")
        kind = message.get("kind")
        content = message.get("content")
        created = message.get("created")
        response_seconds = message.get("response_seconds")
        if type(attempt) is not int or attempt < 1:
            raise ValueError("Invalid agent message attempt")
        if step is not None and (type(step) is not int or step < 1):
            raise ValueError("Invalid agent message step")
        if sender not in actors or recipient not in actors or sender == recipient:
            raise ValueError("Invalid agent message route")
        if kind not in {"request", "response"}:
            raise ValueError("Invalid agent message kind")
        if not isinstance(content, str) or not content or len(content) > 65_536:
            raise ValueError("Invalid agent message content")
        if not isinstance(created, (int, float)):
            raise ValueError("Invalid agent message timestamp")
        if response_seconds is not None and (
            not isinstance(response_seconds, (int, float)) or response_seconds < 0
        ):
            raise ValueError("Invalid agent response duration")
        with self.connect() as database:
            cursor = database.execute(
                "INSERT INTO agent_messages(task_id,attempt,step,sender,recipient,kind,content,created,response_seconds) "
                "SELECT ?,?,?,?,?,?,?,?,? WHERE EXISTS "
                "(SELECT 1 FROM tasks WHERE id=? AND status='running')",
                (
                    task_id, attempt, step, sender, recipient, kind, content,
                    created, response_seconds, task_id,
                ),
            )
            if not cursor.rowcount:
                raise ValueError("Cannot append a message to a task that is not running")

    def complete(self, task_id, result):
        if not isinstance(result, dict) or result.get("status") not in {"success", "failed"}:
            raise ValueError("Coordinator returned an invalid result")
        with self.connect() as database:
            database.execute(
                "UPDATE tasks SET status=?,result=?,progress='',finished=? "
                "WHERE id=? AND status='running'",
                (result["status"], json.dumps(result, ensure_ascii=False), time.time(), task_id),
            )
            database.execute(
                "UPDATE sessions SET updated=? WHERE id=(SELECT session_id FROM tasks WHERE id=?)",
                (time.time(), task_id),
            )
