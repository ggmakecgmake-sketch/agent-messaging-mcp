from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from .settings import Settings, get_settings


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class StoreStatus:
    backend: str
    path_or_dsn: str
    ok: bool
    note: str = ""


class MessageStore:
    """Small storage layer with Postgres first and SQLite fallback.

    The requested durable backend is Postgres through Docker Swarm. SQLite is
    only a local fallback so tools keep working when the current shell cannot
    talk to Docker or the stack is not up yet.
    """

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.settings.ensure_dirs()
        self._backend = "sqlite"
        self._pg = None
        self._status = StoreStatus(
            backend="sqlite",
            path_or_dsn=str(self.settings.sqlite_path),
            ok=True,
            note="Postgres not attempted yet.",
        )
        self._connect()
        self.init_schema()

    @property
    def status(self) -> StoreStatus:
        return self._status

    def _connect(self) -> None:
        if self.settings.db_dsn.startswith(("postgres://", "postgresql://")):
            try:
                import psycopg

                self._pg = psycopg.connect(self.settings.db_dsn, connect_timeout=3)
                self._pg.autocommit = True
                self._backend = "postgres"
                self._status = StoreStatus("postgres", self._redact_dsn(self.settings.db_dsn), True)
                return
            except Exception as exc:
                if not self.settings.sqlite_fallback:
                    raise
                self._backend = "sqlite"
                self._status = StoreStatus(
                    "sqlite",
                    str(self.settings.sqlite_path),
                    True,
                    f"Postgres unavailable; using SQLite fallback: {exc}",
                )
        else:
            self._backend = "sqlite"
            self._status = StoreStatus("sqlite", str(self.settings.sqlite_path), True)

    @staticmethod
    def _redact_dsn(dsn: str) -> str:
        parsed = urlparse(dsn)
        if parsed.password:
            return dsn.replace(parsed.password, "***")
        return dsn

    @contextmanager
    def _sqlite(self):
        conn = sqlite3.connect(self.settings.sqlite_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> None:
        if self._backend == "postgres":
            assert self._pg is not None
            with self._pg.cursor() as cur:
                cur.execute(
                    """
                    create table if not exists chats (
                      id bigserial primary key,
                      platform text not null,
                      browser text not null,
                      chat_key text not null,
                      title text,
                      phone text,
                      last_seen_at timestamptz,
                      last_synced_at timestamptz,
                      created_at timestamptz not null default now(),
                      updated_at timestamptz not null default now(),
                      unique(platform, chat_key)
                    );
                    create table if not exists messages (
                      id bigserial primary key,
                      chat_id bigint references chats(id) on delete cascade,
                      platform text not null,
                      direction text,
                      sender text,
                      body text,
                      sent_at text,
                      sent_at_parsed timestamptz,
                      sent_at_unix double precision,
                      message_hash text,
                      raw jsonb,
                      created_at timestamptz not null default now(),
                      unique(chat_id, message_hash)
                    );
                    create index if not exists idx_messages_hash on messages(chat_id, message_hash);
                    create index if not exists idx_messages_chat_parsed on messages(chat_id, sent_at_parsed);
                    create table if not exists transcripts (
                      id bigserial primary key,
                      platform text not null,
                      chat_key text not null,
                      file_path text not null,
                      message_count integer not null,
                      started_at text,
                      ended_at text,
                      created_at timestamptz not null default now()
                    );
                    create table if not exists sync_logs (
                      id bigserial primary key,
                      platform text not null,
                      chat_key text not null,
                      status text not null default 'started',
                      messages_before integer default 0,
                      messages_after integer default 0,
                      messages_inserted integer default 0,
                      started_at timestamptz not null default now(),
                      ended_at timestamptz,
                      error_message text,
                      created_at timestamptz not null default now()
                    );
                    """
                )
            return

        with self._sqlite() as conn:
            conn.executescript(
                """
                create table if not exists chats (
                  id integer primary key autoincrement,
                  platform text not null,
                  browser text not null,
                  chat_key text not null,
                  title text,
                  phone text,
                  last_seen_at text,
                  last_synced_at text,
                  created_at text not null,
                  updated_at text not null,
                  unique(platform, chat_key)
                );
                create table if not exists messages (
                  id integer primary key autoincrement,
                  chat_id integer references chats(id) on delete cascade,
                  platform text not null,
                  direction text,
                  sender text,
                  body text,
                  sent_at text,
                  sent_at_parsed text,
                  sent_at_unix real,
                  message_hash text,
                  raw text,
                  created_at text not null
                );
                create unique index if not exists idx_msg_unique_hash on messages(chat_id, message_hash);
                create index if not exists idx_messages_chat_parsed on messages(chat_id, sent_at_parsed);
                create table if not exists transcripts (
                  id integer primary key autoincrement,
                  platform text not null,
                  chat_key text not null,
                  file_path text not null,
                  message_count integer not null,
                  started_at text,
                  ended_at text,
                  created_at text not null
                );
                create table if not exists sync_logs (
                  id integer primary key autoincrement,
                  platform text not null,
                  chat_key text not null,
                  status text not null default 'started',
                  messages_before integer default 0,
                  messages_after integer default 0,
                  messages_inserted integer default 0,
                  started_at text not null,
                  ended_at text,
                  error_message text,
                  created_at text not null default current_timestamp
                );
                """
            )

    def upsert_chat(
        self,
        *,
        platform: str,
        browser: str,
        chat_key: str,
        title: str | None = None,
        phone: str | None = None,
        last_synced_at: str | None = None,
    ) -> int:
        now = utc_now()
        if self._backend == "postgres":
            assert self._pg is not None
            with self._pg.cursor() as cur:
                cur.execute(
                    """
                    insert into chats(platform, browser, chat_key, title, phone, last_seen_at, last_synced_at, created_at, updated_at)
                    values (%s, %s, %s, %s, %s, now(), %s, now(), now())
                    on conflict(platform, chat_key) do update set
                      browser = excluded.browser,
                      title = coalesce(excluded.title, chats.title),
                      phone = coalesce(excluded.phone, chats.phone),
                      last_seen_at = now(),
                      last_synced_at = coalesce(excluded.last_synced_at, chats.last_synced_at),
                      updated_at = now()
                    returning id
                    """,
                    (platform, browser, chat_key, title, phone, last_synced_at),
                )
                return int(cur.fetchone()[0])

        with self._sqlite() as conn:
            sync_val = last_synced_at or now
            conn.execute(
                """
                insert into chats(platform, browser, chat_key, title, phone, last_seen_at, last_synced_at, created_at, updated_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(platform, chat_key) do update set
                  browser = excluded.browser,
                  title = coalesce(excluded.title, chats.title),
                  phone = coalesce(excluded.phone, chats.phone),
                  last_seen_at = excluded.last_seen_at,
                  last_synced_at = coalesce(excluded.last_synced_at, chats.last_synced_at),
                  updated_at = excluded.updated_at
                """,
                (platform, browser, chat_key, title, phone, now, sync_val, now, now),
            )
            row = conn.execute(
                "select id from chats where platform = ? and chat_key = ?",
                (platform, chat_key),
            ).fetchone()
            return int(row["id"])

    @staticmethod
    def _compute_msg_hash(chat_id: int, platform: str, direction: str, sender: str, body: str, sent_at: str) -> str:
        from hashlib import sha256
        payload = f"{chat_id}|{platform}|{direction}|{sender}|{body}|{sent_at}"
        return sha256(payload.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _parse_whatsapp_sent_at(raw: str | None):
        import re
        from datetime import datetime
        if not raw:
            return None, None
        m = re.search(r'\[(\d{1,2}):(\d{2})\s*([ap]\.?\s*m\.?)?,\s*(\d{1,2})/(\d{1,2})/(\d{4})\]', str(raw).lower())
        if m:
            hour, minute = int(m.group(1)), int(m.group(2))
            ampm = (m.group(3) or "").lower()
            day, month, year = int(m.group(4)), int(m.group(5)), int(m.group(6))
            if "p" in ampm and hour != 12:
                hour += 12
            elif "a" in ampm and hour == 12:
                hour = 0
            try:
                dt = datetime(year, month, day, hour, minute)
                return dt.strftime("%Y-%m-%dT%H:%M:%S"), float(dt.timestamp())
            except Exception:
                pass
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%dT%H:%M:%S"), float(dt.timestamp())
        except Exception:
            pass
        return None, None

    def insert_messages(self, chat_id: int, platform: str, messages: Iterable[dict[str, Any]]) -> int:
        rows = list(messages)
        if not rows:
            return 0

        inserted = 0
        if self._backend == "postgres":
            assert self._pg is not None
            with self._pg.cursor() as cur:
                for row in rows:
                    raw_time = row.get("time") or row.get("sent_at") or ""
                    parsed_iso, parsed_unix = self._parse_whatsapp_sent_at(raw_time)
                    body = row.get("text") or row.get("body") or ""
                    msg_hash = self._compute_msg_hash(
                        chat_id, platform,
                        row.get("direction") or "", row.get("sender") or "",
                        body, raw_time,
                    )
                    try:
                        cur.execute(
                            """
                            insert into messages(chat_id, platform, direction, sender, body, sent_at,
                                sent_at_parsed, sent_at_unix, message_hash, raw, created_at)
                            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                            on conflict(chat_id, message_hash) do nothing
                            """,
                            (
                                chat_id, platform, row.get("direction"), row.get("sender"),
                                body, raw_time, parsed_iso, parsed_unix,
                                msg_hash, json.dumps(row, ensure_ascii=False),
                            ),
                        )
                        if cur.rowcount > 0:
                            inserted += 1
                    except Exception:
                        pass
            return inserted

        with self._sqlite() as conn:
            for row in rows:
                raw_time = row.get("time") or row.get("sent_at") or ""
                parsed_iso, parsed_unix = self._parse_whatsapp_sent_at(raw_time)
                body = row.get("text") or row.get("body") or ""
                msg_hash = self._compute_msg_hash(
                    chat_id, platform,
                    row.get("direction") or "", row.get("sender") or "",
                    body, raw_time,
                )
                try:
                    conn.execute(
                        """
                        insert into messages(chat_id, platform, direction, sender, body, sent_at,
                            sent_at_parsed, sent_at_unix, message_hash, raw, created_at)
                        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            chat_id, platform, row.get("direction"), row.get("sender"),
                            body, raw_time, parsed_iso, parsed_unix,
                            msg_hash, json.dumps(row, ensure_ascii=False), utc_now(),
                        ),
                    )
                    inserted += 1
                except sqlite3.IntegrityError:
                    pass
        return inserted

    def log_sync_start(self, platform: str, chat_key: str) -> int:
        now = utc_now()
        if self._backend == "postgres":
            assert self._pg is not None
            with self._pg.cursor() as cur:
                cur.execute(
                    "insert into sync_logs(platform, chat_key, status, started_at) values (%s, %s, %s, %s) returning id",
                    (platform, chat_key, "started", now),
                )
                return int(cur.fetchone()[0])
        with self._sqlite() as conn:
            cur2 = conn.execute(
                "insert into sync_logs(platform, chat_key, status, started_at) values (?, ?, ?, ?)",
                (platform, chat_key, "started", now),
            )
            return cur2.lastrowid or 0

    def log_sync_end(self, log_id: int, status: str, messages_before: int, messages_after: int, messages_inserted: int, error: str | None = None) -> None:
        now = utc_now()
        if self._backend == "postgres":
            assert self._pg is not None
            with self._pg.cursor() as cur:
                cur.execute(
                    """update sync_logs
                    set status=%s, messages_before=%s, messages_after=%s, messages_inserted=%s,
                        ended_at=%s, error_message=%s
                    where id=%s""",
                    (status, messages_before, messages_after, messages_inserted, now, error or None, log_id),
                )
            return
        with self._sqlite() as conn:
            conn.execute(
                """update sync_logs
                set status=?, messages_before=?, messages_after=?, messages_inserted=?,
                    ended_at=?, error_message=?
                where id=?""",
                (status, messages_before, messages_after, messages_inserted, now, error or None, log_id),
            )

    def count_messages(self, chat_id: int | None = None) -> int:
        if self._backend == "postgres":
            assert self._pg is not None
            with self._pg.cursor() as cur:
                if chat_id:
                    cur.execute("select count(*) from messages where chat_id = %s", (chat_id,))
                else:
                    cur.execute("select count(*) from messages")
                return int(cur.fetchone()[0])
        sql = "select count(*) from messages where chat_id = ?" if chat_id else "select count(*) from messages"
        with self._sqlite() as conn:
            if chat_id:
                row = conn.execute(sql, (chat_id,)).fetchone()
            else:
                row = conn.execute(sql).fetchone()
            return int(row[0]) if row else 0

    def insert_transcript(
        self,
        *,
        platform: str,
        chat_key: str,
        file_path: str | Path,
        message_count: int,
        started_at: str | None = None,
        ended_at: str | None = None,
    ) -> None:
        if self._backend == "postgres":
            assert self._pg is not None
            with self._pg.cursor() as cur:
                cur.execute(
                    """
                    insert into transcripts(platform, chat_key, file_path, message_count, started_at, ended_at, created_at)
                    values (%s, %s, %s, %s, %s, %s, now())
                    """,
                    (platform, chat_key, str(file_path), message_count, started_at, ended_at),
                )
            return

        with self._sqlite() as conn:
            conn.execute(
                """
                insert into transcripts(platform, chat_key, file_path, message_count, started_at, ended_at, created_at)
                values (?, ?, ?, ?, ?, ?, ?)
                """,
                (platform, chat_key, str(file_path), message_count, started_at, ended_at, utc_now()),
            )
