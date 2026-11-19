"""Log in with Lichess: OAuth 2 authorization code + PKCE, no client secret or app registration.

`LoginFlow.start()` opens a one-shot HTTP listener on 127.0.0.1 and returns the
URL to open in the browser. The user approves on lichess.org, the browser comes
back to the listener, and `wait()` exchanges the code for a token. The user's
password never passes through us.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse

from .. import http

AUTHORIZE = "https://lichess.org/oauth"
TOKEN = "https://lichess.org/api/token"
CLIENT_ID = "knightly-review"

PAGE = """<!doctype html><meta charset=utf-8><title>Knightly Review</title>
<body style="font-family:sans-serif;text-align:center;margin-top:4em">
<h2>{title}</h2><p>{text}</p></body>"""


class LoginError(Exception):
    pass


class LoginFlow:
    def __init__(self, scopes: tuple[str, ...] = ()):
        self.verifier = secrets.token_urlsafe(64)
        self.state = secrets.token_urlsafe(16)
        self.scopes = scopes
        self._done = threading.Event()
        self._code: Optional[str] = None
        self._error: Optional[str] = None
        self._server: Optional[HTTPServer] = None

    @property
    def redirect_uri(self) -> str:
        assert self._server
        return f"http://127.0.0.1:{self._server.server_port}/"

    def start(self) -> str:
        flow = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                q = parse_qs(urlparse(self.path).query)
                if "state" not in q and "error" not in q:
                    self.send_response(404)
                    self.end_headers()
                    return
                if q.get("state", [""])[0] != flow.state:
                    flow._error = "state mismatch"
                elif "error" in q:
                    flow._error = q.get("error_description", q["error"])[0]
                else:
                    flow._code = q.get("code", [""])[0]
                ok = flow._code is not None
                body = PAGE.format(
                    title="Logged in" if ok else "Login failed",
                    text="You can close this tab and go back to Anki." if ok else (flow._error or ""),
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
                flow._done.set()

            def log_message(self, *a):
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self.verifier.encode()).digest()).rstrip(b"=").decode()
        return AUTHORIZE + "?" + urlencode(
            {
                "response_type": "code",
                "client_id": CLIENT_ID,
                "redirect_uri": self.redirect_uri,
                "code_challenge_method": "S256",
                "code_challenge": challenge,
                "scope": " ".join(self.scopes),
                "state": self.state,
            }
        )

    def wait(self, timeout: float = 300, cancel: Optional[threading.Event] = None) -> str:
        """Block until the browser comes back; return the access token."""
        try:
            step = 0.25
            waited = 0.0
            while not self._done.wait(step):
                waited += step
                if waited >= timeout:
                    raise LoginError("timed out waiting for the browser")
                if cancel is not None and cancel.is_set():
                    raise http.Cancelled()
            if not self._code:
                raise LoginError(self._error or "no code returned")
            r = http.session().post(
                TOKEN,
                data={
                    "grant_type": "authorization_code",
                    "code": self._code,
                    "code_verifier": self.verifier,
                    "redirect_uri": self.redirect_uri,
                    "client_id": CLIENT_ID,
                },
                timeout=30,
            )
            if r.status_code != 200:
                raise LoginError(f"token exchange failed ({r.status_code}): {r.text[:200]}")
            return r.json()["access_token"]
        finally:
            self.close()

    def close(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
