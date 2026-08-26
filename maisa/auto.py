"""Automated OP (Osuuspankki) login for Maisa — user ID only, app approval.

Navigates Maisa -> Suomi.fi -> OP, fills ONLY the OP user ID (a username-tier
identifier read from the macOS Keychain / env / a local file, so the value never
passes through the AI), submits, and waits for the user to approve in the
OP-mobiili app. It never enters a password, PIN, key code, or one-time code —
the authenticating step is always the user's in-app approval.

Where the user ID is read from (first hit wins), all local to this machine:
  1. env  MAISA_OP_USERID
  2. Keychain  `security find-generic-password -s maisa-op-userid -w`
  3. file  <config>/op-userid  (0600)

Store it once, e.g. (prompts for the value; -A lets the tool read it headless):
  security add-generic-password -s maisa-op-userid -a maisa -A -w
"""

from __future__ import annotations

import json
import os
import subprocess

from . import auth
from .session import CONFIG_DIR, save

SAML_URL = "https://www.maisa.fi/maisa/Authentication/Saml/Login?idp=TUNNISTUS&forceAuthn=False"
OP_TILE_TEXTS = ["Osuuspankki", "OP Bank Group", "OP Financial", "OP-Pohjola"]
OP_USERID_SEL = "#auth-device-userid-mobilekey"
OP_MOBILEKEY_RADIO = "#auth-device-radio-mobilekey"
OP_SUBMIT_SEL = "#auth-device-submit-mobilekey"


def read_userid() -> str | None:
    v = os.environ.get("MAISA_OP_USERID")
    if v:
        return v.strip()
    try:
        out = subprocess.check_output(
            ["security", "find-generic-password", "-s", "maisa-op-userid", "-w"],
            text=True, stderr=subprocess.DEVNULL, timeout=10).strip()
        if out:
            return out
    except Exception:
        pass
    f = CONFIG_DIR / "op-userid"
    if f.exists():
        val = f.read_text().strip()
        if val:
            return val
    return None


def auto_login(headless: bool = True, timeout_s: int = 240):
    from playwright.sync_api import sync_playwright

    userid = read_userid()
    if not userid:
        raise SystemExit(
            "No OP user ID configured. Store it once (prompts for the value):\n"
            "  security add-generic-password -s maisa-op-userid -a maisa -A -w\n"
            f"or write it to {CONFIG_DIR / 'op-userid'} (chmod 600).")

    auth._ensure_dir()
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(
            viewport={"width": 420, "height": 900},
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"))
        page = context.new_page()

        page.goto(SAML_URL, wait_until="domcontentloaded", timeout=30000)

        # Suomi.fi discovery -> OP tile.
        clicked = False
        for txt in OP_TILE_TEXTS:
            try:
                el = page.get_by_text(txt, exact=False).first
                el.scroll_into_view_if_needed(timeout=3000)
                el.click(timeout=5000)
                clicked = True
                break
            except Exception:
                continue
        if not clicked:
            raise SystemExit("Could not find the OP tile on the Suomi.fi page.")

        # OP login page: mobile-key method + user ID.
        page.wait_for_selector(OP_USERID_SEL, timeout=25000)
        try:
            page.check(OP_MOBILEKEY_RADIO, timeout=3000)
        except Exception:
            pass  # mobile key is the default
        page.fill(OP_USERID_SEL, userid)
        page.click(OP_SUBMIT_SEL)

        # OP has now pushed an approval request to the OP-mobiili app.
        print("APPROVE_NOW :: OP user ID submitted — approve the login in your OP-mobiili app "
              "on your phone.", flush=True)

        page.wait_for_url("**/maisa/Home/**", timeout=timeout_s * 1000)
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        state = context.storage_state()
        sess = auth._session_from_state(state)
        save(sess)
        auth.STATE_PATH.write_text(json.dumps(state))
        browser.close()

    print("AUTO_LOGIN_DONE :: session captured", flush=True)
    return sess
