"""Headless discovery of per-feature API endpoints, using the saved session.

Navigates to each portal feature route and logs every non-static request +
response (JSON bodies) to CONFIG_DIR/sections-discovery.jsonl for analysis.
No login required — reuses ~/.maisa state.json.
"""
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from maisa.auth import STATE_PATH, attach_logger
from maisa.session import CONFIG_DIR, DEFAULT_BASE, DEFAULT_HOST

OUT = CONFIG_DIR / "sections-discovery.jsonl"
ORIGIN = f"https://{DEFAULT_HOST}{DEFAULT_BASE}"

ROUTES = [
    ("messages", "app/communication-center"),
    ("test-results", "Clinical/TestResults"),
    ("events", "Visits"),
    ("medications", "Clinical/Medications"),
    ("health-summary", "app/health-summary"),
    ("letters", "app/letters"),
    ("care-team", "Clinical/CareTeam"),
    ("medical-history", "MedicalHistory"),
    ("health-upkeep", "app/health-upkeep"),
    ("todo", "todo"),
]


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(STATE_PATH))
        fh = attach_logger(context, OUT)
        page = context.new_page()
        for name, route in ROUTES:
            url = f"{ORIGIN}/{route}"
            try:
                print(f"visiting {name}: {url}")
                page.goto(url, wait_until="domcontentloaded", timeout=25000)
                try:
                    page.wait_for_load_state("networkidle", timeout=12000)
                except Exception:
                    pass
                time.sleep(1.5)
                # open first list item for detail endpoints
                for sel in ["[role=listitem]", "li a", ".listItem", "a.item",
                            "table tbody tr", ".conversationListItem"]:
                    try:
                        loc = page.locator(sel).first
                        if loc.count() and loc.is_visible():
                            loc.click(timeout=2500)
                            page.wait_for_load_state("networkidle", timeout=8000)
                            time.sleep(1.0)
                            break
                    except Exception:
                        continue
            except Exception as e:
                print(f"  ! {name} failed: {e}")
        fh.close()
        browser.close()
    print(f"\nWrote {OUT}")


if __name__ == "__main__":
    main()
