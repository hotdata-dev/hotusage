#!/usr/bin/env python3
"""End-to-end over a real socket: the prompt that sends a new organization to
install the client before it can use the dashboard.

Cookie-driven rather than bearer-driven, which is what makes these worth having
separately: every other suite here authenticates with a token, so nothing until
now exercised a flow that spans several requests as a browser.

The load-bearing property is not "the dashboard is gated" -- it is that the gate
cannot close over its own exit. Satisfying it means approving a machine, and
approving a machine means reaching /device in a browser, so a gate that covered
/device would be unsatisfiable by construction.

Run: python3 server/tests/test_install_gate.py
"""
import http.client
import os
import sys
import threading
import urllib.parse
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
import server  # noqa: E402
import test_auth_store as fakes  # noqa: E402


class FakeStore:
    def data(self, fresh=False, days=None):
        return {"generatedAt": "", "sessions": [], "daily": []}

    def encoded_payload(self, key, build, fresh=False):
        return b"{}"

    def detail(self, session_id):
        return None

    def invalidate(self):
        pass


class FakeStores:
    def get(self, database_id):
        return FakeStore()


def boot():
    db = fakes.StatefulSysDb()
    auth = server.AuthStore(db, fakes.FakePool())
    server.Handler.auth = auth
    server.Handler.pool = fakes.FakePool()
    server.Handler.stores = FakeStores()
    server.Handler.token = "shared-ingest-token"
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1], auth


HTTPD, PORT, AUTH = boot()


class Browser:
    """Just enough of one: it keeps cookies and does not follow redirects."""

    def __init__(self):
        self.cookies = {}

    def go(self, path, form=None, method=None):
        conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
        headers = {}
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        body = urllib.parse.urlencode(form) if form is not None else None
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method or ("POST" if body is not None else "GET"),
                     path, body, headers)
        resp = conn.getresponse()
        payload = resp.read()
        for key, value in resp.getheaders():
            if key.lower() == "set-cookie":
                name, _, rest = value.partition("=")
                self.cookies[name] = rest.split(";")[0]
        return resp.status, resp.getheader("Location"), payload


def signed_up(email, org):
    """A browser holding a fresh account with an org and nothing reporting.

    The register and setup buckets allow five an hour from one address, and
    every request here comes from 127.0.0.1, so a suite that signs up more than
    five accounts starts silently getting none. It did: the later tests ran with
    no session at all and still passed, because a redirect to /login is not a
    redirect to /install and that was all they checked. The limiter has its own
    suite; here it is noise, so clear it and then assert loudly that this
    browser really is signed in with an org."""
    server.Handler._rate.clear()
    b = Browser()
    b.go("/register", {"email": email, "password": "hunter2hunter2"})
    b.go("/setup", {"org_name": org})
    assert b.cookies.get("hotusage_session"), f"{email} never got a session"
    viewer = AUTH.user_for_token(b.cookies["hotusage_session"])
    assert viewer and viewer.get("org_slug"), f"{email} has no org: {viewer}"
    return b


def test_an_account_with_nothing_reporting_is_sent_to_the_prompt():
    print("a new organization, before any machine has signed in:")
    b = signed_up("new@x.dev", "Acme Inc")
    for path in ("/", "/index.html", "/admin"):
        status, location, _ = b.go(path)
        assert status == 302, (path, status)
        assert location.startswith("/install?next="), (path, location)
        print(f"    {path:<14} -> {location}")


def test_admin_is_gated_through_the_same_helper():
    """/admin performs its own sign-in and org checks above the shared page
    gate, so it is the route that silently keeps working when a new condition
    is added to the other one. It did exactly that during development."""
    print("the route with its own auth block:")
    b = signed_up("admin2@x.dev", "Beta Ltd")
    status, location, _ = b.go("/admin")
    assert status == 302 and location.startswith("/install"), (status, location)
    print(f"    /admin -> {location}")


