#!/usr/bin/env python3
"""Handler pieces that decide what a stranger can spend or forge.

Template substitution, the /device error map, the rate limiter's notion of who
is calling, and the dashboard store's caches. All of them are reachable from
outside and none of them needs a socket to exercise: Handler.__init__ belongs
to BaseHTTPRequestHandler and wants a live connection, so the instances here
are built without it and given only the attributes the method under test reads.

Run: python3 server/tests/test_handler_hardening.py
"""
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
import server  # noqa: E402


def page_handler():
    """A Handler whose _send just records what would have gone out."""
    h = server.Handler.__new__(server.Handler)
    sent = {}
    h._send = lambda code, body, ctype, **kw: sent.update(
        code=code, body=body.decode(), ctype=ctype)
    return h, sent


def render(template, subs):
    tmp = tempfile.mkdtemp(prefix="hotusage-test-")
    real = server.STATIC_DIR
    try:
        with open(os.path.join(tmp, "page.html"), "w") as f:
            f.write(template)
        server.STATIC_DIR = tmp
        h, sent = page_handler()
        h._page("page.html", subs)
        return sent["body"]
    finally:
        server.STATIC_DIR = real
        shutil.rmtree(tmp)


def test_a_substituted_value_is_not_substituted_again():
    """The template was walked once per key, so a value landed in the page
    before the later keys were applied -- and was then rewritten by them. The
    hostname on the /device page is chosen by whoever runs the collector, so
    `{{EMAIL}}` as a machine name rendered as the victim's address, in the one
    place a page is asking someone to trust what it says about a machine."""
    print("a value that spells another placeholder:")
    out = render("<p>{{HOST}} wants to sign in as {{EMAIL}}</p>",
                 {"HOST": "{{EMAIL}}", "EMAIL": "ada@x.dev"})
    assert out == "<p>{{EMAIL}} wants to sign in as ada@x.dev</p>", out
    assert out.count("ada@x.dev") == 1, "the value was substituted twice"
    print(f"    {out}")


def test_unknown_placeholders_and_escaping_are_unchanged():
    print("everything else about substitution stands:")
    out = render("<p>{{NOPE}} {{NAME}}</p>", {"NAME": "<script>x</script>"})
    assert "{{NOPE}}" in out, out                      # unknown: left as written
    assert "<script>" not in out and "&lt;script&gt;" in out, out
    print(f"    {out}")


def test_the_device_page_writes_its_own_error_text():
    """A /device link is something anyone can send, and the page it lands on is
    the page that binds a collector to an account. The sentence above the
    Approve button must be one of ours."""
    print("the /device error codes:")
    known = server.Handler._device_error("nomatch")
    assert "type it again" in known, known
    assert server.Handler._device_error("") == ""
    attack = server.Handler._device_error(
        "Your session expired, sign in at evil.example")
    assert "evil.example" not in attack, attack
    assert attack == server.Handler._device_error("also-unknown")
    print(f"    unknown codes all read: {attack}")


def test_the_device_code_parameter_is_validated():
    print("the code carried in the URL:")
    assert server.Handler._device_param("code=abcd-efgh") == "ABCD-EFGH"
    for bad in ("code=%3Cscript%3E", "code=" + "A" * 40, "code=a%0d%0aX", ""):
        assert server.Handler._device_param(bad) == "", bad
    assert server.Handler._device_url("ABCD-EFGH", "nomatch") == \
        "/device?code=ABCD-EFGH&err=nomatch"
    assert server.Handler._device_url("") == "/device"
    print("    4 junk values dropped, a good one survives the round trip")


def test_the_approval_form_never_carries_the_code():
    """The code must be typed, never prefilled.

    A prefilled field turns approval into a single click from any link, and a
    link is something anyone can send: SameSite=Lax permits the top-level GET
    that lands on this page. Typing the code is the only evidence the person
    approving is actually sitting at the machine that asked.

    Asserted against the rendered page rather than the template source, because
    the substitution is what would put the value back."""
    print("the approval form:")
    h, sent = page_handler()
    h._page("device.html", {"STATE": "confirm", "CODE": "ABCD-1234",
                            "HOST": "laptop.local", "SCOPE": "ingest,read",
                            "EMAIL": "ada@x.dev", "ERROR": ""})
    body = sent["body"]
    field = body[body.index('id="user_code"'):]
    field = field[:field.index(">")]
    assert "value=" not in field, field
    assert "ABCD-1234" not in field, field
    assert "required" in field, field
    # the code still has to reach the POST, just not the input
    assert 'action="/device/approve?code=ABCD-1234"' in body
    print("    the input is empty and required; the code rides in the action")


