from __future__ import annotations

import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .settings import Settings, get_settings


class UiFallbackError(RuntimeError):
    pass


def _env(settings: Settings) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("DISPLAY", settings.display)
    env.setdefault("XAUTHORITY", settings.xauthority)
    return env


def _run(args: list[str], *, settings: Settings, timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(args, env=_env(settings), capture_output=True, text=True, timeout=timeout)


def _safe_slug(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value)[:80].strip("-") or "chat"


def _screenshot(settings: Settings, name: str) -> str | None:
    path = settings.screenshots_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{_safe_slug(name)}.png"
    result = _run(["maim", str(path)], settings=settings, timeout=10)
    if result.returncode == 0 and path.exists() and path.stat().st_size > 1000:
        return str(path)
    return None


def _find_firefox_whatsapp(settings: Settings) -> str | None:
    for name in ("WhatsApp Business", "WhatsApp"):
        result = _run(["xdotool", "search", "--name", name], settings=settings, timeout=5)
        if result.returncode == 0:
            ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
            if ids:
                return ids[0]
    return None


def _launch_firefox(settings: Settings) -> str:
    wid = _find_firefox_whatsapp(settings)
    if wid:
        return wid
    subprocess.Popen(
        ["setsid", "-f", "firefox", "-P", "default-release", "https://web.whatsapp.com"],
        env=_env(settings),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        wid = _find_firefox_whatsapp(settings)
        if wid:
            return wid
        time.sleep(0.5)
    raise UiFallbackError("Could not find or launch a visible Firefox WhatsApp window.")


def send_whatsapp_message(phone: str, message: str, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    if not digits:
        raise UiFallbackError("A numeric WhatsApp phone target is required for UI fallback.")
    if not message:
        raise UiFallbackError("Message is required.")

    wid = _launch_firefox(settings)
    for args in (
        ["xdotool", "windowactivate", "--sync", wid],
        ["xdotool", "windowraise", wid],
        ["xdotool", "key", "--window", wid, "ctrl+l"],
    ):
        _run(args, settings=settings, timeout=10)
        time.sleep(0.2)

    _run(["xdotool", "type", "--window", wid, f"https://web.whatsapp.com/send?phone={digits}"], settings=settings)
    _run(["xdotool", "key", "--window", wid, "Return"], settings=settings)
    time.sleep(14)
    before = _screenshot(settings, f"whatsapp-{digits}-before-send")

    _run(["xdotool", "windowactivate", "--sync", wid], settings=settings)
    _run(["xdotool", "mousemove", "800", "697"], settings=settings)
    _run(["xdotool", "click", "1"], settings=settings)
    time.sleep(0.3)
    _run(["xdotool", "type", "--delay", "20", str(message)], settings=settings)
    typed = _screenshot(settings, f"whatsapp-{digits}-typed")
    _run(["xdotool", "key", "Return"], settings=settings)
    time.sleep(2)
    after = _screenshot(settings, f"whatsapp-{digits}-sent")

    return {
        "ok": True,
        "platform": "whatsapp",
        "browser": "firefox",
        "chat": digits,
        "fallback": "visible-firefox-xdotool",
        "context_before_send": [],
        "context_after_send": [],
        "screenshots": {
            "before": before,
            "typed": typed,
            "after": after,
        },
    }
