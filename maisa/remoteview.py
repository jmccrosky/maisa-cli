"""Capture-free remote login: stream the *browser itself* to a phone.

Enabling macOS Screen Sharing on Ventura is blocked from the command line, so
instead of screen-grabbing the desktop we drive a headless Chromium and stream
its own frames via Chrome DevTools (Page.startScreencast) to a small web page
served over Tailscale. The viewer forwards the user's taps/scrolls/typing back
via CDP Input events. No Screen Sharing, no VNC, no TCC, no display needed —
works over plain SSH.

The browser runs headless (renders offscreen, launches fine without a GUI
session); the Maisa login + Suomi.fi bank flow work under it. The bank approval
still happens in the user's bank app on their phone, independent of this.

`serve_login()` blocks until the user reaches /maisa/Home/, captures the session
(state.json + session.json), then shuts down. It prints a relayable
`REMOTE_LOGIN_READY :: <url>` line for an orchestrating agent to forward.
"""

from __future__ import annotations

import asyncio
import json

from aiohttp import web

from . import auth
from .session import save

VIEWPORT_W = 400
VIEWPORT_H = 860

# Special-key virtual key codes for Input.dispatchKeyEvent.
_VK = {"Enter": 13, "Backspace": 8, "Tab": 9, "Escape": 27,
       "ArrowUp": 38, "ArrowDown": 40, "ArrowLeft": 37, "ArrowRight": 39}

VIEWER_HTML = """<!doctype html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no">
<title>Maisa login</title>
<style>
  *{box-sizing:border-box}
  html,body{margin:0;height:100%;background:#111;overflow:hidden;font-family:-apple-system,sans-serif}
  /* Fixed positioning + safe-area insets so the bar stays visible on iOS Safari. */
  #screenwrap{position:fixed;top:0;left:0;right:0;bottom:calc(60px + env(safe-area-inset-bottom));background:#000}
  #screen{width:100%;height:100%;object-fit:contain;touch-action:none;user-select:none;display:block}
  #bar{position:fixed;left:0;right:0;bottom:0;display:flex;gap:6px;align-items:center;z-index:10;
       background:#1c1c1e;padding:9px 8px calc(9px + env(safe-area-inset-bottom))}
  #kbd{flex:1;min-width:0;font-size:16px;padding:11px;border-radius:9px;border:0;background:#2c2c2e;color:#fff}
  .b{padding:11px 14px;border-radius:9px;border:0;background:#3a3a3c;color:#fff;font-size:17px}
  #status{position:fixed;top:4px;left:6px;color:#8e8e93;font-size:11px;pointer-events:none;z-index:11}
</style></head><body>
<div id=screenwrap><img id=screen></div>
<div id=bar>
  <input id=kbd placeholder="type here (tap a field above first)" autocapitalize=off autocorrect=off spellcheck=false autocomplete=off>
  <button class=b id=bksp>&#9003;</button>
  <button class=b id=ent>&#9166;</button>
</div>
<div id=status>connecting…</div>
<script>
const img=document.getElementById('screen'), st=document.getElementById('status');
let ws, W=%WW%, H=%HH%;
function connect(){
  ws=new WebSocket((location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws');
  ws.onopen=()=>st.textContent='live';
  ws.onclose=()=>{st.textContent='reconnecting…';setTimeout(connect,1000)};
  ws.onmessage=e=>{const m=JSON.parse(e.data);
    if(m.t==='frame'){img.src='data:image/jpeg;base64,'+m.d;}
    else if(m.t==='done'){st.textContent='login complete — you can close this';}};
}
connect();
function send(o){try{ws.send(JSON.stringify(o))}catch(e){}}
function norm(ev){const r=img.getBoundingClientRect();
  // account for object-fit:contain letterboxing
  const ar=W/H, rr=r.width/r.height; let dw=r.width,dh=r.height,ox=0,oy=0;
  if(rr>ar){dw=r.height*ar;ox=(r.width-dw)/2;}else{dh=r.width/ar;oy=(r.height-dh)/2;}
  const x=(ev.clientX-r.left-ox)/dw, y=(ev.clientY-r.top-oy)/dh;
  return {x:Math.max(0,Math.min(1,x)), y:Math.max(0,Math.min(1,y))};}
let sx,sy,st0,lastY,moved;
img.addEventListener('touchstart',e=>{const t=e.touches[0];const n=norm(t);sx=n.x;sy=n.y;lastY=t.clientY;st0=Date.now();moved=0;e.preventDefault();},{passive:false});
img.addEventListener('touchmove',e=>{const t=e.touches[0];const dy=lastY-t.clientY;lastY=t.clientY;moved+=Math.abs(dy);send({t:'wheel',dy:dy});e.preventDefault();},{passive:false});
img.addEventListener('touchend',e=>{if(moved<8 && Date.now()-st0<500){send({t:'tap',x:sx,y:sy});}e.preventDefault();},{passive:false});
// mouse (for desktop testing)
img.addEventListener('click',e=>{const n=norm(e);send({t:'tap',x:n.x,y:n.y});});
img.addEventListener('wheel',e=>{send({t:'wheel',dy:-e.deltaY});e.preventDefault();},{passive:false});
// typing
const kbd=document.getElementById('kbd');
kbd.addEventListener('beforeinput',e=>{
  if(e.inputType==='insertText'&&e.data){send({t:'text',text:e.data});}
  else if(e.inputType==='deleteContentBackward'){send({t:'key',key:'Backspace'});}
  else if(e.inputType&&e.inputType.indexOf('insertLineBreak')>=0){send({t:'key',key:'Enter'});}
  e.preventDefault();
});
document.getElementById('bksp').onclick=()=>{send({t:'key',key:'Backspace'});kbd.focus();};
document.getElementById('ent').onclick=()=>{send({t:'key',key:'Enter'});kbd.focus();};
</script></body></html>"""


