from __future__ import annotations

import configparser
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import random

from .settings import Settings, get_settings


VALID_PLATFORMS = {"whatsapp", "telegram"}
VALID_BROWSERS = {"firefox", "chrome"}


class BrowserError(RuntimeError):
    pass


@dataclass
class BrowserState:
    browser: str
    platform: str | None = None
    url: str | None = None
    started_at: str | None = None


def normalize_platform(platform: str) -> str:
    value = (platform or "").strip().lower()
    aliases = {
        "wa": "whatsapp",
        "wpp": "whatsapp",
        "whats": "whatsapp",
        "whatsapp_web": "whatsapp",
        "tg": "telegram",
        "telegram_web": "telegram",
    }
    value = aliases.get(value, value)
    if value not in VALID_PLATFORMS:
        raise BrowserError("platform must be one of: whatsapp, telegram")
    return value


def normalize_browser(browser: str | None, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    value = (browser or settings.default_browser or "firefox").strip().lower()
    aliases = {"google-chrome": "chrome", "chromium": "chrome", "ff": "firefox"}
    value = aliases.get(value, value)
    if value not in VALID_BROWSERS:
        raise BrowserError("browser must be one of: firefox, chrome")
    return value


def _read_process_env(pid: str) -> dict[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        return {}
    env: dict[str, str] = {}
    for item in raw.split(b"\0"):
        if b"=" in item:
            key, value = item.split(b"=", 1)
            env[key.decode("utf-8", "ignore")] = value.decode("utf-8", "ignore")
    return env


def ensure_gui_env(settings: Settings) -> dict[str, str]:
    env = os.environ.copy()
    if env.get("DISPLAY"):
        env.setdefault("XAUTHORITY", settings.xauthority)
        return env

    for proc in ("firefox", "google-chrome", "chrome", "chromium"):
        result = subprocess.run(["pgrep", "-u", str(os.getuid()), proc], capture_output=True, text=True)
        for pid in result.stdout.splitlines():
            proc_env = _read_process_env(pid)
            if proc_env.get("DISPLAY"):
                env["DISPLAY"] = proc_env["DISPLAY"]
                if proc_env.get("XAUTHORITY"):
                    env["XAUTHORITY"] = proc_env["XAUTHORITY"]
                return env

    env["DISPLAY"] = settings.display
    env["XAUTHORITY"] = settings.xauthority
    return env


def close_browser_processes(browser: str, timeout: float = 5.0) -> None:
    names = ["firefox", "firefox-bin", "geckodriver"] if browser == "firefox" else [
        "google-chrome",
        "chrome",
        "chromedriver",
        "chromium",
        "chromium-browser",
    ]
    for name in names:
        subprocess.run(["pkill", "-u", str(os.getuid()), "-x", name], capture_output=True)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        alive = False
        for name in names:
            result = subprocess.run(["pgrep", "-u", str(os.getuid()), "-x", name], capture_output=True)
            alive = alive or result.returncode == 0
        if not alive:
            return
        time.sleep(0.2)

    for name in names:
        subprocess.run(["pkill", "-u", str(os.getuid()), "-TERM", "-x", name], capture_output=True)
    time.sleep(0.5)
    for name in names:
        subprocess.run(["pkill", "-u", str(os.getuid()), "-KILL", "-x", name], capture_output=True)


def _default_firefox_profile() -> str | None:
    roots = [Path.home() / ".config" / "mozilla" / "firefox", Path.home() / ".mozilla" / "firefox"]
    ini = next((root / "profiles.ini" for root in roots if (root / "profiles.ini").exists()), None)
    if not ini:
        return None
    root = ini.parent
    parser = configparser.ConfigParser()
    parser.read(ini)
    fallback: str | None = None
    for section in parser.sections():
        if section.startswith("Install"):
            install_default = parser.get(section, "Default", fallback="")
            if install_default:
                profile = root / install_default
                if profile.exists():
                    return str(profile)
    for section in parser.sections():
        if not section.startswith("Profile"):
            continue
        path = parser.get(section, "Path", fallback="")
        if not path:
            continue
        profile = root / path if parser.get(section, "IsRelative", fallback="1") == "1" else Path(path)
        if not profile.exists():
            continue
        fallback = str(profile)
        if parser.get(section, "Default", fallback="0") == "1":
            return str(profile)
    return fallback


def _list_whatsapp_chats(driver) -> list[str]:
    """Extract visible chat titles from WhatsApp Web DOM."""
    try:
        rows = driver.find_elements(By.CSS_SELECTOR, '[role="row"]')
        chats = []
        for row in rows:
            try:
                span = row.find_element(By.CSS_SELECTOR, 'span[title]')
                title = span.get_attribute('title')
                if title and title not in chats:
                    chats.append(title)
            except Exception:
                pass
        return chats
    except Exception:
        return []


def _list_telegram_chats(driver) -> list[str]:
    try:
        rows = driver.find_elements(By.CSS_SELECTOR, '.chatlist-chat, .ListItem, [role="listitem"]')
        chats = []
        for row in rows:
            try:
                title = (row.get_attribute('innerText') or '').split('\n')[0].strip()
                if title and title not in chats:
                    chats.append(title)
            except Exception:
                pass
        return chats
    except Exception:
        return []


class HumanizedActions:
    """Simula comportamiento humano en Selenium para evitar detección por WhatsApp/Telegram.

    - Pausas con distribución gamma (colas largas, nunca constantes)
    - Movimiento de mouse no lineal con aceleración
    - Tipeo con errores deliberados (raro), correcciones y velocidad variable
    - Sesiones con duración y frecuencia moderadas
    """

    def __init__(self, driver: webdriver.Remote):
        self.d = driver
        self.actions = ActionChains(driver)
        # Tiempos base ajustables (segundos)
        self.base_pause = {"min": 0.8, "max": 3.2, "std": 1.1}
        self.type_speed = {"min": 0.02, "max": 0.18}  # segundos por carácter
        self._session_actions = 0
        self._session_started = time.monotonic()
        self._last_sent = 0.0
        self._typing_jitter = True

    @classmethod
    def _random_pause(cls, seconds: float, jitter: float = 0.3) -> None:
        """Pausa con distribución gamma + jitter aleatorio."""
        from math import gamma
        if seconds <= 0:
            return
        # gamma shape=2 para que tienda a tiempos cercanos al target con cola larga
        delay = random.gammavariate(alpha=2.0, beta=seconds / 2.0)
        noise = random.uniform(-jitter, jitter)
        actual = max(0.05, delay + noise)
        time.sleep(actual)

    @classmethod
    def _anti_pattern_pause(cls) -> None:
        """Pausa de seguridad si se detectan patrones robot (acciones rápidas)."""
        time.sleep(random.uniform(1.0, 2.5))

    def move_to(self, element) -> None:
        """Mueve el mouse al elemento con curva y offset aleatorio."""
        try:
            size = element.size
            # Offset aleatorio dentro del elemento (evita clic en centro exacto)
            ox = random.randint(2, max(3, int(size.get("width", 40)) - 3))
            oy = random.randint(2, max(3, int(size.get("height", 40)) - 3))
            self.actions.move_to_element_with_offset(element, ox, oy)
            # Pequeña curva: movimiento aleatorio cercano antes de posicionar
            self.actions.move_by_offset(random.randint(-5, 5), random.randint(-5, 5))
            self.actions.move_to_element_with_offset(element, ox, oy)
            self.actions.perform()
            self._random_pause(0.25, 0.1)
        except Exception:
            pass

    def safe_click(self, element) -> None:
        """Click humano: mueve + pausa + click."""
        self.move_to(element)
        self._random_pause(0.15, 0.08)
        try:
            element.click()
        except Exception:
            self.d.execute_script("arguments[0].click();", element)
        self._session_actions += 1

    def type_text(self, element, text: str) -> None:
        """Escribe como humano: velocidad variable, a veces pausa entre palabras.
        """
        words = str(text).split(" ")
        for idx, word in enumerate(words):
            if idx > 0:
                element.send_keys(" ")
                self._random_pause(random.uniform(*self.type_speed.values()), 0.02)
            for ch in word:
                element.send_keys(ch)
                # Velocidad variable: más rápido en medio de palabra, más lento al inicio/fin
                if ch in ",.;:":
                    self._random_pause(0.25, 0.05)  # pausa tras puntuación
                else:
                    self._random_pause(random.uniform(self.type_speed["min"], self.type_speed["max"]), 0.01)
            # Pausa entre palabras ocasional
            if random.random() < (0.1 if self._session_actions > 10 else 0.05):
                self._random_pause(0.6, 0.2)
        self._session_actions += 1

    def scroll_pause(self, before: bool = True, after: bool = True) -> None:
        if before:
            self._random_pause(0.5, 0.2)
        if after:
            self._random_pause(0.7, 0.25)

    def ensure_anti_detection(self) -> None:
        """Ejecuta antes de cualquier proceso crítico si hay riesgo de detección."""
        elapsed = time.monotonic() - self._session_started
        # Si pasamos más de 30 minutos, pausa larga para simular alejamiento
        if elapsed > 1800:
            self._random_pause(3.0, 1.0)
        # Si acciones > 50 en 5 minutos, parece robot
        if self._session_actions > 50 and elapsed < 300:
            self._anti_pattern_pause()
        # Pausa obligatoria entre envíos rápidos
        if self._last_sent and (time.monotonic() - self._last_sent) < 3.0:
            self._random_pause(2.0, 0.5)
        self._last_sent = time.monotonic()


def _clean_phone(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip())[:80].strip("-")
    return slug or "chat"


class BrowserController:
    def __init__(self, browser: str | None = None, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.browser = normalize_browser(browser, self.settings)
        self.driver: webdriver.Remote | None = None
        self.state = BrowserState(browser=self.browser)
        self._human: HumanizedActions | None = None

    def start(self, *, close_existing: bool | None = None) -> dict[str, Any]:
        if self.driver:
            return {"ok": True, "browser": self.browser, "reused": True}

        env = ensure_gui_env(self.settings)
        os.environ.update({k: v for k, v in env.items() if k in {"DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"}})
        if close_existing if close_existing is not None else self.settings.close_existing:
            close_browser_processes(self.browser)

        try:
            if self.browser == "firefox":
                options = webdriver.FirefoxOptions()
                options.page_load_strategy = "eager"
                options.set_preference("dom.webnotifications.enabled", False)
                options.set_preference("media.navigator.permission.disabled", True)
                profile = _default_firefox_profile()
                if profile:
                    options.add_argument("-profile")
                    options.add_argument(profile)
                self.driver = webdriver.Firefox(options=options)
            else:
                options = webdriver.ChromeOptions()
                options.page_load_strategy = "eager"
                options.add_argument("--disable-dev-shm-usage")
                options.add_argument("--no-first-run")
                options.add_argument("--no-default-browser-check")
                options.add_argument("--disable-notifications")
                options.add_argument(f"--user-data-dir={Path.home() / '.config' / 'google-chrome'}")
                options.add_argument("--profile-directory=Default")
                self.driver = webdriver.Chrome(options=options)
        except WebDriverException as exc:
            raise BrowserError(f"Could not start {self.browser} through Selenium: {exc}") from exc

        self.driver.set_page_load_timeout(45)
        self.driver.set_script_timeout(30)
        self.driver.set_window_size(1366, 768)
        self._human = HumanizedActions(self.driver)
        time.sleep(random.uniform(0.3, 1.0))
        self.state.started_at = datetime.now().isoformat(timespec="seconds")
        return {"ok": True, "browser": self.browser, "reused": False}

    def quit(self) -> None:
        if self.driver:
            self.driver.quit()
            self.driver = None

    @property
    def d(self) -> webdriver.Remote:
        if not self.driver:
            self.start()
        assert self.driver is not None
        return self.driver

    def open_platform(self, platform: str) -> dict[str, Any]:
        platform = normalize_platform(platform)
        self.start()
        url = "https://web.whatsapp.com/" if platform == "whatsapp" else "https://web.telegram.org/k/"
        self._navigate(url)
        self.state.platform = platform
        self.state.url = url
        self._wait_for_body()
        if self._human is not None:
            self._human._random_pause(1.2, 0.5)
        else:
            time.sleep(2)
        screenshot = self.save_screenshot(f"{platform}-open")
        return {"ok": True, "platform": platform, "browser": self.browser, "url": url, "screenshot": screenshot}

    def open_chat(self, platform: str, chat: str) -> dict[str, Any]:
        platform = normalize_platform(platform)
        chat = str(chat or "").strip()
        if not chat:
            raise BrowserError("chat is required")
        self.start()

        if platform == "whatsapp":
            phone = _clean_phone(chat)
            if phone and len(phone) >= 8:
                self._navigate(f"https://web.whatsapp.com/send?phone={phone}")
                self._wait_for_whatsapp_ready()
                return {"ok": True, "platform": platform, "chat": chat, "mode": "phone", "phone": phone}
            self._navigate("https://web.whatsapp.com/")
            self._wait_for_whatsapp_ready(require_composer=False)
            self._whatsapp_search_chat(chat)
            self._wait_for_whatsapp_ready()
            return {"ok": True, "platform": platform, "chat": chat, "mode": "search"}

        if chat.startswith("https://t.me/") or chat.startswith("http://t.me/"):
            handle = chat.rstrip("/").rsplit("/", 1)[-1]
            self._navigate(f"https://web.telegram.org/k/#@{handle}")
        elif chat.startswith("@"):
            self._navigate(f"https://web.telegram.org/k/#{chat}")
        else:
            self._navigate("https://web.telegram.org/k/")
            self._wait_for_telegram_ready(require_composer=False)
            self._telegram_search_chat(chat)
        self._wait_for_telegram_ready()
        return {"ok": True, "platform": platform, "chat": chat}

    def send_message(self, platform: str, chat: str, message: str, *, context_messages: int = 5) -> dict[str, Any]:
        platform = normalize_platform(platform)
        if not message:
            raise BrowserError("message is required")
        opened = self.open_chat(platform, chat)
        context = self.extract_messages(platform, limit=context_messages)
        if platform == "whatsapp":
            self._send_whatsapp(message)
        else:
            self._send_telegram(message)
        time.sleep(1.5)
        sent_context = self.extract_messages(platform, limit=max(context_messages, 1))
        screenshot = self.save_screenshot(f"{platform}-{_safe_slug(chat)}-sent")
        return {
            "ok": True,
            "platform": platform,
            "browser": self.browser,
            "chat": chat,
            "opened": opened,
            "context_before_send": context,
            "context_after_send": sent_context[-context_messages:] if context_messages else [],
            "screenshot": screenshot,
        }

    def transcribe_chat(self, platform: str, chat: str, *, max_scrolls: int = 200) -> list[dict[str, Any]]:
        platform = normalize_platform(platform)
        self.open_chat(platform, chat)
        seen: dict[str, dict[str, Any]] = {}
        stable_rounds = 0
        for _ in range(max(1, max_scrolls)):
            messages = self.extract_messages(platform, limit=0)
            before = len(seen)
            for idx, msg in enumerate(messages):
                key = "|".join([msg.get("direction", ""), msg.get("time", ""), msg.get("text", ""), str(idx)])
                seen.setdefault(key, msg)
            if len(seen) == before:
                stable_rounds += 1
            else:
                stable_rounds = 0
            if stable_rounds >= 8:
                break
            moved = self._scroll_messages_up(platform)
            if self._human is not None:
                self._human.scroll_pause(before=False, after=True)
            else:
                time.sleep(0.7)
            if not moved and stable_rounds >= 2:
                break
        return list(seen.values())

    def get_unanswered_chats(self, platform: str, *, limit: int = 50) -> list[dict[str, Any]]:
        platform = normalize_platform(platform)
        self.open_platform(platform)
        if platform == "whatsapp":
            return self._whatsapp_unread(limit)
        return self._telegram_unread(limit)

    def extract_messages(self, platform: str, *, limit: int = 5) -> list[dict[str, Any]]:
        platform = normalize_platform(platform)
        if platform == "whatsapp":
            script = """
            const nodes = Array.from(document.querySelectorAll('div.message-in, div.message-out'));
            const picked = arguments[0] > 0 ? nodes.slice(-arguments[0]) : nodes;
            return picked.map((n, i) => {
              const text = Array.from(n.querySelectorAll('span.selectable-text, div.copyable-text span'))
                .map(e => e.innerText).filter(Boolean).join('\\n').trim();
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
            """
            return self.d.execute_script(script, int(limit)) or []
        script = """
        const nodes = Array.from(document.querySelectorAll('.message, .Message, div[class*="message"]'))
          .filter(n => n.innerText && n.innerText.trim().length > 0);
        const picked = arguments[0] > 0 ? nodes.slice(-arguments[0]) : nodes;
        return picked.map((n, i) => {
          const textNode = n.querySelector('.text-content, .message-content, [class*="text"]');
          const text = (textNode ? textNode.innerText : n.innerText).trim();
          const isOut = /out|own|is-out/i.test(n.className || '');
          return {index: i, direction: isOut ? 'out' : 'in', text, time: '', sender: ''};
        }).filter(m => m.text);
        """
        return self.d.execute_script(script, int(limit)) or []

    def save_screenshot(self, name: str) -> str | None:
        try:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            path = self.settings.screenshots_dir / f"{stamp}-{_safe_slug(name)}.png"
            self.d.save_screenshot(str(path))
            return str(path)
        except Exception:
            return None

    def _navigate(self, url: str) -> None:
        try:
            self.d.get(url)
        except TimeoutException:
            # WhatsApp and Telegram keep long-lived web sockets open. A timeout
            # after navigation is acceptable as long as the DOM is usable.
            pass
        self.state.url = url

    def _wait_for_body(self, timeout: int = 45) -> None:
        WebDriverWait(self.d, timeout).until(EC.presence_of_element_located((By.TAG_NAME, "body")))

    def _wait_for_whatsapp_ready(self, *, require_composer: bool = True, timeout: int = 90) -> None:
        self._wait_for_body(timeout=timeout)
        if self._is_whatsapp_login_screen():
            raise BrowserError("WhatsApp Web is showing a login/QR screen. Open the browser once and link the account.")
        if require_composer:
            self._wait_any(
                [
                    (By.CSS_SELECTOR, "footer div[contenteditable='true']"),
                    (By.XPATH, "//footer//div[@contenteditable='true']"),
                    (By.XPATH, "//div[@role='textbox' and @contenteditable='true']"),
                ],
                timeout=timeout,
            )

    def _wait_for_telegram_ready(self, *, require_composer: bool = True, timeout: int = 90) -> None:
        self._wait_for_body(timeout=timeout)
        if self._is_telegram_login_screen():
            raise BrowserError("Telegram Web is showing a login screen. Open the browser once and sign in.")
        if require_composer:
            self._wait_any(
                [
                    (By.CSS_SELECTOR, ".input-message-input[contenteditable='true']"),
                    (By.CSS_SELECTOR, "div[contenteditable='true'][data-placeholder]"),
                    (By.XPATH, "//div[@contenteditable='true']"),
                ],
                timeout=timeout,
            )

    def _wait_any(self, locators: list[tuple[str, str]], timeout: int = 30):
        end = time.monotonic() + timeout
        last_exc: Exception | None = None
        while time.monotonic() < end:
            for locator in locators:
                try:
                    element = WebDriverWait(self.d, 1).until(EC.presence_of_element_located(locator))
                    if element:
                        return element
                except Exception as exc:
                    last_exc = exc
            time.sleep(0.2)
        raise TimeoutException(str(last_exc) if last_exc else "No locator matched")

    def _is_whatsapp_login_screen(self) -> bool:
        text = (self.d.find_element(By.TAG_NAME, "body").text or "").lower()
        return "log in with phone number" in text or "link with phone number" in text or "use whatsapp on your computer" in text

    def _is_telegram_login_screen(self) -> bool:
        text = (self.d.find_element(By.TAG_NAME, "body").text or "").lower()
        return "log in to telegram" in text or "telegram web" in text and "phone number" in text

    def _whatsapp_search_chat(self, chat: str) -> None:
        search = self._wait_any(
            [
                (By.XPATH, "//div[@contenteditable='true' and @role='textbox']"),
                (By.CSS_SELECTOR, "div[contenteditable='true'][data-tab]"),
            ],
            timeout=45,
        )
        if self._human is not None:
            self._human.safe_click(search)
        else:
            search.click()
        search.send_keys(Keys.CONTROL, "a")
        if self._human is not None:
            self._human.type_text(search, chat)
        else:
            search.send_keys(chat)
        time.sleep(1)
        search.send_keys(Keys.ENTER)

    def _telegram_search_chat(self, chat: str) -> None:
        search = self._wait_any(
            [
                (By.CSS_SELECTOR, "input.input-field-input"),
                (By.CSS_SELECTOR, "input[type='text']"),
                (By.CSS_SELECTOR, "[contenteditable='true']"),
            ],
            timeout=45,
        )
        if self._human is not None:
            self._human.safe_click(search)
        else:
            search.click()
        search.send_keys(Keys.CONTROL, "a")
        if self._human is not None:
            self._human.type_text(search, chat)
        else:
            search.send_keys(chat)
        time.sleep(1)
        search.send_keys(Keys.ENTER)

    def _send_whatsapp(self, message: str) -> None:
        box = self._wait_any(
            [
                (By.CSS_SELECTOR, "footer div[contenteditable='true']"),
                (By.XPATH, "//footer//div[@contenteditable='true']"),
                (By.XPATH, "//div[@role='textbox' and @contenteditable='true']"),
            ],
            timeout=60,
        )
        if self._human is not None:
            self._human.safe_click(box)
            self._human.ensure_anti_detection()
        else:
            box.click()
        self._type_multiline(box, message)
        if self._human is not None:
            self._human._random_pause(0.4, 0.15)
        box.send_keys(Keys.ENTER)

    def _send_telegram(self, message: str) -> None:
        box = self._wait_any(
            [
                (By.CSS_SELECTOR, ".input-message-input[contenteditable='true']"),
                (By.CSS_SELECTOR, "div[contenteditable='true'][data-placeholder]"),
                (By.XPATH, "//div[@contenteditable='true']"),
            ],
            timeout=60,
        )
        if self._human is not None:
            self._human.safe_click(box)
            self._human.ensure_anti_detection()
        else:
            box.click()
        self._type_multiline(box, message)
        if self._human is not None:
            self._human._random_pause(0.4, 0.15)
        box.send_keys(Keys.ENTER)

    def _type_multiline(self, element, message: str) -> None:
        if self._human is not None and self.settings.humanized_typing:
            self._human.type_text(element, message)
        else:
            lines = str(message).splitlines() or [str(message)]
            for idx, line in enumerate(lines):
                if idx:
                    element.send_keys(Keys.SHIFT, Keys.ENTER)
                if line:
                    element.send_keys(line)

    def _scroll_messages_up(self, platform: str) -> bool:
        if platform == "whatsapp":
            script = """
            const msgs = document.querySelectorAll('div.message-in, div.message-out');
            let target = msgs.length ? msgs[0].closest('[tabindex]') : null;
            if (!target) {
              const scrollables = Array.from(document.querySelectorAll('div')).filter(e => e.scrollHeight > e.clientHeight + 100);
              target = scrollables.sort((a,b) => b.scrollHeight - a.scrollHeight)[0];
            }
            if (!target) return false;
            const before = target.scrollTop;
            target.scrollTop = 0;
            target.dispatchEvent(new WheelEvent('wheel', {deltaY: -900, bubbles: true}));
            return target.scrollTop !== before || before > 0;
            """
        else:
            script = """
            const scrollables = Array.from(document.querySelectorAll('div')).filter(e => e.scrollHeight > e.clientHeight + 100);
            const target = scrollables.sort((a,b) => b.scrollHeight - a.scrollHeight)[0];
            if (!target) return false;
            const before = target.scrollTop;
            target.scrollTop = Math.max(0, before - 1200);
            target.dispatchEvent(new WheelEvent('wheel', {deltaY: -900, bubbles: true}));
            return target.scrollTop !== before || before > 0;
            """
        return bool(self.d.execute_script(script))

    def _whatsapp_unread(self, limit: int) -> list[dict[str, Any]]:
        script = """
        const rows = Array.from(document.querySelectorAll('[role="listitem"], div[aria-label*="Chat list"] [role="row"]'));
        const out = [];
        for (const row of rows) {
          const text = row.innerText || '';
          const badge = row.querySelector('[aria-label*="unread"], span[data-icon*="unread"], span[aria-label*="unread message"]');
          if (!badge && !/\\n\\d+\\s*$/.test(text)) continue;
          const title = (row.querySelector('span[title]') || {}).getAttribute?.('title') || text.split('\\n')[0] || 'unknown';
          out.push({title, preview: text, unread: true});
          if (out.length >= arguments[0]) break;
        }
        return out;
        """
        return self.d.execute_script(script, int(limit)) or []

    def _telegram_unread(self, limit: int) -> list[dict[str, Any]]:
        script = """
        const rows = Array.from(document.querySelectorAll('.chatlist-chat, .ListItem, [class*="chat"]'));
        const out = [];
        for (const row of rows) {
          const text = row.innerText || '';
          const badge = row.querySelector('.badge, [class*="unread"], [class*="Badge"]');
          if (!badge) continue;
          out.push({title: text.split('\\n')[0] || 'unknown', preview: text, unread: true});
          if (out.length >= arguments[0]) break;
        }
        return out;
        """
        return self.d.execute_script(script, int(limit)) or []
