#!/usr/bin/env python3
"""
Extractor masivo: extrae TODOS los chats de WhatsApp Web
y guarda historial completo en SQLite.
"""
from __future__ import annotations
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agent_messaging_mcp.browser import BrowserController, normalize_platform
from agent_messaging_mcp.db import MessageStore, utc_now
from agent_messaging_mcp.settings import get_settings

def main() -> int:
    settings = get_settings()
    settings.ensure_dirs()
    store = MessageStore(settings)
    controller = BrowserController(browser="firefox", settings=settings)

    print("[1/5] Cerrando Firefox existente antes de abrir Selenium...")
    from agent_messaging_mcp.browser import close_browser_processes
    close_browser_processes("firefox", timeout=8.0)
    time.sleep(1.5)

    print("[2/5] Abriendo WhatsApp Web con Firefox/Selenium...")
    result = controller.open_platform("whatsapp")
    print(f"  → Plataforma: {result.get('platform')}, Screenshot: {result.get('screenshot')}")
    time.sleep(2)

    print("\n[3/5] Obteniendo lista de chats...")
    chats = controller._whatsapp_unread(limit=200)
    # Si está vacío, intentamos obtener TODAS las conversaciones
    if not chats:
        print("  → No hay chats no leídos. Escaneando TODA la lista lateral...")
        chats = controller.d.execute_script("""
        const items = Array.from(document.querySelectorAll('[role="listitem"]'));
        return items.map((item, i) => {
          const titleEl = item.querySelector('span[title]');
          const title = titleEl ? titleEl.getAttribute('title') : '';
          const texts = Array.from(item.querySelectorAll('span[title]'));
          const preview = texts.map(e => e.innerText).join(' ');
          return {i, title: title || ('chat-' + i), preview: preview || '', unread: false};
        }).filter(c => c.title);
        """) or []
    print(f"  → {len(chats)} chats encontrados")

    if not chats:
        print("ERROR: No se encontraron chats. ¿Está escaneada la sesión?")
        return 1

    print("\n[4/5] Procesando chats (abriendo + extrayendo mensajes)...")
    results = []
    for idx, chat_info in enumerate(chats, 1):
        title = chat_info.get("title", "").strip() or chat_info.get("preview", "").split("\n")[0].strip()
        if not title or title.lower() in ["chats", "comunidades", "archivados"]:
            continue
        if not title:
            continue

        print(f"\n  [{idx}/{len(chats)}] Chat: '{title}'")
        try:
            # Abrir chat
            from selenium.webdriver.common.by import By
            from selenium.webdriver.common.keys import Keys
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.support.ui import WebDriverWait

            # Buscar el chat en la lista lateral
            search_input = controller._wait_any([
                (By.XPATH, "//div[@contenteditable='true' and @role='textbox']"),
                (By.CSS_SELECTOR, "div[contenteditable='true'][data-tab='1']"),
            ], timeout=30)
            search_input.click()
            time.sleep(0.3)
            for _ in range(3):
                search_input.send_keys(Keys.CONTROL, "a")
                time.sleep(0.1)
                search_input.send_keys(Keys.DELETE)
            time.sleep(0.2)
            search_input.send_keys(title)
            time.sleep(1.5)
            search_input.send_keys(Keys.ENTER)
            time.sleep(2.5)

            # Extraer mensajes del chat
            messages = controller.extract_messages("whatsapp", limit=0)
            print(f"    → {len(messages)} mensajes extraídos")

            # Guardar en BD
            chat_id = store.upsert_chat(
                platform="whatsapp",
                browser="firefox",
                chat_key=title,
                title=title,
                phone="",
            )
            store.insert_messages(chat_id, "whatsapp", messages)
            # Guardar transcript
            transcript_path = (settings.transcripts_dir / f"{datetime.now().strftime('%Y%m%d')}-{title}.md")
            lines = [f"# {title}", f"\n- Extraído: {datetime.now().isoformat()}", f"- Mensajes: {len(messages)}\n"]
            for m in messages:
                sender = m.get("sender", "unknown")
                direction = m.get("direction", "in")
                text = m.get("text", "")
                t = m.get("time", "")
                lines.append(f"## {sender} ({direction}) {t}".strip())
                lines.append(f"{text}\n")
            transcript_path.write_text("\n".join(lines), encoding="utf-8")
            store.insert_transcript(platform="whatsapp", chat_key=title, file_path=str(transcript_path), message_count=len(messages))

            results.append({
                "title": title,
                "chat_id": chat_id,
                "message_count": len(messages),
                "unread": chat_info.get("unread", False),
                "transcript": str(transcript_path),
            })

            # Tomar screenshot
            ss = controller.save_screenshot(f"whatsapp-{title}-extracted")
            print(f"    → BD insertada, screenshot: {ss[:80] if ss else 'ninguno'}")

        except Exception as exc:
            print(f"    → ERROR: {exc}")
            try:
                controller.save_screenshot(f"whatsapp-{title}-error")
            except Exception:
                pass
            continue

    print("\n[4/5] Cerrando navegador...")
    controller.quit()

    print(f"\n[5/5] RESUMEN FINAL — {len(results)} chats procesados:")
    print("-" * 70)
    print(f"{'Número/Chat':<25} {'ID en BD':<10} {'Mensajes':<10} {'Unread':<8} {'Preview'}")
    print("-" * 70)
    for r in results:
        title_short = r["title"][:22]
        preview = r["transcript"][:40] if r.get("transcript") else ""
        print(f"{title_short:<25} {r['chat_id']:<10} {r['message_count']:<10} {'Sí' if r['unread'] else 'No':<8} {preview}")

    # Query BD para confirmar
    print("\n--- Verificación BD ---")
    import sqlite3
    conn = sqlite3.connect(settings.sqlite_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as total FROM chats")
    total_chats = cur.fetchone()["total"]
    cur.execute("SELECT COUNT(*) as total FROM messages")
    total_msgs = cur.fetchone()["total"]
    cur.execute("SELECT id, chat_key, title, phone FROM chats ORDER BY updated_at DESC LIMIT 20")
    rows = cur.fetchall()
    conn.close()
    print(f"Chats en BD: {total_chats}, Mensajes en BD: {total_msgs}")
    print("\nÚltimos 20 chats en la BD:")
    for r in rows:
        print(f"  ID={r['id']} | ChatKey='{r['chat_key']}' | Title='{r['title']}' | Phone={r['phone'] or 'N/A'}")

    return 0

if __name__ == "__main__":
    sys.exit(main())
