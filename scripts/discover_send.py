"""Capture the message SUBMIT endpoint safely (armed-abort; nothing is sent).

Drives: Send a message -> pick subject -> compose form -> Send. Before clicking
Send we ARM an interceptor that aborts EVERY /maisa/api POST and logs it, so the
actual submit is captured but never reaches the server, regardless of its name.
"""
import json, time
from playwright.sync_api import sync_playwright
from maisa.auth import STATE_PATH
from maisa.session import CONFIG_DIR, DEFAULT_BASE, DEFAULT_HOST

ORIGIN = f"https://{DEFAULT_HOST}{DEFAULT_BASE}"
OUT = CONFIG_DIR / "send-capture.json"
state = {"armed": False}
captured = []


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(storage_state=str(STATE_PATH))

        def handle(route):
            r = route.request
            if state["armed"] and r.method == "POST" and "/maisa/api/" in r.url:
                captured.append({"url": r.url, "data": (r.post_data or "")[:4000],
                                 "ctype": r.headers.get("content-type", "")})
                print("ARMED-ABORT:", r.url.replace(ORIGIN, ""))
                return route.abort()
            return route.continue_()

        context.route("**/maisa/**", handle)
        page = context.new_page()

        def shot(n):
            page.screenshot(path=str(CONFIG_DIR / f"send-step-{n}.png"))

        page.goto(f"{ORIGIN}/app/communication-center", wait_until="domcontentloaded", timeout=30000)
        try: page.wait_for_load_state("networkidle", timeout=12000)
        except Exception: pass
        time.sleep(2)
        page.get_by_role("button", name="Send a message", exact=False).first.click(timeout=6000)
        time.sleep(2.5); shot(1)
        # pick the first subject (role=button in the "Select a Subject" panel)
        clicked_subj = False
        for name in ["Non-urgent question about care or services",
                     "Question about medication", "Information I want to send"]:
            try:
                page.get_by_role("button", name=name, exact=False).first.click(timeout=4000)
                clicked_subj = True; print("subject:", name); break
            except Exception:
                continue
        if not clicked_subj:
            print("subject panel buttons:",
                  page.evaluate("()=>[...document.querySelectorAll('button')].map(b=>b.textContent.trim()).filter(Boolean).slice(0,20)"))
        time.sleep(3); shot(2)
        # possible disclaimer / continue button
        for lbl in ["Continue", "Accept", "I Agree", "Agree", "OK", "Next"]:
            try:
                page.get_by_role("button", name=lbl, exact=False).first.click(timeout=1500)
                time.sleep(2); break
            except Exception: continue
        shot(3)
        # fill body: textarea or rich text
        filled = False
        for sel in ["textarea", "[contenteditable=true]", "div[role=textbox]"]:
            try:
                loc = page.locator(sel).first
                if loc.count():
                    loc.click(timeout=1500); loc.fill("Test message - do not send") if sel == "textarea" else page.keyboard.type("Test message - do not send")
                    filled = True; break
            except Exception: continue
        # subject line if present
        try:
            page.get_by_placeholder("Subject", exact=False).first.fill("Test", timeout=1500)
        except Exception: pass
        time.sleep(1); shot(4)
        print("form filled:", filled, "url:", page.url)
        buttons = page.evaluate("() => [...document.querySelectorAll('button')].map(b=>b.textContent.trim()).filter(Boolean)")
        print("buttons:", buttons[:25])
        # ARM then click Send
        state["armed"] = True
        clicked = False
        for lbl in ["Send", "Send message", "Send Message", "Submit"]:
            try:
                page.get_by_role("button", name=lbl, exact=True).first.click(timeout=2500)
                clicked = True; print("clicked", lbl); break
            except Exception: continue
        if not clicked:
            for lbl in ["Send", "Submit"]:
                try:
                    page.get_by_text(lbl, exact=False).last.click(timeout=2000); clicked = True; print("clicked(text)", lbl); break
                except Exception: continue
        time.sleep(2); shot(5)
        OUT.write_text(json.dumps(captured, indent=2, ensure_ascii=False))
        browser.close()
    print(f"captured {len(captured)} -> {OUT}")
    for c in captured: print("  ", c["url"].replace(ORIGIN, ""), c["data"][:120])


if __name__ == "__main__":
    main()
