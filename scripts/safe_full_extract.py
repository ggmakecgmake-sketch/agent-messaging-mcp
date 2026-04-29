#!/usr/bin/env python3
"""
Extractor masivo seguro: clona el perfil de Firefox, elimina locks,
y extrae TODOS los chats de WhatsApp Web con historial completo en SQLite.
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from agent_messaging_mcp.browser import BrowserController, close_browser_processes
from agent_messaging_mcp.db import MessageStore, utc_now
from agent_messaging_mcp.settings import get_settings

# ── Paths ──
ORIG_PROFILE = Path.home() / ".config/mozilla/firefox/u4h1soqk.default-release"
CLONE_DIR    = Path.home() / ".cache/messaging-mcp-firefox-profile"
LOCK_FILES   = [".parentlock", "lock", "places.sqlite-wal", "cookies.sqlite-wal"]

def clone_profile() -> str:
    """Copiar perfil de Firefox a ruta temporal, eliminar locks."""
    print(f"  Clonando perfil a {CLONE_DIR}...")
    if CLONE_DIR.exists():
        shutil.rmtree(CLONE_DIR, ignore_errors=True)
    shutil.copytree(ORIG_PROFILE, CLONE_DIR)
    for lock in LOCK_FILES:
        (CLONE_DIR / lock).unlink(missing_ok=True)
    return str(CLONE_DIR)

def main() -> int:
    settings = get_settings()
    settings.ensure_dirs()
    store = MessageStore(settings)

    # ── 0. Si ya hay chats en la BD, reportarlos ──
    import sqlite3
    conn = sqlite3.connect(settings.sqlite_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as total FROM chats")
    total_chats = cur.fetchone()["total"]
    cur.execute("SELECT COUNT(*) as total FROM messages")
    total_msgs = cur.fetchone()["total"]
    conn.close()

    if total_chats > 0:
        print(f"\n📦 BD ya tiene {total_chats} chats y {total_msgs} mensajes.")
        print("Ejecutando solo resumen de la BD existente...\n")
        return report_existing()

    print("=" * 60)
    print(" EXTRACTOR COMPLETO — WhatsApp Web → SQLite")
    print("=" * 60)

    # ── 1. Cerrar Firefox existente ──
    print("\n[1/5] Cerrando Firefox existente...")
    close_browser_processes("firefox", timeout=8.0)
    time.sleep(1.5)

    # ── 2. Copiar perfil limpio ──
    print("[2/5] Clonando perfil de Firefox (sin locks)...")
    cloned_profile = clone_profile()
    print(f"  → Perfil listo: {cloned_profile}")

    # ── 3. Abrir Selenium con perfil clonado ──
    print("[3/5] Abriendo WhatsApp Web con Selenium...")
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait

    options = webdriver.FirefoxOptions()
    options.page_load_strategy = "eager"
    options.set_preference("dom.webnotifications.enabled", False)
    options.add_argument("-profile")
    options.add_argument(cloned_profile)

    os.environ.setdefault("DISPLAY", ":0")
    driver = webdriver.Firefox(options=options)
    driver.set_page_load_timeout(45)
    driver.set_script_timeout(30)
    driver.set_window_size(1366, 768)
    time.sleep(0.5)

    print("  → Firefox abierto, navegando a WhatsApp Web...")
    try:
        driver.get("https://web.whatsapp.com/")
    except Exception:
        pass

    # Esperar que cargue
    time.sleep(4)
    print("  → Esperando sesión activa...")
    WebDriverWait(driver, 60).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    time.sleep(4)

    # Verificar si vemos el chat list o el QR
    page_text = driver.find_element(By.TAG_NAME, "body").text.lower()
    if "log in" in page_text or "link with phone" in page_text or "qr" in page_text:
        print("\n❌ ERROR: WhatsApp Web muestra pantalla de login/QR.")
        print("   Escaneá el QR con tu celular primero.")
        driver.quit()
        return 1

    print("  → Sesión OK. Escaneando chats...")

    # Screenshot
    ss_path = settings.screenshots_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-wa-open.png"
    ss_path.parent.mkdir(parents=True, exist_ok=True)
    driver.save_screenshot(str(ss_path))
    print(f"  → Screenshot: {ss_path}")

    # ── 4. Extraer lista de chats ──
    print("\n[4/5] Obteniendo todos los chats...")
    chats = driver.execute_script("""
    const contacts = [];
    const items = document.querySelectorAll('[id="pane-side"] [role="listitem"], div[role="listitem"]');
    for (let item of items) {
      const titleEl = item.querySelector('span[title]');
      if (!titleEl) continue;
      const title = titleEl.getAttribute('title');
      const previewEls = item.querySelectorAll('span[dir], div[data-testid="conversation-info"]');
      const preview = Array.from(item.querySelectorAll('*')).map(e => e.innerText).slice(0,3).join(' ');
      const unreadBadge = item.querySelector('[aria-label*="unread"], span[data-icon*="unread"]');
      contacts.push({title: title || '', preview: preview.substring(0,80), unread: !!unreadBadge});
    }
    return contacts;
    """)
    print(f"  → {len(chats)} chats encontrados")
    if not chats:
        print("ERROR: No se detectaron chats. Verificá que WhatsApp está abierto y cargado. Saliendo.")
        driver.quit()
        return 1

    # ── 5. Extraer mensajes de cada chat ──
    print(f"\n[5/5] Extrayendo mensajes de {len(chats)} chats...")
    results = []
    for idx, chat_info in enumerate(chats, 1):
        title = str(chat_info.get("title", "")).strip()
        if not title:
            continue
        print(f"\n  [{idx}/{len(chats)}] Chat: '{title}'")
        try:
            # Buscar chat en barra lateral
            search_input = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.XPATH, "//div[@contenteditable='true']"))
            )
            search_input.click()
            time.sleep(0.2)
            search_input.send_keys(Keys.CONTROL, "a")
            search_input.send_keys(Keys.BACKSPACE)
            time.sleep(0.2)
            search_input.send_keys(title)
            time.sleep(1.5)
            search_input.send_keys(Keys.ENTER)
            time.sleep(2.0)

            # Extraer mensajes
            msgs = driver.execute_script("""
            const nodes = Array.from(document.querySelectorAll('div.message-in, div.message-out'));
            return nodes.map((n, i) => {
              const spans = Array.from(n.querySelectorAll('span.selectable-text, div.copyable-text span'));
              const text = spans.map(e => e.innerText).filter(Boolean).join('\\n').trim();
              const pre = n.querySelector('[data-pre-plain-text]');
              const meta = pre ? pre.getAttribute('data-pre-plain-text') : '';
              return {
                index: i,
                direction: n.classList.contains('message-out') ? 'out' : 'in',
                text,
                time: meta,
                sender: meta ? meta.replace(/^\\[[^\\]]+\\]\\s*/, '').replace(/:\\s*$/, '') : ''
              };
            }).filter(m => m.text || m.time);
            """)
            print(f"    → {len(msgs)} mensajes extraídos")

            # Guardar en BD
            chat_id = store.upsert_chat(
                platform="whatsapp", browser="firefox", chat_key=title, title=title, phone="",
            )
            store.insert_messages(chat_id, "whatsapp", msgs)

            # Guardar transcript
            transcript_path = settings.transcripts_dir / f"{datetime.now().strftime('%Y%m%d')}_{_safe_slug(title)}.md"
            lines = [f"# {title}",f"- Extraído: {datetime.now().isoformat()}",f"- Mensajes: {len(msgs)}\n"]
            for m in msgs:
                sender = m.get("sender", "unknown")
                direction = m.get("direction", "in")
                text = m.get("text", "")
                t = m.get("time", "")
                lines.append(f"## {sender} ({direction}) {t}".strip())
                lines.append(f"{text}\n")
            transcript_path.write_text("\n".join(lines), encoding="utf-8")
            store.insert_transcript(platform="whatsapp", chat_key=title, file_path=str(transcript_path), message_count=len(msgs))

            results.append({"title": title, "chat_id": chat_id, "message_count": len(msgs), "unread": chat_info.get("unread", False), "transcript": str(transcript_path)})

            # Screenshot
            ss_chat = settings.screenshots_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-wa-{idx}-extracted.png"
            driver.save_screenshot(str(ss_chat))
            print(f"    → Guardado en BD (ID={chat_id}), transcript={transcript_path.name}")

        except Exception as exc:
            print(f"    → ERROR: {exc}")
            try:
                driver.save_screenshot(str(settings.screenshots_dir / f"error-{idx}.png"))
            except Exception:
                pass

    print("\n" + "=" * 60)
    print(" RESUMEN FINAL")
    print("=" * 60)
    print(f"\n{'Chat':<25} {'ID en BD':<10} {'Mensajes':<10} {'Unread'}")
    print("-" * 55)
    for r in results:
        print(f"{r['title'][:23]:<25} {r['chat_id']:<10} {r['message_count']:<10} {'Sí' if r['unread'] else 'No'}")

    print(f"\n📊 Total: {len(results)} chats extraídos → SQLite: {settings.sqlite_path}")
    print(f"📁 Transcripts: {settings.transcripts_dir}")
    print(f"📷 Screenshots: {settings.screenshots_dir}")

    driver.quit()
    return 0

def report_existing() -> int:
    """Reporte de la BD existente."""
    import sqlite3
    settings = get_settings()
    conn = sqlite3.connect(settings.sqlite_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("""
    SELECT c.id, c.chat_key, c.title, c.phone, COUNT(m.id) as msg_count
    FROM chats c LEFT JOIN messages m ON c.id = m.chat_id
    GROUP BY c.id
    ORDER BY c.id
    """)
    rows = cur.fetchall()
    conn.close()

    print("-" * 70)
    print(f"{'Chat':<25} {'ID en BD':<10} {'Mensajes':<10} {'Phone'}")
    print("-" * 70)
    for r in rows:
        title = r['title'] or r['chat_key'] or 'N/A'
        phone = r['phone'] or 'N/A'
        msg_count = r['msg_count'] or 0
        print(f"{title[:23]:<25} {r['id']:<10} {msg_count:<10} {phone}")

    total_chats = sum(1 for _ in rows)
    total_msgs = sum(r['msg_count'] for r in rows)
    print("-" * 70)
    print(f"Total: {total_chats} chats | {total_msgs} mensajes")
    print(f"BD: {settings.sqlite_path}")
    return 0

def _safe_slug(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value)[:60].strip("-") or "chat"

if __name__ == "__main__":
    sys.exit(main())
