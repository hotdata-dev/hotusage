#!/usr/bin/env python3
"""Which routes a read-scoped bearer token may authenticate.

The gate is the security-critical half of the skill's API access: a token
minted to answer questions about usage must not reach a route that mutates
anything or hands back another credential (invite links, collector tokens).
Handler.__init__ is BaseHTTPRequestHandler's, which wants a live socket, so
the instance is built without it -- _read_viewer touches nothing else.

Run: python3 server/tests/test_read_token_routes.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
import server  # noqa: E402

VIEWER = {"email": "ada@x.dev", "org_slug": "acme-inc", "org_name": "Acme Inc",
          "database_id": "dbidacmeinc1", "via": "token"}
TOKEN = "read-token-aaaaaaaaaaaaaaaaaaaaaa"


class FakeAuth:
    """Answers for exactly one read token and one cookie session."""

    def __init__(self):
        self.asked = []

    def user_for_token(self, token):
        return dict(VIEWER, via="cookie") if token == "cookie-session" else None

    def read_viewer(self, token):
        self.asked.append(token)
        return dict(VIEWER) if token == TOKEN else None


def handler(cookie=None, bearer=None):
    h = server.Handler.__new__(server.Handler)
    headers = {}
    if cookie:
        headers["Cookie"] = f"hotusage_session={cookie}"
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    h.headers = headers
    h.auth = FakeAuth()
    return h


# Every GET route the server answers, split by whether a read token may have it.
ALLOWED = ["/api/data", "/api/orgs", "/api/status", "/api/session/abc123"]
REFUSED = ["/api/admin/state", "/", "/index.html", "/admin", "/device",
           "/logout", "/api/data/../api/admin/state", "/api/datax",
           "/api/session", "/api/sessions/abc"]


def test_a_read_token_reaches_the_read_routes():
    print("a read token authenticates the read-only GETs:")
    for path in ALLOWED:
        h = handler(bearer=TOKEN)
        viewer = h._read_viewer(path)
        assert viewer and viewer["email"] == "ada@x.dev", (path, viewer)
        assert h.auth.asked == [TOKEN], path
        print(f"    {path}")


def test_a_read_token_reaches_nothing_else():
    print("and nothing else, on any other route:")
    for path in REFUSED:
        h = handler(bearer=TOKEN)
        assert h._read_viewer(path) is None, path
        # the token is not even looked up: the route decides first
        assert h.auth.asked == [], f"{path} consulted the token"
    print(f"    {len(REFUSED)} routes refused, token never consulted")


def test_an_unknown_token_authenticates_nothing():
    print("an unknown or revoked token is refused on the read routes too:")
    for token in ("not-a-real-token-aaaaaaaaaaaa", "", "   "):
        h = handler(bearer=token)
        assert h._read_viewer("/api/data") is None, token
    print("    3 bad tokens refused")


def test_a_cookie_session_still_wins():
    print("a signed-in browser is unaffected:")
    h = handler(cookie="cookie-session")
    for path in ALLOWED + REFUSED:
        viewer = h._read_viewer(path)
        assert viewer and viewer["via"] == "cookie", path
    assert h.auth.asked == [], "a cookie session must not consult bearer tokens"
    print(f"    {len(ALLOWED + REFUSED)} routes, all via the cookie")


def test_a_cookie_and_a_token_together_use_the_cookie():
    """A browser that somehow carries both is still a browser: the session is
    the stronger credential, and preferring it keeps the token from widening
    what a signed-in page can do."""
    print("both credentials present:")
    h = handler(cookie="cookie-session", bearer=TOKEN)
    viewer = h._read_viewer("/api/data")
    assert viewer["via"] == "cookie", viewer
    assert h.auth.asked == [], "the token must not be consulted at all"
    print("    the cookie session is used, the token ignored")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed")