def test_the_gate_does_not_close_over_its_own_exit():
    """The one property that makes the gate satisfiable at all."""
    print("everything needed to satisfy the prompt stays reachable:")
    b = signed_up("exit@x.dev", "Gamma GmbH")
    _, user_code = AUTH.start_device_auth("laptop", scope="ingest,read")
    # (path, the status it must answer with). Pinned exactly rather than
    # "anything but a bounce to /install": a /device that 302s to /login is
    # just as unsatisfiable, and an assertion that only rules out one wrong
    # answer accepts every other one.
    for path, want in ((f"/device?code={user_code}", 200),
                       ("/static/app.css", 200),
                       ("/install", 200),
                       ("/install/skip?next=%2F", 302)):
        status, location, _ = b.go(path)
        assert status == want, (path, status, location)
        assert not (location or "").startswith("/install?"), (path, location)
        print(f"    {path.split('?')[0]:<22} {status} (reachable, not bounced)")


def test_approving_a_machine_opens_the_dashboard():
    print("once a machine signs in:")
    b = signed_up("ada@x.dev", "Delta SA")
    status, _, body = b.go("/api/install-status")
    assert status == 200 and b'"ready": false' in body, body
    print("    before: " + body.decode())

    device_code, user_code = AUTH.start_device_auth("ada-mbp", scope="ingest,read")
    AUTH.approve_device(user_code, "ada@x.dev")
    AUTH.poll_device(device_code)

    status, _, body = b.go("/api/install-status")
    assert status == 200 and b'"ready": true' in body, body
    print("    after:  " + body.decode())

    status, location, _ = b.go("/")
    assert status == 200, (status, location)
    print("    /              200 (no longer gated)")


def test_skip_lets_someone_past_and_sticks():
    """Chosen over a hard gate so an admin with no machine to install on can
    still reach the page where they invite the people who do have one."""
    print("skip for now:")
    b = signed_up("skipper@x.dev", "Epsilon BV")
    status, location, _ = b.go("/")
    assert status == 302 and location.startswith("/install"), (status, location)

    status, location, _ = b.go("/install/skip?next=%2Fadmin")
    assert status == 302 and location == "/admin", (status, location)
    assert b.cookies.get("hotusage_install_skipped") == "1", b.cookies
    print(f"    /install/skip -> {location}, cookie set")

    for path in ("/", "/admin"):
        status, location, _ = b.go(path)
        assert status == 200, (path, status, location)
    print("    / and /admin both 200 afterwards")


def test_skip_only_ever_returns_to_a_same_site_path():
    """The destination rides in the query string, so it is an open-redirect the
    moment it is trusted; _safe_next is what stops that, and this is the route
    that would show it had stopped being called."""
    print("where skip is willing to send you:")
    b = signed_up("safe@x.dev", "Zeta Oy")
    for hostile in ("//evil.com", "https://evil.com", "/\\evil.com",
                    "/ok\r\nX-Injected: 1"):
        _, location, _ = b.go(
            "/install/skip?next=" + urllib.parse.quote(hostile, safe=""))
        assert location == "/", (hostile, location)
        print(f"    {hostile!r:<28} -> {location}")


def test_the_api_is_not_gated():
    """A read-scoped bearer token is how the installed client asks questions.
    Gating /api/ would break the skill for exactly the machines whose existence
    is the proof the client IS installed."""
    print("the collector's own read token, on an org with no browser session:")
    AUTH.create_account("cli@x.dev", "hunter2hunter2")
    AUTH.create_org_for("cli@x.dev", "Eta AB")
    token = fakes.approved_token(AUTH, "cli@x.dev", scope="read")
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=10)
    conn.request("GET", "/api/status", None, {"Authorization": f"Bearer {token}"})
    resp = conn.getresponse()
    body = resp.read()
    assert resp.status == 200, (resp.status, body)
    assert b"install" not in body.lower(), body
    print(f"    /api/status {resp.status}, answered normally")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed")
