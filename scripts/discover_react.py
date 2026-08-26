"""Headed discovery of the React-app JSON endpoints (messages, results, etc.).

Reuses the saved session (no login). Headed Chromium avoids the headless bot
block. Visits each React feature, opens the first item, and for messaging opens
the compose form, logging all endpoints to CONFIG_DIR/react-discovery.jsonl.
"""
import time
from playwright.sync_api import sync_playwright

from maisa.auth import STATE_PATH, attach_logger
from maisa.session import CONFIG_DIR, DEFAULT_BASE, DEFAULT_HOST

OUT = CONFIG_DIR / "react-discovery.jsonl"
ORIGIN = f"https://{DEFAULT_HOST}{DEFAULT_BASE}"


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(storage_state=str(STATE_PATH))
        fh = attach_logger(context, OUT)
        page = context.new_page()

        def visit(route, wait=3.0):
            print("visit", route)
            page.goto(f"{ORIGIN}/{route}", wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            time.sleep(wait)

        # Messages: list -> open first conversation -> open compose
        visit("app/communication-center", 4)
        try:
            page.get_by_role("listitem").first.click(timeout=4000)
            time.sleep(2.5)
        except Exception as e:
            print("  conv click:", e)
        try:
            page.goto(f"{ORIGIN}/app/communication-center", wait_until="domcontentloaded")
            time.sleep(2)
            page.get_by_text("Send a message", exact=False).first.click(timeout=4000)
            time.sleep(2.5)
        except Exception as e:
            print("  compose click:", e)

        # Test results: list -> open first
        visit("app/test-results", 4)
        try:
            page.get_by_role("listitem").first.click(timeout=4000)
            time.sleep(2.5)
        except Exception as e:
            print("  result click:", e)

        visit("app/letters", 3)
        try:
            page.get_by_role("listitem").first.click(timeout=4000)
            time.sleep(2)
        except Exception:
            pass
        visit("app/health-summary", 3)

        fh.close()
        browser.close()
    print("wrote", OUT)


if __name__ == "__main__":
    main()
