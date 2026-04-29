PRAGMA foreign_keys=OFF;
BEGIN TRANSACTION;
CREATE TABLE chats (
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
INSERT INTO chats VALUES(1,'whatsapp','firefox','573186517885','573186517885','573186517885','2026-04-29T20:56:10.237656+00:00','2026-04-29T20:56:10.237656+00:00','2026-04-29T20:56:10.237656+00:00');
CREATE TABLE messages (
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
INSERT INTO messages VALUES(1,1,'whatsapp','out','agent','BOT AUTO','2026-04-29T20:56:10.247493+00:00','{"direction": "out", "sender": "agent", "text": "BOT AUTO", "time": "2026-04-29T20:56:10.247493+00:00", "raw_source": "manual-visible-firefox"}','2026-04-29T20:56:10.247531+00:00');
CREATE TABLE transcripts (
                  id integer primary key autoincrement,
                  platform text not null,
                  chat_key text not null,
                  file_path text not null,
                  message_count integer not null,
                  started_at text,
                  ended_at text,
                  created_at text not null
                );
DELETE FROM sqlite_sequence;
INSERT INTO sqlite_sequence VALUES('chats',1);
INSERT INTO sqlite_sequence VALUES('messages',1);
COMMIT;
