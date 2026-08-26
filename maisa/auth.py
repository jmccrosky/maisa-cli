"""Login + session capture via Playwright.

Finnish health-data access requires strong authentication (Suomi.fi bank /
certificate). We can't script that, and we shouldn't touch the user's banking
credentials. So `login()` opens a real browser, lets the *user* complete the
bank flow, then captures the resulting MyChart session locally:

  * ~/.maisa/state.json  -- Playwright storage_state (httpOnly cookies included)
  * ~/.maisa/session.json -- the cookies as a flat dict for httpx replay

Playwright reads the httpOnly cookies at the browser level (not page JS), so
this works where document.cookie cannot. Everything stays on the user's disk.

An optional discovery pass clicks through the portal's sections while logging
every non-static request+response to a JSONL file, which is how the API surface
is reverse-engineered.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from .session import CONFIG_DIR, DEFAULT_BASE, DEFAULT_HOST, Session, save

STATE_PATH = CONFIG_DIR / "state.json"
DISCOVERY_PATH = CONFIG_DIR / "last-discovery.jsonl"

LOGIN_URL = f"https://{DEFAULT_HOST}{DEFAULT_BASE}/Authentication/Login"
HOME_URL = f"https://{DEFAULT_HOST}{DEFAULT_BASE}/Home/"

_STATIC_RE = (".css", ".js", ".png", ".svg", ".jpg", ".jpeg", ".gif",
              ".woff", ".woff2", ".ttf", ".ico", ".map")


def _ensure_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass


def _session_from_state(state: dict) -> Session:
    cookies = {}
    for c in state.get("cookies", []):
        if "maisa.fi" in c.get("domain", ""):
            cookies[c["name"]] = c["value"]
    return Session(
        host=DEFAULT_HOST,
        base=DEFAULT_BASE,
        cookies=cookies,
        captured_at=time.time(),
    )


def save_state(context) -> Session:
    _ensure_dir()
    state = context.storage_state()
    STATE_PATH.write_text(json.dumps(state))
    os.chmod(STATE_PATH, 0o600)
    sess = _session_from_state(state)
    save(sess)
    return sess


def _is_static(url: str) -> bool:
    path = url.split("?")[0].lower()
    return path.endswith(_STATIC_RE) or any(
        seg in path for seg in ("/styles/", "/scripts/", "/bundles/", "/images/", "/fonts/")
    )


def attach_logger(context, out_path: Path):
    fh = out_path.open("w")

    def on_response(resp):
        try:
            url = resp.url
            if _is_static(url):
                return
            req = resp.request
            rec = {
                "method": req.method,
                "url": url,
                "status": resp.status,
                "ctype": resp.headers.get("content-type", ""),
            }
            # capture JSON bodies (truncated) — that's the API schema
            if "application/json" in rec["ctype"] and req.method in ("GET", "POST"):
                try:
                    body = resp.text()
                    rec["body"] = body[:6000]
                except Exception:
                    pass
                if req.method == "POST":
                    try:
                        rec["req_body"] = (req.post_data or "")[:2000]
                    except Exception:
                        pass
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
        except Exception:
            pass

    context.on("response", on_response)
    return fh


def _discover(page) -> None:
    """Best-effort: visit each portal section to trigger its API calls."""
    tiles = ["Messages", "Test Results", "Events", "Letters",
             "Book Appointment", "Frequently Asked"]
    for label in tiles:
        try:
            page.goto(HOME_URL, wait_until="networkidle", timeout=20000)
            el = page.get_by_text(label, exact=False).first
            el.click(timeout=5000)
            page.wait_for_load_state("networkidle", timeout=15000)
            time.sleep(1.0)
            # open the first item in a list view, if any, for detail endpoints
            try:
                page.locator("[role=listitem], li, .conversationListItem").first.click(timeout=3000)
                page.wait_for_load_state("networkidle", timeout=8000)
                time.sleep(0.8)
            except Exception:
                pass
        except Exception:
            continue


def login(headless: bool = False, discover: bool = True, timeout_s: int = 300) -> Session:
    from playwright.sync_api import sync_playwright

    _ensure_dir()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context()
        fh = attach_logger(context, DISCOVERY_PATH) if discover else None
        page = context.new_page()
        print(f"Opening {LOGIN_URL}\n>> Complete the bank / strong authentication in the browser window.")
        page.goto(LOGIN_URL)
        # Wait until we land on the authenticated Home page.
        page.wait_for_url("**/maisa/Home/**", timeout=timeout_s * 1000)
        print("Login detected. Capturing session…")
        page.wait_for_load_state("networkidle", timeout=20000)
        if discover:
            print("Mapping API endpoints (visiting sections)…")
            _discover(page)
        sess = save_state(context)
        if fh:
            fh.close()
        browser.close()
    print(f"Saved session ({len(sess.cookies)} cookies) to {CONFIG_DIR}.")
    if discover:
        print(f"Discovery log: {DISCOVERY_PATH}")
    return sess


def tailscale_ip() -> str | None:
    """Best-effort Tailscale IPv4 of this machine (for the phone-facing URL)."""
    for cmd in ("tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale"):
        exe = shutil.which(cmd) or (cmd if os.path.exists(cmd) else None)
        if not exe:
            continue
        try:
            out = subprocess.check_output([exe, "ip", "-4"], text=True, timeout=5).strip()
            return out.splitlines()[0].strip() if out else None
        except Exception:
            continue
    return None


def remote_login(timeout_s: int = 900, novnc_port: int | None = None) -> Session:
    """Headful login for a remote (unattended) Mac, viewed from a phone.

    The browser opens on this machine's desktop; the user drives it from their
    phone over Tailscale via macOS Screen Sharing (a VNC app, or a noVNC link if
    `novnc_port` is given and websockify is running). We print a clear, easily
    relayable READY line so an orchestrating agent can forward it over WhatsApp,
    then block until login completes and the session is captured.

    Requirements handled elsewhere (see REMOTE-LOGIN.md): Screen Sharing enabled,
    Tailscale up, the Mac logged in and awake.
    """
    from playwright.sync_api import sync_playwright

    _ensure_dir()
    ip = tailscale_ip()
    # Keep the Mac awake for the duration of the login.
    caff = None
    if shutil.which("caffeinate"):
        caff = subprocess.Popen(["caffeinate", "-dimsu"])
    try:
        # Emit the connection instruction FIRST so the agent can relay it.
        if novnc_port and ip:
            where = f"http://{ip}:{novnc_port}/vnc.html?autoconnect=1&resize=scale"
            how = f"Open this link on your phone (over Tailscale): {where}"
        elif ip:
            where = f"{ip}:5900"
            how = f"Open your VNC viewer app and connect to {where} (over Tailscale)."
        else:
            where = "this Mac's screen"
            how = "Connect to this Mac's screen via Screen Sharing over Tailscale."
        print(f"REMOTE_LOGIN_READY :: {how}", flush=True)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, args=["--start-maximized"])
            context = browser.new_context(no_viewport=True)
            page = context.new_page()
            page.goto(LOGIN_URL)
            print(">> Waiting for you to complete the bank login on your phone…", flush=True)
            page.wait_for_url("**/maisa/Home/**", timeout=timeout_s * 1000)
            page.wait_for_load_state("networkidle", timeout=20000)
            sess = save_state(context)
            browser.close()
        print("REMOTE_LOGIN_DONE :: session captured", flush=True)
        return sess
    finally:
        if caff:
            caff.terminate()


def load_state_cookies() -> dict[str, str]:
    if not STATE_PATH.exists():
        return {}
    state = json.loads(STATE_PATH.read_text())
    return {c["name"]: c["value"] for c in state.get("cookies", []) if "maisa.fi" in c.get("domain", "")}
