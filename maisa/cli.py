"""Maisa CLI — agent-friendly interface to the Maisa (Apotti/MyChart) portal.

Every data command accepts --json for machine consumption; without it you get a
compact human-readable view. Auth is a captured session (see `maisa login`);
data calls use plain httpx, no browser.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from rich.console import Console
from rich.table import Table

from . import session as sess
from .client import MaisaClient, SessionExpired

console = Console()
err = Console(stderr=True)

FOLDERS = {"inbox": 1, "archive": 2, "bookmarked": 3, "automated": 6}


def _api():
    from .api import MaisaAPI
    return MaisaAPI()


def _emit(args, data, human):
    """Print JSON if --json, else run the human formatter."""
    if getattr(args, "json", False):
        console.print_json(json.dumps(data, ensure_ascii=False, default=str))
    else:
        human(data)


def _run(fn):
    """Wrap an API call with session-expiry handling."""
    try:
        return fn()
    except SessionExpired as e:
        err.print(f"[red]Session expired:[/] {e}")
        raise SystemExit(2)


# -- auth / session -------------------------------------------------------

def cmd_login(args):
    from . import auth
    if args.auto:
        # OP auto-login: fills the user ID (from Keychain/env/file), user approves in app.
        from . import auto
        auto.auto_login(headless=not args.headed, timeout_s=args.timeout)
        return 0
    if args.stream:
        # Capture-free: stream the browser itself to the phone over Tailscale.
        # No macOS Screen Sharing needed. Blocks until login completes.
        from . import remoteview
        remoteview.run(host=args.host, port=args.port, timeout_s=args.timeout)
        return 0
    if args.remote:
        # Unattended Mac: browser opens here, user drives it from their phone
        # over Tailscale/Screen Sharing. The READY line is meant to be relayed.
        auth.remote_login(timeout_s=args.timeout, novnc_port=args.novnc_port)
        console.print("[green]Session captured.[/]")
        return 0
    console.print("[bold]Opening a browser for Maisa login.[/] Complete the bank / strong-auth flow.")
    auth.login(headless=False, discover=not args.no_discover, timeout_s=args.timeout)
    console.print("[green]Session captured.[/] Try `maisa whoami`.")
    return 0


def cmd_session_show(args):
    s = sess.load()
    if not s:
        err.print("[yellow]No session.[/] Run `maisa login`.")
        return 1
    if args.json:
        console.print_json(json.dumps({
            "host": s.host, "cookies": sorted(s.cookies), "hasCsrf": bool(s.csrf_token),
            "capturedAt": s.captured_at, "ageHours": round(s.age_seconds() / 3600, 2),
        }))
        return 0
    t = Table(show_header=False, box=None)
    t.add_row("host", s.host)
    t.add_row("cookies", str(len(s.cookies)))
    if s.captured_at:
        t.add_row("captured", time.strftime("%Y-%m-%d %H:%M", time.localtime(s.captured_at)))
        t.add_row("age", f"{s.age_seconds()/3600:.1f} h")
    console.print(t)
    return 0


def cmd_session_import(args):
    raw = sys.stdin.read() if args.file == "-" else open(args.file).read()
    d = json.loads(raw)
    s = sess.Session(host=d.get("host", sess.DEFAULT_HOST), base=d.get("base", sess.DEFAULT_BASE),
                     cookies=d.get("cookies", {}), csrf_token=d.get("csrf_token"),
                     captured_at=d.get("captured_at") or time.time(), meta=d.get("meta", {}))
    sess.save(s)
    console.print(f"[green]Saved[/] ({len(s.cookies)} cookies).")
    return 0


def cmd_session_clear(args):
    sess.clear()
    console.print("Cleared.")
    return 0


def cmd_keepalive(args):
    """Ping the session-keepalive endpoints to extend the session."""
    s = sess.load_or_die()
    c = MaisaClient(s)
    try:
        r = c.get("Home/KeepAlive?cnt=1&noCache=0.1")
        c.get("keepalive.asp?cnt=1&noCache=0.1")
        c.persist()
        console.print(f"[green]keepalive[/] -> {r.status_code}")
    except SessionExpired as e:
        err.print(f"[red]expired:[/] {e}")
        return 2
    return 0


# -- read commands --------------------------------------------------------

def cmd_whoami(args):
    with _api() as a:
        _emit(args, _run(a.whoami), lambda d: console.print(
            f"[bold]{d['name']}[/]" + (f"   proxies: {', '.join(d['proxies'])}" if d['proxies'] else "")))
    return 0


def cmd_messages_list(args):
    folder = FOLDERS.get(args.folder, 1)
    with _api() as a:
        data = _run(lambda: a.messages(folder, search=args.search or "", limit=args.limit))

        def human(rows):
            t = Table(title=f"Messages ({args.folder})")
            t.add_column("subject")
            t.add_column("preview", overflow="fold")
            t.add_column("id", style="dim", no_wrap=True)
            for m in rows:
                subj = ("● " if m["unread"] else "") + (m["subject"] or "")
                t.add_row(subj, (m["preview"] or "")[:70], (m["id"] or "")[:18] + "…")
            console.print(t)
            console.print(f"[dim]{len(rows)} conversations. Read one: maisa messages read <id>[/]")
        _emit(args, data, human)
    return 0


def cmd_messages_read(args):
    with _api() as a:
        data = _run(lambda: a.conversation(args.id))

        def human(c):
            console.print(f"[bold]{c['subject']}[/]  ({c['totalMessages']} messages, "
                          f"canReply={c['canReply']})\n")
            for m in c["messages"]:
                console.print(f"[cyan]{m['from']}[/] [dim]{m['date']}[/]")
                console.print(m["body"] or "[dim](no text)[/]")
                if m["attachments"]:
                    console.print(f"[dim]attachments: {', '.join(m['attachments'])}[/]")
                console.print("[dim]" + "─" * 50 + "[/]")
        _emit(args, data, human)
    return 0


def cmd_messages_topics(args):
    with _api() as a:
        data = _run(a.compose_topics)

        def human(rows):
            t = Table(title="Message topics (use menuItemId with `messages send`)")
            t.add_column("menuItemId", style="bold")
            t.add_column("name")
            for r in rows:
                t.add_row(str(r["menuItemId"]), r["name"])
            console.print(t)
        _emit(args, data, human)
    return 0


def cmd_messages_send(args):
    if not args.yes:
        err.print("[yellow]This sends a real message to your care team.[/] "
                  "Re-run with --yes to confirm.")
        err.print(f"  topic={args.topic}  subject={args.subject!r}")
        err.print(f"  body={args.body!r}")
        return 1
    with _api() as a:
        res = _run(lambda: a.send_message(args.topic, args.subject, args.body))
        console.print(f"[green]Sent.[/] composeId={res['composeId'][:16]}…")
    return 0


def cmd_results_list(args):
    with _api() as a:
        data = _run(lambda: a.test_results(max_results=args.limit))

        def human(rows):
            t = Table(title="Test results")
            t.add_column("date", style="dim", no_wrap=True)
            t.add_column("name")
            t.add_column("provider", style="dim")
            t.add_column("csn", style="dim", no_wrap=True)
            for r in rows:
                t.add_row(str(r["date"] or ""), r["name"] or "", r["provider"] or "", (r["csn"] or "")[:16] + "…")
            console.print(t)
            console.print(f"[dim]{len(rows)} results. Detail: maisa results read <csn>[/]")
        _emit(args, data, human)
    return 0


def cmd_results_read(args):
    with _api() as a:
        _emit(args, _run(lambda: a.test_result(args.csn, args.org or "")),
              lambda d: console.print_json(json.dumps(d, ensure_ascii=False)))
    return 0


def cmd_letters_list(args):
    with _api() as a:
        data = _run(a.letters)

        def human(rows):
            t = Table(title="Letters")
            t.add_column("date", style="dim", no_wrap=True)
            t.add_column("title")
            t.add_column("from", style="dim")
            t.add_column("hnoId", style="dim", no_wrap=True)
            for l in rows[: args.limit] if args.limit else rows:
                t.add_row(str(l["date"] or ""), l["title"] or "", l["from"] or "", (l["hnoId"] or "")[:16] + "…")
            console.print(t)
        _emit(args, data, human)
    return 0


def cmd_letters_read(args):
    with _api() as a:
        _emit(args, _run(lambda: a.letter(args.hno_id, args.csn or "")),
              lambda d: console.print_json(json.dumps(d, ensure_ascii=False)))
    return 0


def cmd_appointments(args):
    with _api() as a:
        data = _run(a.past_visits if args.past else a.appointments)

        def human(rows):
            t = Table(title="Past visits" if args.past else "Upcoming appointments")
            t.add_column("date")
            t.add_column("time")
            t.add_column("type")
            t.add_column("provider", style="dim")
            t.add_column("department", style="dim")
            for v in rows:
                t.add_row(str(v["date"] or ""), str(v["time"] or ""), v["type"] or "",
                          v["provider"] or "", v["department"] or "")
            console.print(t)
            for v in rows:
                if v.get("instructions"):
                    console.print(f"[dim]— {v['type']}: {v['instructions'][:200]}[/]")
        _emit(args, data, human)
    return 0


def cmd_health(args):
    with _api() as a:
        fn = {
            "summary": a.health_summary, "allergies": a.allergies,
            "medications": a.medications, "immunizations": a.immunizations,
            "issues": a.health_issues, "careteam": a.care_team,
        }[args.topic]
        _emit(args, _run(fn), lambda d: console.print_json(json.dumps(d, ensure_ascii=False)))
    return 0


def cmd_oauth_status(args):
    from . import oauth
    cfg = oauth.OAuthConfig.from_env()
    t = oauth.load_tokens()
    info = {
        "fhir_base": cfg.fhir_base,
        "client_id_set": bool(cfg.client_id),
        "redirect_uri": cfg.redirect_uri,
        "scopes": cfg.scopes,
        "hasTokens": bool(t),
        "accessTokenValid": bool(t and not t.expired()),
        "hasRefreshToken": bool(t and t.refresh_token),
        "patient": t.patient if t else None,
        "expiresInSec": int(t.expires_at - time.time()) if t else None,
    }
    if args.json:
        console.print_json(json.dumps(info))
    else:
        for k, v in info.items():
            console.print(f"  {k}: {v}")
        if not cfg.client_id:
            console.print("\n[yellow]No client_id.[/] This is the durable-persistence path "
                          "(refresh tokens). Register an app at https://fhir.epic.com, get it "
                          "enabled for Apotti, then set MAISA_OAUTH_CLIENT_ID + MAISA_FHIR_BASE.")
    return 0


def cmd_oauth_login(args):
    from . import oauth
    cfg = oauth.OAuthConfig.from_env()
    t = oauth.interactive_login(cfg, open_browser=not args.print_url)
    console.print(f"[green]Got tokens.[/] refresh_token stored; access expires in "
                  f"{int(t.expires_at - time.time())}s. patient={t.patient or '?'}")
    return 0


def cmd_oauth_refresh(args):
    from . import oauth
    cfg = oauth.OAuthConfig.from_env()
    t = oauth.ensure_access_token(cfg)
    console.print(f"[green]Access token valid[/] for {int(t.expires_at - time.time())}s.")
    return 0


def cmd_fhir(args):
    from . import oauth
    cfg = oauth.OAuthConfig.from_env()
    t = oauth.ensure_access_token(cfg)
    import httpx
    url = args.path if args.path.startswith("http") else f"{(cfg.fhir_base or t.fhir_base)}/{args.path.lstrip('/')}"
    r = httpx.get(url, headers={"Authorization": f"Bearer {t.access_token}",
                                "Accept": "application/fhir+json"}, timeout=30)
    console.print(f"[dim]{r.status_code}[/]")
    try:
        console.print_json(json.dumps(r.json()))
    except Exception:
        console.print(r.text[:4000])
    return 0


def cmd_raw(args):
    body = None
    if args.data:
        body = json.loads(args.data) if args.json_body else args.data
    s = sess.load_or_die()
    with MaisaClient(s) as c:
        try:
            if args.method == "POST" and args.form:
                resp = c.post_form(args.path, data=body if isinstance(body, dict) else None)
            elif args.json_body:
                out = c.json_api(args.path, body or {})
                console.print_json(json.dumps(out, ensure_ascii=False, default=str))
                c.persist()
                return 0
            else:
                kw = {"data": body} if body is not None else {}
                resp = c.request(args.method, args.path, **kw)
            c.persist()
        except SessionExpired as e:
            err.print(f"[red]expired:[/] {e}")
            return 2
    ct = resp.headers.get("content-type", "")
    console.print(f"[dim]{resp.status_code} {ct[:30]}[/]")
    if "json" in ct:
        console.print_json(json.dumps(resp.json(), ensure_ascii=False))
    else:
        console.print(resp.text[: args.max])
    return 0


# -- parser ---------------------------------------------------------------

def _add_json(p):
    p.add_argument("--json", action="store_true", help="output raw JSON")


def build_parser():
    p = argparse.ArgumentParser(prog="maisa", description="Maisa (Apotti/MyChart) CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("login", help="log in via the browser bank flow")
    pl.add_argument("--no-discover", action="store_true")
    pl.add_argument("--timeout", type=int, default=300)
    pl.add_argument("--remote", action="store_true",
                    help="unattended Mac: open the browser here, drive it from your phone via Tailscale/Screen Sharing")
    pl.add_argument("--novnc-port", type=int, default=None,
                    help="if a noVNC/websockify server is running on this port, emit a tap-to-open link instead of a VNC address")
    pl.add_argument("--stream", action="store_true",
                    help="capture-free: stream the browser itself to your phone over Tailscale (no Screen Sharing needed)")
    pl.add_argument("--host", default="0.0.0.0", help="bind host for --stream (default 0.0.0.0)")
    pl.add_argument("--port", type=int, default=6080, help="port for the --stream viewer")
    pl.add_argument("--auto", action="store_true",
                    help="OP auto-login: fill your OP user ID (from Keychain), you approve in the OP app")
    pl.add_argument("--headed", action="store_true", help="run --auto with a visible browser (needs a GUI session)")
    pl.set_defaults(func=cmd_login)

    ps = sub.add_parser("session", help="manage the stored session")
    ss = ps.add_subparsers(dest="scmd", required=True)
    sh = ss.add_parser("show"); _add_json(sh); sh.set_defaults(func=cmd_session_show)
    im = ss.add_parser("import"); im.add_argument("file"); im.set_defaults(func=cmd_session_import)
    ss.add_parser("clear").set_defaults(func=cmd_session_clear)

    sub.add_parser("keepalive", help="extend the session").set_defaults(func=cmd_keepalive)

    wa = sub.add_parser("whoami", help="show the logged-in patient + proxies"); _add_json(wa)
    wa.set_defaults(func=cmd_whoami)

    # messages
    pm = sub.add_parser("messages", help="secure messages")
    ms = pm.add_subparsers(dest="mcmd", required=True)
    ml = ms.add_parser("list"); ml.add_argument("--folder", choices=FOLDERS, default="inbox")
    ml.add_argument("--search", default=""); ml.add_argument("--limit", type=int, default=0)
    _add_json(ml); ml.set_defaults(func=cmd_messages_list)
    mr = ms.add_parser("read"); mr.add_argument("id"); _add_json(mr); mr.set_defaults(func=cmd_messages_read)
    mt = ms.add_parser("topics"); _add_json(mt); mt.set_defaults(func=cmd_messages_topics)
    msd = ms.add_parser("send")
    msd.add_argument("--topic", required=True, help="menuItemId from `messages topics`")
    msd.add_argument("--subject", required=True); msd.add_argument("--body", required=True)
    msd.add_argument("--yes", action="store_true", help="confirm sending a real message")
    msd.set_defaults(func=cmd_messages_send)

    # results
    pr = sub.add_parser("results", help="test/lab results")
    rs = pr.add_subparsers(dest="rcmd", required=True)
    rl = rs.add_parser("list"); rl.add_argument("--limit", type=int, default=0); _add_json(rl)
    rl.set_defaults(func=cmd_results_list)
    rr = rs.add_parser("read"); rr.add_argument("csn"); rr.add_argument("--org", default="")
    _add_json(rr); rr.set_defaults(func=cmd_results_read)

    # letters
    pt = sub.add_parser("letters", help="letters / documents")
    ls = pt.add_subparsers(dest="lcmd", required=True)
    ll = ls.add_parser("list"); ll.add_argument("--limit", type=int, default=0); _add_json(ll)
    ll.set_defaults(func=cmd_letters_list)
    lr = ls.add_parser("read"); lr.add_argument("hno_id"); lr.add_argument("--csn", default="")
    _add_json(lr); lr.set_defaults(func=cmd_letters_read)

    # appointments
    pa = sub.add_parser("appointments", help="upcoming appointments / past visits")
    pa.add_argument("--past", action="store_true"); _add_json(pa)
    pa.set_defaults(func=cmd_appointments)

    # health record
    ph = sub.add_parser("health", help="health record")
    ph.add_argument("topic", choices=["summary", "allergies", "medications",
                                      "immunizations", "issues", "careteam"])
    _add_json(ph); ph.set_defaults(func=cmd_health)

    # oauth (durable refresh-token persistence — SMART on FHIR)
    po = sub.add_parser("oauth", help="durable persistence via SMART-on-FHIR refresh tokens")
    os_ = po.add_subparsers(dest="ocmd", required=True)
    ost = os_.add_parser("status"); _add_json(ost); ost.set_defaults(func=cmd_oauth_status)
    olg = os_.add_parser("login"); olg.add_argument("--print-url", action="store_true",
                        help="print the authorize URL instead of opening a browser")
    olg.set_defaults(func=cmd_oauth_login)
    os_.add_parser("refresh").set_defaults(func=cmd_oauth_refresh)

    pf = sub.add_parser("fhir", help="authenticated FHIR GET (uses OAuth access token)")
    pf.add_argument("path", help="FHIR resource path, e.g. Patient/<id> or Observation?category=laboratory")
    pf.set_defaults(func=cmd_fhir)

    # raw
    prw = sub.add_parser("raw", help="issue a raw request")
    prw.add_argument("path"); prw.add_argument("-X", "--method", default="GET")
    prw.add_argument("-d", "--data"); prw.add_argument("--json-body", action="store_true",
                     help="send body as JSON to a /api endpoint and print JSON")
    prw.add_argument("--form", action="store_true", help="POST as form with CSRF token")
    prw.add_argument("--max", type=int, default=4000)
    prw.set_defaults(func=cmd_raw)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
