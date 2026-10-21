"""HTTP client that replays a captured Maisa session against the MyChart API.

Epic MyChart's web SPA talks to JSON endpoints under the app base path. This
client wraps httpx with the captured cookies + headers, points requests at the
right origin, and turns "your session died" responses (a 401, or an HTML login
redirect served where JSON was expected) into a typed error the CLI can act on.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx

from .session import Session, save

# Epic encodes opaque record IDs in URL-safe base64 with a custom escaping:
#   $ -> -24   = -> -3D   + -> -2B   / -> -2F
# These IDs appear as "WP-24....-3D-3D-24....-3D". They must be passed back
# verbatim (already-escaped) as identifiers; no decode is needed to use them,
# but this helps when eyeballing them.
_CSRF_RE = re.compile(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"')


class SessionExpired(RuntimeError):
    """The server no longer accepts the captured session; strong re-auth needed."""


# Headers the Epic MyChart SPA sends on its XHR calls. Sending these makes our
# requests look like the in-app fetches rather than a top-level navigation,
# which matters: MyChart returns JSON to XHR-style requests and an HTML login
# page to navigations when the session is gone.
def _spa_headers(session: Session) -> dict[str, str]:
    h = {
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{session.app_url}/",
        "Origin": session.origin,
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        ),
    }
    if session.csrf_token:
        # Epic accepts the anti-forgery token under this header name.
        h["__RequestVerificationToken"] = session.csrf_token
        h["verificationToken"] = session.csrf_token
    if session.access_token:
        h["Authorization"] = f"Bearer {session.access_token}"
    h.update(session.extra_headers)
    return h


class MaisaClient:
    def __init__(self, session: Session, timeout: float = 30.0):
        self.session = session
        self._http = httpx.Client(
            base_url=session.origin,
            cookies=session.cookies,
            headers=_spa_headers(session),
            timeout=timeout,
            follow_redirects=False,  # a redirect to /Authentication/Login == expired
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "MaisaClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- low level --------------------------------------------------------

    def request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        """Issue a request under the app base path (path is app-relative)."""
        if path.startswith("http"):
            url = path
        elif path.startswith("/"):
            url = f"{self.session.origin}{path}"
        else:
            url = f"{self.session.app_url}/{path}"
        resp = self._http.request(method, url, **kw)
        self._check_alive(resp)
        # Refresh any rotated cookies the server handed back.
        if resp.cookies:
            self.session.cookies.update(dict(resp.cookies))
        self.session.last_ok_at = time.time()
        return resp

    def get(self, path: str, **kw: Any) -> httpx.Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> httpx.Response:
        return self.request("POST", path, **kw)

    def get_json(self, path: str, **kw: Any) -> Any:
        return self.request("GET", path, **kw).json()

    def post_json(self, path: str, **kw: Any) -> Any:
        return self.request("POST", path, **kw).json()

    # -- CSRF -------------------------------------------------------------

    def ensure_csrf(self, force: bool = False) -> str:
        """Fetch and cache the MyChart anti-forgery token (needed for POSTs)."""
        if self.session.csrf_token and not force:
            return self.session.csrf_token
        resp = self._http.get(f"{self.session.app_url}/Home/CSRFToken")
        self._check_alive(resp)
        m = _CSRF_RE.search(resp.text)
        if not m:
            # Since Oct 2026 /Home/CSRFToken can come back empty; the token is
            # also in the Home page's hidden __CSRFContainer.
            resp = self._http.get(f"{self.session.app_url}/Home")
            self._check_alive(resp)
            m = _CSRF_RE.search(resp.text)
        if not m:
            raise RuntimeError("Could not parse CSRF token from /Home/CSRFToken")
        token = m.group(1)
        self.session.csrf_token = token
        self._http.headers["__RequestVerificationToken"] = token
        self._http.headers["verificationToken"] = token
        return token

    def json_api(self, path: str, payload: dict | None = None) -> Any:
        """POST a JSON body to a React `/maisa/api/...` endpoint with CSRF.

        These endpoints take an application/json body and the anti-forgery token
        as a header; they return JSON. Returns the decoded response."""
        self.ensure_csrf()
        url = path if path.startswith("http") else f"{self.session.app_url}/{path.lstrip('/')}"
        resp = self._http.post(
            url,
            json=payload if payload is not None else {},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "__RequestVerificationToken": self.session.csrf_token or "",
                "verificationToken": self.session.csrf_token or "",
            },
        )
        self._check_alive(resp)
        self.session.last_ok_at = time.time()
        if resp.cookies:
            self.session.cookies.update(dict(resp.cookies))
        ctype = resp.headers.get("content-type", "")
        if "json" in ctype:
            return resp.json()
        # Some endpoints return a bare JSON string/empty on success.
        txt = resp.text.strip()
        if txt.startswith(('"', "{", "[")):
            import json as _json
            return _json.loads(txt)
        return txt

    def post_form(self, path: str, data: dict | None = None, **kw: Any) -> httpx.Response:
        """POST application/x-www-form-urlencoded with the CSRF token attached.

        Epic's MVC endpoints expect the anti-forgery token both as a header and
        as a `__RequestVerificationToken` form field."""
        self.ensure_csrf()
        body = dict(data or {})
        body.setdefault("__RequestVerificationToken", self.session.csrf_token)
        return self.request("POST", path, data=body, **kw)

    # -- expiry detection -------------------------------------------------

    def _check_alive(self, resp: httpx.Response) -> None:
        if resp.status_code in (401, 403):
            raise SessionExpired(
                f"{resp.status_code} from {resp.request.url}. Session rejected; "
                "run `maisa login` to re-authenticate."
            )
        loc = resp.headers.get("location", "")
        if resp.status_code in (301, 302, 303, 307) and (
            "Authentication/Login" in loc or "Saml" in loc or "tunnistautuminen" in loc
        ):
            raise SessionExpired(
                "Server redirected to the login flow; session expired. "
                "Run `maisa login` to re-authenticate."
            )
        ctype = resp.headers.get("content-type", "")
        if "text/html" in ctype and "Authentication/Login" in resp.text[:4000]:
            raise SessionExpired(
                "Got the login page instead of data; session expired. "
                "Run `maisa login` to re-authenticate."
            )

    def persist(self) -> None:
        save(self.session)
