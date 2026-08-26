---
name: maisa
description: Access the user's Maisa (Finnish Apotti / Epic MyChart) health portal — read health record, secure messages, appointments, test results and letters, and (with explicit confirmation) send a message to their care team. Use whenever the user asks about their health data, care-team messages, upcoming/past appointments, lab/test results, or letters from care, or wants to message their care team.
---

# Maisa health portal

Wraps the `maisa` CLI (https://github.com/jmccrosky/maisa-cli), which talks to
the Maisa patient portal over its reverse-engineered API. Run it from where the
repo is cloned:

```
cd <REPO_PATH> && uv run maisa <command>
```

Add `--json` to any data command and parse that (don't scrape the human table).

## Commands

- `maisa whoami` — the patient + proxy (family) accounts.
- `maisa messages list [--folder inbox|archive|bookmarked|automated] [--search Q] [--limit N]`
- `maisa messages read <id>` — full conversation thread.
- `maisa messages topics` — subjects for starting a new message.
- `maisa results list [--limit N]` / `maisa results read <csn>`
- `maisa letters list [--limit N]` / `maisa letters read <hnoId> [--csn C]`
- `maisa appointments [--past]`
- `maisa health summary|allergies|medications|immunizations|issues|careteam`
- `maisa keepalive` — extend the session.

## Sending a message (state-changing — needs care)

`maisa messages send --topic <menuItemId> --subject S --body B --yes`

NEVER send without first showing the user the exact topic, subject, and body and
getting an explicit "yes, send it". Without `--yes` the command only prints what
it would send. Messages go to real clinicians and into the medical record.

## Session handling (important)

Auth is a captured web session that lasts ~1 hour. Two rules:

1. **Keep it alive.** While actively using the tool in a session with the user,
   run `maisa keepalive` every few minutes so it doesn't expire mid-task.

2. **Re-login when it expires.** Any command may exit code 2 / print
   "Session expired". Preferred path — OP auto-login:

       maisa login --auto

   It fills the user's OP user ID (read locally from the Keychain — never
   through you) and submits, then prints `APPROVE_NOW :: ... approve the login
   in your OP-mobiili app`. **Relay that to the user over WhatsApp** so they
   approve on their phone. It waits until they reach the Maisa home page, prints
   `AUTO_LOGIN_DONE :: session captured`, and exits 0. Then retry the original
   command. (Run it in the background so you can read and relay the APPROVE_NOW
   line while it waits.) You never handle the user's user ID or any bank secret;
   the user only approves in the app.

   Fallback (only if `--auto` fails, e.g. OP page changed): `maisa login
   --stream`, which streams a browser to the user's phone over Tailscale; relay
   the `REMOTE_LOGIN_READY :: http://<ip>:6080/` URL and let them drive the
   login on their phone. It prints `REMOTE_LOGIN_DONE` when captured.

You never handle the user's bank credentials — the user always does the bank
step themselves on their phone. There is no way around a periodic strong-auth
login; that is a legal requirement, not a limitation of the tool.

## Notes

- IDs are opaque tokens (e.g. `WP-24…`); pass them back verbatim, never edit them.
- If a command reports no session at all, the tool was never logged in — run the
  `maisa login --remote` flow above.
