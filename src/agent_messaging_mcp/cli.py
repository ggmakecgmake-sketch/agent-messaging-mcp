from __future__ import annotations

import argparse
import json
import os
import sys

from .browser import BrowserController
from .db import MessageStore, utc_now
from .server import _write_transcript
from .settings import get_settings
from .ui_fallback import send_whatsapp_message


def _print(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_open(args: argparse.Namespace) -> int:
    controller = BrowserController(args.browser, get_settings())
    _print(controller.open_platform(args.platform))
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    controller = None
    if args.platform == "whatsapp" and os.environ.get("AGENT_MESSAGING_UI_FALLBACK", "0") == "1":
        result = send_whatsapp_message(args.chat, args.message, get_settings())
        browser_used = "firefox"
    else:
        controller = BrowserController(args.browser, get_settings())
        result = controller.send_message(args.platform, args.chat, args.message, context_messages=args.context_messages)
        browser_used = controller.browser
    store = MessageStore(get_settings())
    chat_id = store.upsert_chat(
        platform=args.platform,
        browser=browser_used,
        chat_key=args.chat,
        title=args.chat,
        phone="".join(ch for ch in args.chat if ch.isdigit()) or None,
    )
    inserted = store.insert_messages(
        chat_id,
        args.platform,
        [*(result.get("context_before_send") or []), {"direction": "out", "sender": "agent", "text": args.message, "time": utc_now()}],
    )
    result["stored_messages"] = inserted
    result["db"] = store.status.__dict__
    _print(result)
    if args.close_after and controller:
        controller.quit()
    return 0 if result.get("ok") else 1


def cmd_transcribe(args: argparse.Namespace) -> int:
    controller = BrowserController(args.browser, get_settings())
    messages = controller.transcribe_chat(args.platform, args.chat, max_scrolls=args.max_scrolls)
    transcript = _write_transcript(args.platform, args.chat, messages, args.output_dir)
    store = MessageStore(get_settings())
    chat_id = store.upsert_chat(platform=args.platform, browser=controller.browser, chat_key=args.chat, title=args.chat)
    inserted = store.insert_messages(chat_id, args.platform, messages)
    store.insert_transcript(platform=args.platform, chat_key=args.chat, file_path=transcript, message_count=len(messages))
    _print(
        {
            "ok": True,
            "platform": args.platform,
            "chat": args.chat,
            "message_count": len(messages),
            "stored_messages": inserted,
            "transcript": transcript,
            "db": store.status.__dict__,
        }
    )
    return 0


def cmd_unanswered(args: argparse.Namespace) -> int:
    controller = BrowserController(args.browser, get_settings())
    rows = controller.get_unanswered_chats(args.platform, limit=args.limit)
    _print({"ok": True, "platform": args.platform, "count": len(rows), "chats": rows})
    return 0


def cmd_db_status(args: argparse.Namespace) -> int:
    store = MessageStore(get_settings())
    _print({"ok": True, "db": store.status.__dict__})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agent Messaging MCP helper CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    open_p = sub.add_parser("open")
    open_p.add_argument("--platform", required=True, choices=["whatsapp", "telegram"])
    open_p.add_argument("--browser", default="firefox", choices=["firefox", "chrome"])
    open_p.set_defaults(func=cmd_open)

    send_p = sub.add_parser("send")
    send_p.add_argument("--platform", required=True, choices=["whatsapp", "telegram"])
    send_p.add_argument("--browser", default="firefox", choices=["firefox", "chrome"])
    send_p.add_argument("--chat", required=True)
    send_p.add_argument("--message", required=True)
    send_p.add_argument("--context-messages", type=int, default=5)
    send_p.add_argument("--close-after", action="store_true")
    send_p.set_defaults(func=cmd_send)

    transcribe_p = sub.add_parser("transcribe")
    transcribe_p.add_argument("--platform", required=True, choices=["whatsapp", "telegram"])
    transcribe_p.add_argument("--browser", default="firefox", choices=["firefox", "chrome"])
    transcribe_p.add_argument("--chat", required=True)
    transcribe_p.add_argument("--max-scrolls", type=int, default=200)
    transcribe_p.add_argument("--output-dir")
    transcribe_p.set_defaults(func=cmd_transcribe)

    unread_p = sub.add_parser("unanswered")
    unread_p.add_argument("--platform", required=True, choices=["whatsapp", "telegram"])
    unread_p.add_argument("--browser", default="firefox", choices=["firefox", "chrome"])
    unread_p.add_argument("--limit", type=int, default=50)
    unread_p.set_defaults(func=cmd_unanswered)

    db_p = sub.add_parser("db-status")
    db_p.set_defaults(func=cmd_db_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        _print({"ok": False, "error": str(exc), "type": exc.__class__.__name__})
        return 1


if __name__ == "__main__":
    sys.exit(main())
