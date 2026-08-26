"""Reach the message compose form and log every endpoint it calls (no send).

Headed, reuses saved session. Logs all /maisa/ requests during the compose
flow and screenshots the form, so the compose-view + submit endpoints can be
identified. Nothing is submitted.
"""
import json, time
from playwright.sync_api import sync_playwright
from maisa.auth import STATE_PATH
from maisa.session import CONFIG_DIR, DEFAULT_BASE, DEFAULT_HOST

ORIGIN = f"https://{DEFAULT_HOST}{DEFAULT_BASE}"
OUT = CONFIG_DIR / "compose-requests.json"
SHOT = CONFIG_DIR / "compose-form.png"
reqs = []


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(storage_state=str(STATE_PATH))
        context.on("request", lambda r: reqs.append(
            {"m": r.method, "u": r.url, "d": (r.post_data or "")[:400]})
            if "/maisa/" in r.url and r.method == "POST" else None)
        page = context.new_page()
        page.goto(f"{ORIGIN}/app/communication-center", wait_until="domcontentloaded", timeout=30000)
        try: page.wait_for_load_state("networkidle", timeout=12000)
        except Exception: pass
        time.sleep(2)
        try:
            page.get_by_text("Send a message", exact=False).first.click(timeout=6000)
            time.sleep(2)
            page.get_by_text("Send a message about", exact=False).first.click(timeout=5000)
            time.sleep(3)
        except Exception as e:
            print("nav:", e)
        time.sleep(3)
        print("compose URL:", page.url)
        try:
            page.screenshot(path=str(SHOT), full_page=True)
        except Exception as e:
            print("shot:", e)
        # dump any form action / send button info
        info = page.evaluate("""() => ({
            url: location.href,
            buttons: [...document.querySelectorAll('button')].map(b=>b.textContent.trim()).filter(Boolean).slice(0,20),
            textareas: document.querySelectorAll('textarea').length,
            contenteditable: document.querySelectorAll('[contenteditable=true]').length,
            forms: [...document.querySelectorAll('form')].map(f=>f.action)
        })""")
        print("FORM INFO:", json.dumps(info, ensure_ascii=False))
        # filter to conversations api posts
        conv = [r for r in reqs if r and "conversations" in r["u"]]
        seen=set(); uniq=[]
        for r in conv:
            k=r["u"].split("?")[0]
            if k not in seen: seen.add(k); uniq.append(r)
        OUT.write_text(json.dumps(uniq, indent=2, ensure_ascii=False))
        for r in uniq: print("  POST", r["u"].replace(ORIGIN,""), "  data=", r["d"][:80])
        browser.close()
    print("wrote", OUT, "and", SHOT)


if __name__ == "__main__":
    main()
