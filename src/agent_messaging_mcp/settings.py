from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    project_root: Path = PROJECT_ROOT
    data_dir: Path = PROJECT_ROOT / "data"
    transcripts_dir: Path = PROJECT_ROOT / "transcripts"
    screenshots_dir: Path = Path(
        os.environ.get(
            "AGENT_MESSAGING_SCREENSHOTS_DIR",
            "/home/cristian/brain-sync/07-Images/Screenshots/Shared/agent-messaging-mcp",
        )
    )
    default_browser: str = os.environ.get("AGENT_MESSAGING_BROWSER", "firefox")
    db_dsn: str = os.environ.get(
        "AGENT_MESSAGING_DB_DSN",
        "postgresql://agentmsg:agentmsg@127.0.0.1:55432/agent_messaging",
    )
    sqlite_path: Path = Path(
        os.environ.get(
            "AGENT_MESSAGING_SQLITE_PATH",
            str(PROJECT_ROOT / "data" / "messaging.sqlite3"),
        )
    )
    sqlite_fallback: bool = os.environ.get("AGENT_MESSAGING_SQLITE_FALLBACK", "1") != "0"
    display: str = os.environ.get("DISPLAY", ":0")
    xauthority: str = os.environ.get("XAUTHORITY", str(Path.home() / ".Xauthority"))
    close_existing: bool = os.environ.get("AGENT_MESSAGING_CLOSE_EXISTING", "1") != "0"
    humanized_typing: bool = os.environ.get("AGENT_MESSAGING_HUMANIZED", "1") != "0"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.transcripts_dir.mkdir(parents=True, exist_ok=True)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
