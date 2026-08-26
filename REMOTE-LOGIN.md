# Remote login (headless Mac + phone over Tailscale)

The tool runs on a Mac you don't sit at; you chat with the agent over WhatsApp.
Finnish strong auth needs a human-driven browser, so the login browser opens
**on the Mac** and you drive it **from your phone** over your private tailnet.
The bank confirmation still happens in your bank's app on your phone as usual —
that part is device-independent.

Flow per login:

1. Agent hits an expired session (`maisa` commands exit code 2 with
   "Session expired").
2. Agent runs `maisa login --remote`. That opens the browser on the Mac and
   prints one line, e.g.:
   `REMOTE_LOGIN_READY :: Open your VNC viewer app and connect to 100.115.115.80:5900 (over Tailscale).`
3. Agent forwards that line to you on WhatsApp.
4. You connect from your phone, see the Maisa login, do the bank flow (approve in
   your bank app), land on the home page.
5. `maisa login --remote` prints `REMOTE_LOGIN_DONE :: session captured` and
   exits 0. The agent retries whatever it was doing.

## One-time setup

### 1. Enable macOS Screen Sharing  (you must do this — it's a security setting)

System Settings → General → Sharing → turn on **Screen Sharing**. Under its
options, set "VNC viewers may control screen with password" and choose a
password. (CLI equivalent, run it yourself with `!` since it needs sudo:
`sudo /System/Library/CoreServices/RemoteManagement/ARDAgent.app/Contents/Resources/kickstart -activate -configure -access -on -restart -agent -privs -all`.)

Verify: `lsof -iTCP:5900 -sTCP:LISTEN` should show something listening.

### 2. Tailscale  (already up here — IP 100.115.115.80)

Install Tailscale on your **phone** too and sign into the same tailnet. Keep
Screen Sharing reachable **only** over Tailscale — do not port-forward 5900 to
the internet.

### 3. Keep the Mac usable while you're away

- The Mac must stay **logged in** (a locked screen is fine; the login window is
  not — Playwright can't render there).
- `maisa login --remote` runs `caffeinate` for the duration so it won't sleep
  mid-login. For always-on, System Settings → Displays/Battery → prevent sleep,
  or run the agent under `caffeinate -s`.

### 4. Pick how your phone connects

**Option A — VNC viewer app (least to install/run):**
Install a VNC client on your phone (e.g. RealVNC Viewer, free). Save a
connection to `100.115.115.80:5900`. Per login you just open the app.
`maisa login --remote` prints the address to connect to.

**Option B — noVNC "tap a link" (no phone app):**
Install once on the Mac: `brew install novnc`. Start the web bridge (leave it
running, or have the agent start it):
```sh
websockify --web="$(brew --prefix)/share/novnc" 6080 localhost:5900
```
Then run `maisa login --remote --novnc-port 6080`. It prints a
`http://100.115.115.80:6080/vnc.html?...` link you open on your phone — no app.

## Minimizing how often you log in

The web session lasts ~1 h. The agent should:

- run `maisa keepalive` periodically while it's actively using the tool (resets
  the inactivity clock), and
- only trigger `maisa login --remote` when a command actually returns
  "Session expired" — not preemptively.

So you get a WhatsApp "tap to log in" roughly once per active stretch, not per
command. (The durable, weeks-long alternative is the OAuth refresh-token path in
`maisa/oauth.py`, but it needs an Apotti-approved client_id — see NOTES.md.)

## Security notes

- The connection briefly exposes an interactive browser holding your bank login.
  Keeping it Tailscale-only + a VNC password is what protects it. Never expose
  5900 or the noVNC port publicly.
- macOS Screen Sharing shows the **whole desktop** while connected. If you want
  the login browser isolated instead, run it in a Linux container with
  Xvfb+x11vnc+noVNC (needs a container runtime; not installed here).
