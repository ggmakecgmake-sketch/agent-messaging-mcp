# Agent Messaging MCP

Shared MCP server for Hermes and Claw to operate WhatsApp Web and Telegram Web through Python + Selenium.

## Tools

- `open_messaging_web(platform, browser="firefox")`
- `send_message(platform, chat, message, browser="firefox", context_messages=5)`
- `transcribe_chat(platform, chat, browser="firefox", max_scrolls=200)`
- `get_unanswered_chats(platform, browser="firefox", limit=50)`
- `database_status()`
- `close_messaging_browser(browser="firefox")`

`platform` must be `whatsapp` or `telegram`. Browser can be `firefox` or `chrome`; Firefox is the local default.

## Runtime

```bash
/home/cristian/agent-messaging-mcp/bin/agent-messaging-mcp
```

Hermes registers it as an MCP stdio server named `agent_messaging`.
Claw uses the `agent-messaging-mcp` skill and can call the same server through:

```bash
/home/cristian/agent-messaging-mcp/bin/agent-messaging-call database_status
```

Direct CLI helper:

```bash
/home/cristian/agent-messaging-mcp/bin/agent-messaging send \
  --platform whatsapp \
  --browser firefox \
  --chat 573186517885 \
  --message "BOT AUTO"
```

## Database

The primary database is local Postgres in Docker Swarm:

```bash
/home/cristian/agent-messaging-mcp/scripts/deploy_db_swarm.sh
```

The DSN is:

```text
postgresql://agentmsg:agentmsg@127.0.0.1:55432/agent_messaging
```

If Docker is not available to the current shell, tools fall back to SQLite at:

```text
/home/cristian/agent-messaging-mcp/data/messaging.sqlite3
```

This keeps the MCP usable while preserving the Swarm deployment as the durable target.

## Local GUI Note

On this server, Firefox profile `default-release` is the logged-in WhatsApp Business profile. If Selenium cannot attach to that profile, set:

```bash
export AGENT_MESSAGING_UI_FALLBACK=1
```

With that flag, WhatsApp sends use the same MCP/CLI entrypoint and operate the visible Firefox session through `xdotool`, saving screenshots under Brain Sync.

---

## Data Recovery Note — 2026-04-29 Session

During today's session, Telegram Web crashed mid-transcription. A full chat extraction of the **Cristian Gomez** Telegram DM was printed to the Python stdout buffer during `full_extract.py` / `safe_full_extract.py` execution but **failed to be INSERTed into the local SQLite database** before the script aborted.

**Status:**
- SQLite DB (`data/messaging.sqlite3`) holds the pre-incident state (1 WhatsApp chat, 1 auto message).
- Telegram data from today exists **only in the session log/output** of the `safe_full_extract.py` run and was not persisted.
- A `messaging_dump_20260429.sql` exports the current DB schema + data for portability.

**TODO for future session:**
1. Re-open Telegram Web and re-run `safe_full_extract.py` to rebuild the full transcript.
2. Verify INSERT returns non-zero row counts for each table.
3. Run `database_status()` to confirm total message and chat counts.

---

## Repository Link

https://github.com/ggmakecgmake-sketch/agent-messaging-mcp
