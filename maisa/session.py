"""Persistent session storage for the Maisa CLI.

A Maisa session is whatever the browser held after a successful strong-auth
login: the portal cookies (the primary auth mechanism for Epic MyChart web),
plus any CSRF token and bearer/API tokens the SPA uses, plus metadata (host,
capture time, observed expiry). We store it on disk so the CLI can replay the
session without re-driving a browser, until the server expires it.

The store is a single JSON file at ~/.maisa/session.json with 0600 perms.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir

CONFIG_DIR = Path(user_config_dir("maisa", appauthor=False))
SESSION_PATH = CONFIG_DIR / "session.json"

# The Maisa portal host. Overridable via env for other Apotti deployments.
DEFAULT_HOST = os.environ.get("MAISA_HOST", "www.maisa.fi")
# Base path of the MyChart app inside the host.
DEFAULT_BASE = os.environ.get("MAISA_BASE", "/maisa")


@dataclass
class Session:
    host: str = DEFAULT_HOST
    base: str = DEFAULT_BASE
    # name -> value. These are replayed as the Cookie header. Includes any
    # httpOnly cookies harvested from the browser network layer.
    cookies: dict[str, str] = field(default_factory=dict)
    # Anti-CSRF token the SPA sends (Epic uses __RequestVerificationToken /
    # a "verificationToken" header on state-changing calls).
    csrf_token: str | None = None
    # Bearer/access token if the deployment uses token auth for its API.
    access_token: str | None = None
    # Extra headers the SPA sends that the server requires (populated at capture).
    extra_headers: dict[str, str] = field(default_factory=dict)
    # Bookkeeping.
    captured_at: float = 0.0
    last_ok_at: float = 0.0
    # Free-form notes captured during reverse engineering (e.g. user display name).
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def origin(self) -> str:
        return f"https://{self.host}"

    @property
    def app_url(self) -> str:
        return f"{self.origin}{self.base}"

    def age_seconds(self) -> float:
        return time.time() - self.captured_at if self.captured_at else float("inf")


def _ensure_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass


def save(session: Session) -> Path:
    _ensure_dir()
    tmp = SESSION_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(session), indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(SESSION_PATH)
    return SESSION_PATH


def load() -> Session | None:
    if not SESSION_PATH.exists():
        return None
    data = json.loads(SESSION_PATH.read_text())
    # Tolerate older/newer files by only passing known fields.
    known = {f for f in Session.__dataclass_fields__}
    return Session(**{k: v for k, v in data.items() if k in known})


def load_or_die() -> Session:
    s = load()
    if s is None or not s.cookies:
        raise SystemExit(
            "No Maisa session found. Run `maisa login` and complete the bank "
            "authentication in the browser, or `maisa session import <file>`."
        )
    return s


def clear() -> None:
    if SESSION_PATH.exists():
        SESSION_PATH.unlink()
