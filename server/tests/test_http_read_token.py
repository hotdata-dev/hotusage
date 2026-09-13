#!/usr/bin/env python3
"""End-to-end over a real socket: what a read token can and cannot do.

Drives the actual Handler through ThreadingHTTPServer, with a real AuthStore
over the stateful fake system database from test_auth_store and a stand-in for
the usage store. That covers the do_GET/do_POST wiring the route-gate unit test
cannot: a token that authenticates in _read_viewer but 500s in a handler, or an
/ingest path that accepts a credential minted for reading.

Run: python3 server/tests/test_http_read_token.py
"""
import gzip
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
import server  # noqa: E402
import test_auth_store as fakes  # noqa: E402

PAYLOAD = {"generatedAt": "2026-09-13T10:00:00+00:00", "sessions": [],
           "daily": [], "source": "fake"}
DETAIL = {"id": "sess-1", "detail": [{"t": "2026-09-13T10:00:00+00:00",
                                      "ctx": 1000, "out": 50}], "cwd": "/repo"}


class FakeStore:
    def data(self, fresh=False, days=None):
        return dict(PAYLOAD, windowDays=days)

    def encoded_payload(self, key, build, fresh=False):
        return gzip.compress(json.dumps(build()).encode())

    def detail(self, session_id):
        return dict(DETAIL, id=session_id) if session_id == "sess-1" else None

    def invalidate(self):
        pass


class FakeStores:
    def get(self, database_id):
        return FakeStore()


def boot():
    """A server on a loopback port, plus the read token to talk to it with."""
    db = fakes.StatefulSysDb()
    auth = server.AuthStore(db, fakes.FakePool())
    auth.create_account("ada@x.dev", "hunter2hunter2")
    auth.create_org_for("ada@x.dev", "Acme Inc")
    read_token = fakes.approved_token(auth, "ada@x.dev", scope="read")
    ingest_token = fakes.approved_token(auth, "ada@x.dev", hostname="desktop")

    server.Handler.auth = auth
    server.Handler.pool = fakes.FakePool()
    server.Handler.stores = FakeStores()
    server.Handler.token = "shared-ingest-token"

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    return httpd, base, read_token, ingest_token


def call(base, path, token=None, method="GET", body=None, gzip_ok=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    if gzip_ok:
        # urllib asks for identity by default; the skill asks for gzip, and
        # that is a different branch of /api/data (encoded_payload)
        req.add_header("Accept-Encoding", "gzip")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read()


HTTPD, BASE, READ, INGEST = boot()


def test_the_read_routes_answer_a_read_token():
    print("a read token gets real answers:")
    for path, check in (
        ("/api/status", lambda d: d["org"] == "acme-inc"),
        ("/api/data?days=7", lambda d: d["windowDays"] == 7
         and d["viewer"]["email"] == "ada@x.dev"),
        ("/api/data?days=all", lambda d: d["windowDays"] is None),
        ("/api/orgs", lambda d: d["active"] == "acme-inc"),
        ("/api/session/sess-1", lambda d: d["cwd"] == "/repo"),
    ):
        status, raw = call(BASE, path, READ)
        assert status == 200, (path, status, raw[:200])
        assert check(json.loads(raw)), (path, raw[:200])
        print(f"    200 {path}")


def test_the_gzip_branch_is_the_one_the_skill_uses():
    """/api/data has two paths: a gzipped cached payload for clients that
    accept it, and plain JSON otherwise. The skill always takes the first."""
    print("the gzipped payload path:")
    status, raw = call(BASE, "/api/data?days=30", READ, gzip_ok=True)
    assert status == 200, (status, raw[:200])
    data = json.loads(raw)          # call() decompressed it
    assert data["windowDays"] == 30, data
    assert data["viewer"]["email"] == "ada@x.dev", data
    print("    200, gzipped, decodes to the same payload")


def test_a_missing_session_is_404_not_a_crash():
    status, _ = call(BASE, "/api/session/nope", READ)
    assert status == 404, status
    print("a session that does not exist: 404")


def test_the_read_token_cannot_reach_the_admin_state():
    print("everything else refuses it:")
    for path in ("/api/admin/state", "/api/data/../api/admin/state"):
        status, raw = call(BASE, path, READ)
        assert status in (401, 404), (path, status, raw[:200])
        print(f"    {status} {path}")


def test_the_read_token_cannot_ingest():
    """The whole point of the scope: a credential sitting on a laptop to answer
    questions must not be able to write usage rows as its owner."""
    print("and it cannot report usage:")
    body = {"user_email": "ada@x.dev", "hostname": "laptop", "sessions": []}
    status, raw = call(BASE, "/ingest", READ, method="POST", body=body)
    assert status == 401, (status, raw[:200])
    print(f"    {status} POST /ingest with a read token")
    # the ingest token minted alongside it still works, so this is the scope
    # talking and not a broken ingest path
    status, raw = call(BASE, "/ingest", INGEST, method="POST", body=body)
    assert status == 200, (status, raw[:200])
    print(f"    {status} POST /ingest with an ingest token")


def test_an_unauthenticated_api_call_is_401():
    for path in ("/api/data", "/api/status", "/api/orgs"):
        status, _ = call(BASE, path)
        assert status == 401, (path, status)
    print("no credential at all: 401 on every API route")


def test_the_collector_signout_revokes_a_read_token_too():
    print("a skill can sign itself out:")
    token = fakes.approved_token(server.Handler.auth, "ada@x.dev", scope="read")
    assert call(BASE, "/api/status", token)[0] == 200
    status, raw = call(BASE, "/api/collector/signout", token, method="POST",
                       body={})
    assert status == 200 and json.loads(raw)["revoked"] is True, raw[:200]
    assert call(BASE, "/api/status", token)[0] == 401, "it must stop working"
    print("    signed out, and the token stops working at once")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    try:
        for t in tests:
            t()
        print(f"\n{len(tests)} tests passed")
    finally:
        HTTPD.shutdown()
