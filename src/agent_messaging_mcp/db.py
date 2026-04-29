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
                      raw jsonb,
                      created_at timestamptz not null default now()
                    );
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
                  raw text,
                  created_at text not null
                );
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
    ) -> int:
        now = utc_now()
        if self._backend == "postgres":
            assert self._pg is not None
            with self._pg.cursor() as cur:
                cur.execute(
                    """
                    insert into chats(platform, browser, chat_key, title, phone, last_seen_at, created_at, updated_at)
                    values (%s, %s, %s, %s, %s, now(), now(), now())
                    on conflict(platform, chat_key) do update set
                      browser = excluded.browser,
                      title = coalesce(excluded.title, chats.title),
                      phone = coalesce(excluded.phone, chats.phone),
                      last_seen_at = now(),
                      updated_at = now()
                    returning id
                    """,
                    (platform, browser, chat_key, title, phone),
                )
                return int(cur.fetchone()[0])

        with self._sqlite() as conn:
            conn.execute(
                """
                insert into chats(platform, browser, chat_key, title, phone, last_seen_at, created_at, updated_at)
                values (?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(platform, chat_key) do update set
                  browser = excluded.browser,
                  title = coalesce(excluded.title, chats.title),
                  phone = coalesce(excluded.phone, chats.phone),
                  last_seen_at = excluded.last_seen_at,
                  updated_at = excluded.updated_at
                """,
                (platform, browser, chat_key, title, phone, now, now, now),
            )
            row = conn.execute(
                "select id from chats where platform = ? and chat_key = ?",
                (platform, chat_key),
            ).fetchone()
            return int(row["id"])

    def insert_messages(self, chat_id: int, platform: str, messages: Iterable[dict[str, Any]]) -> int:
        rows = list(messages)
        if not rows:
            return 0
        now = utc_now()
        if self._backend == "postgres":
            assert self._pg is not None
            with self._pg.cursor() as cur:
                cur.executemany(
                    """
                    insert into messages(chat_id, platform, direction, sender, body, sent_at, raw, created_at)
                    values (%s, %s, %s, %s, %s, %s, %s, now())
                    """,
                    [
                        (
                            chat_id,
                            platform,
                            row.get("direction"),
                            row.get("sender"),
                            row.get("text") or row.get("body"),
                            row.get("time"),
                            json.dumps(row, ensure_ascii=False),
                        )
                        for row in rows
                    ],
                )
            return len(rows)

        with self._sqlite() as conn:
            conn.executemany(
                """
                insert into messages(chat_id, platform, direction, sender, body, sent_at, raw, created_at)
                values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        chat_id,
                        platform,
                        row.get("direction"),
                        row.get("sender"),
                        row.get("text") or row.get("body"),
                        row.get("time"),
                        json.dumps(row, ensure_ascii=False),
                        now,
                    )
                    for row in rows
                ],
            )
        return len(rows)

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
