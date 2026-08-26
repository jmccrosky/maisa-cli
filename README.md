# maisa-cli

An agent-friendly command-line interface for **Maisa** — the Finnish Apotti /
Epic MyChart patient portal (HUS, Helsinki, Vantaa, Kerava). Read health
information, messages, appointments, test results and letters, and send
messages, over the reverse-engineered JSON API — **no browser on data calls**.

Every data command supports `--json` for machine consumption.

> **Disclaimer.** Personal, unofficial tool for accessing *your own* health data
> through your own strong authentication. Not affiliated with or endorsed by
> Apotti, HUS, or Epic. No warranty; use at your own risk, and in line with the
> portal's terms and applicable law.

## How auth works (important)

Finnish health-data access legally requires **strong authentication** (the
Suomi.fi bank / mobile-certificate flow), so there is no way to skip it forever.
The design:

1. `maisa login` opens a browser **once** for the bank login and captures the
   MyChart session (httpOnly cookies) to `~/Library/Application Support/maisa/`
   using Playwright (locally — nothing is exfiltrated).
2. Every other command replays that session with plain `httpx`. No browser.
3. The session lives until Epic expires it server-side. `maisa keepalive`
   extends it; when it finally dies, commands print a clear "session expired"
   message and you re-run `maisa login`.

### Durable persistence (the mobile-FaceID mechanism)

The ~1-hour web session is the interim path. The mobile app's multi-week
persistence is **SMART-on-FHIR OAuth2 with a refresh token** — reverse-engineered
and implemented in `maisa/oauth.py` (`maisa oauth …`). It needs a `client_id`
registered at https://fhir.epic.com and enabled by Apotti for patient access;
then one bank login yields a refresh token good for weeks of headless refresh
(`refresh` needs only client_id + refresh_token — public client, no secret).

```sh
export MAISA_FHIR_BASE="https://gw.apottiekosysteemi.fi/Interconnect-FHIR-EKO01/api/FHIR/R4"
export MAISA_OAUTH_CLIENT_ID="<your registered client id>"
uv run maisa oauth login      # one-time bank login -> stores a refresh token
uv run maisa oauth status     # token state
uv run maisa fhir Patient     # authenticated FHIR call (auto-refreshes)
```

See `NOTES.md` → "Persistence" for the confirmed endpoints/capabilities and the
Apotti production-access gate.

## Install

```sh
uv sync
uv run playwright install chromium   # one-time, for `maisa login`
uv run maisa login                   # do the bank auth in the window that opens
uv run maisa whoami
```

## Commands

```
maisa login                         capture a session via the browser bank flow
maisa session show|import|clear     inspect / import / delete the stored session
maisa keepalive                     ping to extend the session

maisa whoami                        logged-in patient + proxy (family) accounts

maisa messages list [--folder inbox|archive|bookmarked|automated] [--search Q] [--limit N]
maisa messages read <id>            full conversation thread
maisa messages topics               subjects you can start a new message under
maisa messages send --topic <menuItemId> --subject S --body B --yes

maisa results list [--limit N]      test / lab results
maisa results read <csn> [--org O]  one result in detail

maisa letters list [--limit N]      letters / documents
maisa letters read <hnoId> [--csn C]

maisa appointments [--past]         upcoming appointments (or past visits)

maisa health summary|allergies|medications|immunizations|issues|careteam

maisa raw <path> [-X POST] [-d BODY] [--json-body] [--form]   probe any endpoint
```

Add `--json` to any data command for raw JSON output.

## Sending messages

`messages send` posts a real message to your care team, so it requires `--yes`:

```sh
uv run maisa messages topics
uv run maisa messages send --topic 32600 \
  --subject "Question about my appointment" \
  --body "Hi, ..." --yes
```

Without `--yes` it prints what it *would* send and stops.

## Layout

- `maisa/auth.py`   — Playwright login + session capture
- `maisa/session.py`— on-disk session store (0600)
- `maisa/client.py` — httpx transport, CSRF handling, expiry detection
- `maisa/api.py`    — the high-level API (all endpoints)
- `maisa/cli.py`    — the command surface
- `scripts/`        — discovery tools used to map the API (re-run to re-map)
```