# --- rate limiter -----------------------------------------------------------
def rate_handler(ip="203.0.113.9", forwarded=None):
    h = server.Handler.__new__(server.Handler)
    h.headers = {"X-Forwarded-For": forwarded} if forwarded else {}
    h.client_address = (ip, 54321)
    return h


def fresh_rate():
    server.Handler._rate = {}


def test_x_forwarded_for_is_only_believed_behind_a_proxy():
    """Nothing stops a caller sending the header themselves. Reachable
    directly, trusting it hands the attacker a fresh identity per request and
    every limit in the file becomes decoration."""
    print("a caller minting identities with X-Forwarded-For:")
    fresh_rate()
    server.Handler.TRUST_FORWARDED_FOR = False
    try:
        for i in range(server.Handler.RATE_LIMIT):
            h = rate_handler(forwarded=f"10.0.0.{i}")
            assert not h._rate_limited("register"), i
        h = rate_handler(forwarded="10.0.0.99")
        assert h._rate_limited("register"), "the peer address must be the bucket"
        assert list(server.Handler._rate) == ["register:203.0.113.9"]
        print(f"    {server.Handler.RATE_LIMIT} allowed, then limited, one bucket")

        # behind App Runner the header is written by the proxy, so it is the
        # only thing that distinguishes one caller from another
        fresh_rate()
        server.Handler.TRUST_FORWARDED_FOR = True
        for i in range(server.Handler.RATE_LIMIT + 3):
            h = rate_handler(forwarded=f"198.51.100.1, 10.0.0.{i}")
            assert not h._rate_limited("register"), i
        assert len(server.Handler._rate) == server.Handler.RATE_LIMIT + 3
        print("    trusted: the rightmost entry buckets each caller separately")
    finally:
        server.Handler.TRUST_FORWARDED_FOR = False
        fresh_rate()


def test_the_rate_table_cannot_grow_without_bound():
    """One entry per (bucket, IP) and nothing evicts between purges, so the
    table itself is a memory lever for anyone who can reach the port."""
    print("the limiter's own table:")
    fresh_rate()
    real = server.Handler.MAX_RATE_KEYS
    server.Handler.MAX_RATE_KEYS = 4
    try:
        for i in range(12):
            rate_handler(ip=f"192.0.2.{i}")._rate_limited("register")
            time.sleep(0.002)  # so "oldest" is decidable
        assert len(server.Handler._rate) == 4, server.Handler._rate
        assert "register:192.0.2.0" not in server.Handler._rate, "oldest kept"
        assert "register:192.0.2.11" in server.Handler._rate, "newest evicted"
        print(f"    12 callers, {len(server.Handler._rate)} keys, oldest evicted")
    finally:
        server.Handler.MAX_RATE_KEYS = real
        fresh_rate()


# --- dashboard store caches -------------------------------------------------
class CountingHd:
    """Enough of a HotdataClient for HotdataStore.detail()."""

    db = "dbidcounting"

    def __init__(self, cwd="/repo"):
        self.cwd = cwd
        self.calls = 0

    def rows(self, query):
        self.calls += 1
        if "SELECT cwd" in query:
            return [{"cwd": self.cwd}] if self.cwd else []
        return []


def test_a_session_that_does_not_exist_is_not_remembered():
    """/api/session/<id> is reachable by any read-scoped token and the id is
    whatever the caller types. Caching the misses turned that into unbounded
    growth, one permanent entry per id anyone cared to invent."""
    print("asking for sessions that are not there:")
    hd = CountingHd(cwd="")
    store = server.HotdataStore(hd, ttl=60)
    assert store.detail("nope-1") is None
    assert store.detail("nope-1") is None
    assert store.cache == {}, store.cache
    assert hd.calls == 2, "a miss must be re-read, not served from the cache"
    print("    nothing cached, both lookups went to hotdata")

    hd.cwd = "/repo"
    assert store.detail("sess-1")["cwd"] == "/repo"
    before = hd.calls
    assert store.detail("sess-1")["cwd"] == "/repo"
    assert hd.calls == before, "a real session is still cached"
    print("    a session that exists is cached as before")


def test_the_store_caches_are_capped():
    print("the per-session caches:")
    store = server.HotdataStore(CountingHd(), ttl=60)
    store.MAX_ENTRIES = 6
    for i in range(20):
        store.detail(f"sess-{i}")
        time.sleep(0.002)
    assert len(store.cache) == 6, len(store.cache)
    assert "cwd:sess-0" not in store.cache, "the oldest entry survived"
    assert "detail:sess-19" in store.cache, "the newest was evicted"
    for i in range(20):
        store.encoded_payload(f"key-{i}", lambda: {"ok": True})
        time.sleep(0.002)
    assert len(store.encoded) == 6, len(store.encoded)
    store.invalidate()
    assert store.cache == {} and store.encoded == {}
    print("    both capped at 6, invalidate() still clears everything")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed")