async def serve_login(host: str = "0.0.0.0", port: int = 6080, timeout_s: int = 900):
    from playwright.async_api import async_playwright

    auth._ensure_dir()
    ip = auth.tailscale_ip()
    clients: set[web.WebSocketResponse] = set()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = await browser.new_context(
            viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"))
        page = await context.new_page()
        cdp = await context.new_cdp_session(page)

        async def start_cast():
            try:
                await cdp.send("Page.startScreencast", {
                    "format": "jpeg", "quality": 55,
                    "maxWidth": VIEWPORT_W, "maxHeight": VIEWPORT_H, "everyNthFrame": 1})
            except Exception:
                pass

        async def on_frame(params):
            sid = params.get("sessionId")
            data = params.get("data", "")
            msg = json.dumps({"t": "frame", "d": data})
            for ws in list(clients):
                try:
                    await ws.send_str(msg)
                except Exception:
                    clients.discard(ws)
            try:
                await cdp.send("Page.screencastFrameAck", {"sessionId": sid})
            except Exception:
                pass

        cdp.on("Page.screencastFrame", lambda p: asyncio.create_task(on_frame(p)))
        # Cross-origin navigations (Suomi.fi, bank) can pause the cast; restart it.
        page.on("framenavigated",
                lambda f: asyncio.create_task(start_cast()) if f == page.main_frame else None)

        async def dispatch(m):
            t = m.get("t")
            if t == "tap":
                x, y = m["x"] * VIEWPORT_W, m["y"] * VIEWPORT_H
                await cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
                for et in ("mousePressed", "mouseReleased"):
                    await cdp.send("Input.dispatchMouseEvent",
                                   {"type": et, "x": x, "y": y, "button": "left", "clickCount": 1})
            elif t == "wheel":
                await cdp.send("Input.dispatchMouseEvent", {
                    "type": "mouseWheel", "x": VIEWPORT_W / 2, "y": VIEWPORT_H / 2,
                    "deltaX": 0, "deltaY": -float(m.get("dy", 0))})
            elif t == "text":
                await cdp.send("Input.insertText", {"text": m.get("text", "")})
            elif t == "key":
                k = m.get("key", "")
                vk = _VK.get(k, 0)
                for et in ("rawKeyDown", "keyUp"):
                    await cdp.send("Input.dispatchKeyEvent", {
                        "type": et, "key": k, "code": k,
                        "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk})

        async def ws_handler(request):
            ws = web.WebSocketResponse(max_msg_size=0)
            await ws.prepare(request)
            clients.add(ws)
            await start_cast()
            try:
                async for msg in ws:
                    if msg.type == web.WSMsgType.TEXT:
                        try:
                            await dispatch(json.loads(msg.data))
                        except Exception:
                            pass
            finally:
                clients.discard(ws)
            return ws

        html = VIEWER_HTML.replace("%WW%", str(VIEWPORT_W)).replace("%HH%", str(VIEWPORT_H))
        app = web.Application()
        app.router.add_get("/", lambda r: web.Response(text=html, content_type="text/html"))
        app.router.add_get("/ws", ws_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()

        url = f"http://{ip}:{port}/" if ip else f"http://<agent-mac-ip>:{port}/"
        print(f"REMOTE_LOGIN_READY :: Open this on your phone (Tailscale): {url}", flush=True)

        await page.goto(auth.LOGIN_URL)
        await start_cast()

        # Wait for the authenticated home page.
        captured = None
        for _ in range(timeout_s):
            if "/maisa/Home" in page.url:
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                state = await context.storage_state()
                captured = auth._session_from_state(state)
                save(captured)
                auth.STATE_PATH.write_text(json.dumps(state))
                for ws in list(clients):
                    try:
                        await ws.send_str(json.dumps({"t": "done"}))
                    except Exception:
                        pass
                break
            await asyncio.sleep(1)

        try:
            await cdp.send("Page.stopScreencast")
        except Exception:
            pass
        await runner.cleanup()
        await browser.close()

    if captured:
        print("REMOTE_LOGIN_DONE :: session captured", flush=True)
    else:
        print("REMOTE_LOGIN_TIMEOUT :: no login within the time limit", flush=True)
    return captured


def run(host: str = "0.0.0.0", port: int = 6080, timeout_s: int = 900):
    return asyncio.run(serve_login(host=host, port=port, timeout_s=timeout_s))
