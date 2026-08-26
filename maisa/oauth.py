"""SMART-on-FHIR OAuth2 persistence — the mechanism behind the app's FaceID.

This is the *sanctioned* long-lived-session method, reverse-engineered from
Apotti's public SMART discovery document:

  Authorization Code + PKCE (S256), public client (no secret), and a
  `refresh_token` grant. One interactive bank login yields an access token
  (short-lived) plus a refresh token (long-lived), and the refresh token mints
  new access tokens for weeks with only client_id + refresh_token — exactly what
  the mobile app stores behind biometrics.

Endpoints are discovered from the FHIR base's `.well-known/smart-configuration`.

WHAT YOU MUST SUPPLY (one-time, out of band):
  * MAISA_FHIR_BASE   — the FHIR R4 base URL for your Apotti environment
                        (sandbox: https://gw.apottiekosysteemi.fi/Interconnect-FHIR-EKO01/api/FHIR/R4)
  * MAISA_OAUTH_CLIENT_ID — a client_id registered at https://fhir.epic.com
                        ("Build Apps") and enabled for Apotti patient access.
  * MAISA_OAUTH_REDIRECT  — a redirect URI registered for that client
                        (default http://localhost:8765/callback for a loopback CLI client).

Production patient data lives on a PRD Interconnect host that is access-gated by
Apotti; the ecosystem/sandbox host (EKO01) carries only test patients. Getting a
client enabled for production is an Apotti approval step, not a code change.

Status: discovery + PKCE + URL construction are exercised in tests; the live
token exchange/refresh is standard OAuth2 but is not exercised here because it
requires a real registered client_id (see `scripts/oauth_selfcheck.py`).
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass, asdict
from pathlib import Path

import httpx

from .session import CONFIG_DIR

OAUTH_PATH = CONFIG_DIR / "oauth.json"

DEFAULT_SANDBOX_FHIR = "https://gw.apottiekosysteemi.fi/Interconnect-FHIR-EKO01/api/FHIR/R4"
DEFAULT_REDIRECT = "http://localhost:8765/callback"
# Patient-access scopes. offline_access is what makes the server issue a refresh token.
DEFAULT_SCOPES = "openid fhirUser offline_access patient/*.read"


@dataclass
class OAuthConfig:
    fhir_base: str
    client_id: str
    redirect_uri: str = DEFAULT_REDIRECT
    scopes: str = DEFAULT_SCOPES

    @classmethod
    def from_env(cls) -> "OAuthConfig":
        base = os.environ.get("MAISA_FHIR_BASE", DEFAULT_SANDBOX_FHIR)
        cid = os.environ.get("MAISA_OAUTH_CLIENT_ID", "")
        return cls(
            fhir_base=base.rstrip("/"),
            client_id=cid,
            redirect_uri=os.environ.get("MAISA_OAUTH_REDIRECT", DEFAULT_REDIRECT),
            scopes=os.environ.get("MAISA_OAUTH_SCOPES", DEFAULT_SCOPES),
        )


@dataclass
class OAuthTokens:
    access_token: str = ""
    refresh_token: str = ""
    token_type: str = "Bearer"
    expires_at: float = 0.0
    patient: str = ""
    scope: str = ""
    fhir_base: str = ""

    def expired(self, skew: int = 60) -> bool:
        return time.time() >= (self.expires_at - skew)


# -- discovery ------------------------------------------------------------

def discover(fhir_base: str) -> dict:
    """Fetch the SMART configuration (public metadata) for the FHIR base."""
    url = fhir_base.rstrip("/") + "/.well-known/smart-configuration"
    r = httpx.get(url, headers={"Accept": "application/json"}, timeout=20, follow_redirects=True)
    r.raise_for_status()
    return r.json()


# -- PKCE -----------------------------------------------------------------

def make_pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(40)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_authorize_url(cfg: OAuthConfig, authorize_endpoint: str,
                        state: str, code_challenge: str) -> str:
    params = {
        "response_type": "code",
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "scope": cfg.scopes,
        "state": state,
        "aud": cfg.fhir_base,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return authorize_endpoint + "?" + urllib.parse.urlencode(params)


# -- token exchange / refresh --------------------------------------------

def exchange_code(cfg: OAuthConfig, token_endpoint: str, code: str, verifier: str) -> OAuthTokens:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": cfg.redirect_uri,
        "client_id": cfg.client_id,
        "code_verifier": verifier,
    }
    return _token_request(cfg, token_endpoint, data)


def refresh(cfg: OAuthConfig, token_endpoint: str, refresh_token: str) -> OAuthTokens:
    # Public client refresh: client_id + refresh_token only, no secret.
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": cfg.client_id,
    }
    return _token_request(cfg, token_endpoint, data)


def _token_request(cfg: OAuthConfig, token_endpoint: str, data: dict) -> OAuthTokens:
    r = httpx.post(token_endpoint, data=data,
                   headers={"Accept": "application/json",
                            "Content-Type": "application/x-www-form-urlencoded"},
                   timeout=30)
    r.raise_for_status()
    j = r.json()
    return OAuthTokens(
        access_token=j.get("access_token", ""),
        # Epic may or may not rotate the refresh token; keep the old one if absent.
        refresh_token=j.get("refresh_token", data.get("refresh_token", "")),
        token_type=j.get("token_type", "Bearer"),
        expires_at=time.time() + int(j.get("expires_in", 3600)),
        patient=j.get("patient", ""),
        scope=j.get("scope", ""),
        fhir_base=cfg.fhir_base,
    )


# -- token storage --------------------------------------------------------

def save_tokens(t: OAuthTokens) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    OAUTH_PATH.write_text(json.dumps(asdict(t), indent=2))
    os.chmod(OAUTH_PATH, 0o600)


def load_tokens() -> OAuthTokens | None:
    if not OAUTH_PATH.exists():
        return None
    return OAuthTokens(**json.loads(OAUTH_PATH.read_text()))


# -- interactive login (loopback capture) ---------------------------------

class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):  # noqa: N802
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        _CallbackHandler.result = {k: v[0] for k, v in params.items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<h2>Maisa login complete. You can close this tab.</h2>")

    def log_message(self, *a):  # silence
        pass


def interactive_login(cfg: OAuthConfig, open_browser=True) -> OAuthTokens:
    if not cfg.client_id:
        raise SystemExit(
            "No OAuth client_id. Register an app at https://fhir.epic.com (Build Apps),\n"
            "get it enabled for Apotti, then set MAISA_OAUTH_CLIENT_ID and MAISA_FHIR_BASE.")
    meta = discover(cfg.fhir_base)
    authz, token_ep = meta["authorization_endpoint"], meta["token_endpoint"]
    verifier, challenge = make_pkce()
    state = secrets.token_urlsafe(16)
    url = build_authorize_url(cfg, authz, state, challenge)

    host, port = _redirect_hostport(cfg.redirect_uri)
    server = http.server.HTTPServer((host, port), _CallbackHandler)
    _CallbackHandler.result = {}
    threading.Thread(target=server.handle_request, daemon=True).start()

    print("Opening browser for the one-time bank login…")
    if open_browser:
        _open(url)
    else:
        print(url)
    # wait for the redirect
    for _ in range(600):
        if _CallbackHandler.result:
            break
        time.sleep(0.5)
    res = _CallbackHandler.result
    if res.get("state") != state:
        raise SystemExit("state mismatch — aborting")
    if "code" not in res:
        raise SystemExit(f"no code in redirect: {res}")
    tokens = exchange_code(cfg, token_ep, res["code"], verifier)
    save_tokens(tokens)
    return tokens


def ensure_access_token(cfg: OAuthConfig) -> OAuthTokens:
    """Return valid tokens, refreshing with the refresh_token if needed."""
    t = load_tokens()
    if t is None:
        raise SystemExit("No OAuth tokens. Run `maisa oauth login`.")
    if t.expired() and t.refresh_token:
        meta = discover(cfg.fhir_base or t.fhir_base)
        t = refresh(cfg, meta["token_endpoint"], t.refresh_token)
        save_tokens(t)
    return t


def _redirect_hostport(redirect_uri: str) -> tuple[str, int]:
    u = urllib.parse.urlparse(redirect_uri)
    return (u.hostname or "localhost", u.port or 80)


def _open(url: str) -> None:
    # A real browser handles the Suomi.fi strong-auth flow and the redirect.
    import webbrowser
    webbrowser.open(url)
