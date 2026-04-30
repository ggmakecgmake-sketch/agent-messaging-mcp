from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .browser import BrowserController, BrowserError, normalize_browser, normalize_platform, _list_whatsapp_chats, _list_telegram_chats
from .db import MessageStore, utc_now
from .settings import get_settings
from .ui_fallback import send_whatsapp_message


mcp = FastMCP("agent-messaging-mcp")
_controllers: dict[str, BrowserController] = {}


def _controller(browser: str | None = None) -> BrowserController:
    settings = get_settings()
    key = normalize_browser(browser, settings)
    if key not in _controllers:
        _controllers[key] = BrowserController(key, settings)
    return _controllers[key]


def _store() -> MessageStore:
    return MessageStore(get_settings())


def _write_transcript(platform: str, chat: str, messages: list[dict[str, Any]], output_dir: str | None = None) -> str:
    settings = get_settings()
    base = Path(output_dir).expanduser() if output_dir else settings.transcripts_dir
    base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe_chat = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in chat)[:80].strip("-") or "chat"
    path = base / f"{stamp}-{platform}-{safe_chat}.md"
    lines = [
        f"# {platform.title()} Transcript - {chat}",
        "",
        f"- Created: {datetime.now().isoformat(timespec='seconds')}",
        f"- Message count: {len(messages)}",
        "",
    ]
    for msg in messages:
        direction = msg.get("direction") or "unknown"
        time_text = msg.get("time") or ""
        sender = msg.get("sender") or direction
        text = msg.get("text") or msg.get("body") or ""
        lines.append(f"## {sender} {time_text}".strip())
        lines.append("")
        lines.append(str(text).strip())
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")

    jsonl_path = path.with_suffix(".jsonl")
    jsonl_path.write_text(
        "\n".join(json.dumps(msg, ensure_ascii=False) for msg in messages) + ("\n" if messages else ""),
        encoding="utf-8",
    )
    return str(path)


def _error(exc: Exception) -> dict[str, Any]:
    return {"ok": False, "error": str(exc), "type": exc.__class__.__name__}


@mcp.tool()
def open_messaging_web(platform: str, browser: str = "firefox") -> dict[str, Any]:
    """Open one Selenium-controlled browser instance for WhatsApp Web or Telegram Web.

    The selected browser is closed first by default, so only one instance remains.
    Platform has no default: pass whatsapp or telegram. Browser defaults to firefox.
    """
    try:
        platform = normalize_platform(platform)
        c = _controller(browser)
        return c.open_platform(platform)
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def send_message(
    platform: str,
    chat: str,
    message: str,
    browser: str = "firefox",
    context_messages: int = 5,
) -> dict[str, Any]:
    """Open a chat, read the last N messages as context, send a message, and persist the context/outgoing message."""
    try:
        platform = normalize_platform(platform)
        if platform == "whatsapp" and os.environ.get("AGENT_MESSAGING_UI_FALLBACK", "0") == "1":
            result = send_whatsapp_message(chat, message, get_settings())
            browser_used = "firefox"
        else:
            c = _controller(browser)
            result = c.send_message(platform, chat, message, context_messages=context_messages)
            browser_used = c.browser
        store = _store()
        chat_id = store.upsert_chat(
            platform=platform,
            browser=browser_used,
            chat_key=str(chat),
            title=str(chat),
            phone="".join(ch for ch in str(chat) if ch.isdigit()) or None,
        )
        context = result.get("context_before_send") or []
        outgoing = [{"direction": "out", "sender": "agent", "text": message, "time": utc_now()}]
        inserted = store.insert_messages(chat_id, platform, [*context, *outgoing])
        result["stored_messages"] = inserted
        result["db"] = store.status.__dict__
        return result
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def transcribe_chat(
    platform: str,
    chat: str,
    browser: str = "firefox",
    max_scrolls: int = 200,
    output_dir: str | None = None,
) -> dict[str, Any]:
    """Scroll upward through one selected chat, save all loaded history to a new transcript file, and insert it into the local DB."""
    try:
        platform = normalize_platform(platform)
        c = _controller(browser)
        messages = c.transcribe_chat(platform, chat, max_scrolls=max_scrolls)
        transcript = _write_transcript(platform, chat, messages, output_dir)
        store = _store()
        chat_id = store.upsert_chat(platform=platform, browser=c.browser, chat_key=str(chat), title=str(chat))
        inserted = store.insert_messages(chat_id, platform, messages)
        store.insert_transcript(
            platform=platform,
            chat_key=str(chat),
            file_path=transcript,
            message_count=len(messages),
        )
        return {
            "ok": True,
            "platform": platform,
            "browser": c.browser,
            "chat": chat,
            "message_count": len(messages),
            "stored_messages": inserted,
            "transcript": transcript,
            "jsonl": str(Path(transcript).with_suffix(".jsonl")),
            "db": store.status.__dict__,
        }
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def get_unanswered_chats(platform: str, browser: str = "firefox", limit: int = 50) -> dict[str, Any]:
    """Return chats that appear unread/unanswered in WhatsApp Web or Telegram Web."""
    try:
        platform = normalize_platform(platform)
        c = _controller(browser)
        rows = c.get_unanswered_chats(platform, limit=limit)
        return {"ok": True, "platform": platform, "browser": c.browser, "count": len(rows), "chats": rows}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def database_status() -> dict[str, Any]:
    """Show which local database backend is active for the messaging MCP."""
    try:
        store = _store()
        return {"ok": True, "db": store.status.__dict__}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def close_messaging_browser(browser: str = "firefox") -> dict[str, Any]:
    """Close the Selenium-controlled messaging browser session for one browser."""
    try:
        key = normalize_browser(browser)
        controller = _controllers.pop(key, None)
        if controller:
            controller.quit()
        return {"ok": True, "browser": key, "closed": bool(controller)}
    except Exception as exc:
        return _error(exc)


@mcp.tool()
def get_chat_list(platform: str, browser: str = "firefox") -> dict[str, Any]:
    """List all visible chat titles from WhatsApp Web or Telegram Web."""
    try:
        platform = normalize_platform(platform)
        c = _controller(browser)
        c.open_platform(platform)
        time.sleep(3)  # Allow list to render
        driver = c.d
        if platform == "whatsapp":
            titles = _list_whatsapp_chats(driver)
        else:
            titles = _list_telegram_chats(driver)
        return {"ok": True, "platform": platform, "browser": c.browser, "count": len(titles), "chats": titles}
    except Exception as exc:
        return _error(exc)


def main() -> None:
    get_settings().ensure_dirs()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
