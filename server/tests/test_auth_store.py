#!/usr/bin/env python3
"""AuthStore tests that need no hotdata account.

server.py imports no hotdata at module level -- the client is built lazily
inside HotdataClient -- so AuthStore can be driven against a fake system
database that records every query and every write. That is enough to assert
the things worth asserting here: what is refused, what is written when it is
not, and which caches are dropped.

Run: python3 server/tests/test_auth_store.py
"""
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
import server  # noqa: E402


class FakeSysDb:
    """Stands in for HotdataClient against the system database."""

    def __init__(self, orgs=(), db="dbidsystem00000000000000"):
        self.db = db
        self.orgs = [dict(o) for o in orgs]
        self.loads = []
        self.lock = threading.Lock()

    def rows(self, query):
        assert "orgs" in query, query   # the only table these tests read
        return [dict(o) for o in self.orgs]

    def load(self, table, rows, mode):
        self.loads.append((table, [dict(r) for r in rows], mode))


class FakePool:
    """A ClientPool whose databases can be made unreachable."""

    def __init__(self, reachable=True):
        self.reachable = reachable
        self.probed = []
        self.prepared = []

    def get(self, database_id):
        pool = self

        class Client:
            def sql(self, query):
                if not pool.reachable:
                    raise RuntimeError("no such database")
                pool.probed.append(database_id)
                return []

            def ensure_schema_and_tables(self, tables):
                pool.prepared.append(database_id)

        return Client()


ORGS = [
    {"slug": "hotdata", "name": "Hotdata", "database_id": "dbidhotdata0000000000000",
     "created_at": "2025-11-02"},
    {"slug": "acme", "name": "Acme", "database_id": "dbidacme111111111111111",
     "created_at": "2026-04-04"},
]


def store(reachable=True):
    db, pool = FakeSysDb(ORGS), FakePool(reachable)
    auth = server.AuthStore(db, pool)
    # populated so the test can prove they are dropped
    auth.org_cache["hotdata"] = ({"slug": "hotdata"}, 0)
    auth.route_cache["e@x.dev"] = (("hotdata", "dbidhotdata0000000000000"), 0)
    auth.token_cache["tok"] = ({"email": "e@x.dev"}, 9e9)
    return auth, db, pool


def refuses(fn, needle):
    try:
        fn()
    except ValueError as e:
        assert needle in str(e).lower(), f"wanted {needle!r} in {str(e)!r}"
        print(f"    refused: {e}")
        return
    raise AssertionError(f"expected a refusal mentioning {needle!r}")


def test_set_org_database_refusals():
    print("set_org_database refuses:")
    auth, db, pool = store()
    refuses(lambda: auth.set_org_database("hotdata", "nonsense"), "does not look like")
    refuses(lambda: auth.set_org_database("hotdata", ""), "does not look like")
    refuses(lambda: auth.set_org_database("hotdata", "dbid$$$$$$$$$$$$$$$$"),
            "does not look like")
    # the accounts database is well-formed and claimed by no org
    refuses(lambda: auth.set_org_database("hotdata", db.db), "system database")
    refuses(lambda: auth.set_org_database("ghost", "dbidbrandnew00000000000"),
            "no such organization")
    # one database per org is the whole isolation model
    refuses(lambda: auth.set_org_database("hotdata", "dbidacme111111111111111"),
            "already reports")
    assert db.loads == [], "a refusal must never write"
    assert pool.prepared == [], "a refusal must never touch a database"


def test_unreachable_target_is_not_recorded():
    print("an unreachable database fails before the switch is recorded:")
    auth, db, pool = store(reachable=False)
    refuses(lambda: auth.set_org_database("hotdata", "dbidbrandnew00000000000"),
            "could not reach")
    assert db.loads == [], "the org must keep the database it had"


def test_no_op():
    print("setting the database it already has changes nothing:")
    auth, db, pool = store()
    assert auth.set_org_database("hotdata", "dbidhotdata0000000000000") == \
        "dbidhotdata0000000000000"
    assert db.loads == [] and pool.prepared == []
    assert auth.org_cache, "a no-op must not drop the caches"
    print("    no write, caches intact")


def test_success():
    print("a good id is probed, prepared, recorded, and clears the caches:")
    auth, db, pool = store()
    previous = auth.set_org_database("hotdata", "dbidbrandnew00000000000")
    assert previous == "dbidhotdata0000000000000", previous
    assert pool.probed == ["dbidbrandnew00000000000"], pool.probed
    assert pool.prepared == ["dbidbrandnew00000000000"], pool.prepared
    table, rows, mode = db.loads[0]
    assert (table, mode) == ("orgs", "upsert"), (table, mode)
    # an upsert carries the whole row: a partial one would blank name/created_at
    assert rows == [{"slug": "hotdata", "name": "Hotdata",
                     "database_id": "dbidbrandnew00000000000",
                     "created_at": "2025-11-02"}], rows
    # a viewer dict carries database_id for up to 30 days, and a route carries
    # it for ingest: either would keep using the database just replaced
    assert auth.org_cache == {}, "org_cache"
    assert auth.route_cache == {}, "route_cache"
    assert auth.token_cache == {}, "token_cache"
    print(f"    {previous} -> dbidbrandnew00000000000, three caches cleared")


class StatefulSysDb:
    """A system database that reads back what it wrote.

    Enough of a SQL engine for the signup path: table name, equality filters,
    and the one LEFT JOIN (users -> orgs) that get_user and user_for_token
    rely on to hand back org_name and database_id.
    """

    KEYS = {"users": ("email",), "orgs": ("slug",), "org_admins": ("email", "org_slug"),
            "org_memberships": ("email", "org_slug"),
            "device_codes": ("device_code",), "device_scopes": ("device_code",),
            "collector_tokens": ("token",), "token_scopes": ("token",),
            "collector_token_usage": ("token",), "auth_sessions": ("token",),
            "system_admins": ("email",)}

    def __init__(self, db="dbidsystem00000000000000"):
        self.db = db
        self.tables = {t: [] for t in self.KEYS}
        self.lock = threading.Lock()
        self.created = []

    def rows(self, query):
        import re
        flat = " ".join(query.split())
        table = re.search(r"FROM \S+\.public\.(\w+)", flat).group(1)
        out = [dict(r) for r in self.tables[table]]
        if table == "users" and "LEFT JOIN" in flat:   # get_user / user_for_token
            by_slug = {o["slug"]: o for o in self.tables["orgs"]}
            for r in out:
                org = by_slug.get(r.get("org_slug"))
                r["org_name"] = org["name"] if org else None
                r["database_id"] = org["database_id"] if org else None
        for col, val in re.findall(r"([\w.]+) = '([^']*)'", flat):
            col = col.split(".")[-1]
            out = [r for r in out if str(r.get(col, "")) == val]
        return out

    def load(self, table, rows, mode):
        for r in rows:
            key = self.KEYS[table]
            self.tables[table] = [x for x in self.tables[table]
                                  if tuple(x.get(k) for k in key) != tuple(r.get(k) for k in key)]
            if mode != "delete":
                self.tables[table].append(dict(r))

    def create_org_database(self, slug):
        # a fresh id per CALL, not per slug: the real API provisions a new
        # database each time, and that difference is what lets the loser of a
        # slug race notice it lost
        self.n = getattr(self, "n", 0) + 1
        db = f"dbid{''.join(c for c in slug if c.isalnum())[:14]}{self.n}"
        self.created.append(db)
        return db


def signup_store():
    db = StatefulSysDb()
    return server.AuthStore(db, FakePool()), db


def test_register_creates_an_account_with_no_org():
    print("registration creates the account alone:")
    auth, db = signup_store()
    auth.create_account("Ada@x.dev", "hunter2hunter2")
    user, = db.tables["users"]
    assert user["email"] == "ada@x.dev", user       # normalised
    assert user["org_slug"] == "", user             # no org, by design
    assert db.tables["orgs"] == [], "registration must provision no database"
    assert db.tables["org_memberships"] == [] and db.tables["org_admins"] == []
    assert db.created == [], db.created
    print("    ada@x.dev, org_slug='', no database provisioned")

    # an org-less account has nothing to backfill, and "" is not a membership
    assert auth.memberships("ada@x.dev") == set()
    assert db.tables["org_memberships"] == [], db.tables["org_memberships"]
    # ingest has nowhere to route until they have one
    assert auth.route_for_email("ada@x.dev") is None
    print("    no membership backfilled, ingest has no route")

    refuses(lambda: auth.create_account("ada@x.dev", "otherpassword"),
            "already registered")


def test_org_step_makes_them_first_member_and_admin():
    print("the org step, taken after the account exists:")
    auth, db = signup_store()
    auth.create_account("ada@x.dev", "hunter2hunter2")
    slug = auth.create_org_for("ada@x.dev", "Acme Inc")
    assert slug == "acme-inc", slug
    assert db.tables["orgs"][0]["database_id"] == "dbidacmeinc1", db.tables["orgs"]
    assert [m["org_slug"] for m in db.tables["org_memberships"]] == ["acme-inc"]
    assert [a["email"] for a in db.tables["org_admins"]] == ["ada@x.dev"]
    assert db.tables["users"][0]["org_slug"] == "acme-inc", "it must become active"
    assert auth.route_for_email("ada@x.dev") == ("acme-inc", "dbidacmeinc1")
    print(f"    {slug}: database provisioned, member, admin, active, routable")


def test_org_step_refusals():
    print("the org step refuses:")
    auth, db = signup_store()
    auth.create_account("ada@x.dev", "hunter2hunter2")
    for bad in ("", "a", "   ", "!!"):
        refuses(lambda b=bad: auth.create_org_for("ada@x.dev", b), "at least 2")
    refuses(lambda: auth.create_org_for("nobody@x.dev", "Ghost Co"), "no such user")
    auth.create_org_for("ada@x.dev", "Acme Inc")
    auth.create_account("bob@x.dev", "hunter2hunter2")
    # joining an existing org goes through an invite, never through its name
    refuses(lambda: auth.create_org_for("bob@x.dev", "Acme Inc"), "already exists")


def test_joining_an_org_does_not_grant_admin():
    """Also the invite path for an org-less account: both accept_invite
    branches funnel an existing account through join_org."""
    print("a second member joins without becoming an admin:")
    auth, db = signup_store()
    auth.create_account("ada@x.dev", "hunter2hunter2")
    auth.create_org_for("ada@x.dev", "Acme Inc")
    auth.create_account("bob@x.dev", "hunter2hunter2")
    auth.join_org("bob@x.dev", "acme-inc")
    assert sorted(a["email"] for a in db.tables["org_admins"]) == ["ada@x.dev"]
    bob = [u for u in db.tables["users"] if u["email"] == "bob@x.dev"][0]
    assert bob["org_slug"] == "acme-inc", bob
    assert auth.memberships("bob@x.dev") == {"acme-inc"}
    print("    bob is a member, admins unchanged:",
          [a["email"] for a in db.tables["org_admins"]])


def test_losing_a_slug_race_does_not_join_the_winners_org():
    """Two creations of one name can both pass the existence check. Both then
    provision, and the orgs upsert makes the LAST writer the owner of the slug.
    The earlier writer has to notice and refuse -- with ensure_org it could not,
    because ensure_org hands back the existing row's database id, which would
    equal what the loser was just given.
    """
    print("the loser of a slug race is refused, not quietly admitted:")
    auth, db = signup_store()
    auth.create_account("ada@x.dev", "hunter2hunter2")
    auth.create_account("mallory@x.dev", "hunter2hunter2")

    # ada provisions; mallory's concurrent provision lands on top of her row
    real_provision = auth.provision_org
    raced = {"done": False}

    def provision_then_get_raced(slug, name=None):
        mine = real_provision(slug, name)
        if not raced["done"]:
            raced["done"] = True
            real_provision(slug, "Acme Inc")   # mallory, a moment later
        return mine

    auth.provision_org = provision_then_get_raced
    refuses(lambda: auth.create_org_for("ada@x.dev", "Acme Inc"), "already exists")
    auth.provision_org = real_provision

    assert db.tables["orgs"][0]["database_id"] == db.created[-1], \
        "the last writer owns the slug"
    assert db.tables["org_memberships"] == [], db.tables["org_memberships"]
    assert db.tables["org_admins"] == [], db.tables["org_admins"]
    assert db.tables["users"][0]["org_slug"] == "", "ada stays org-less"
    print("    no membership, no admin, no active org in a stranger's org")


def approved_token(auth, email, scope=None, hostname="laptop"):
    """Drive one device sign-in end to end and return the minted token."""
    kwargs = {} if scope is None else {"scope": scope}
    device_code, user_code = auth.start_device_auth(hostname, **kwargs)
    auth.approve_device(user_code, email)
    got = auth.poll_device(device_code)
    assert got, "an approved device must hand its token over"
    assert got[0] == email, got
    return got[1]


def scoped_store():
    auth, db = signup_store()
    auth.create_account("ada@x.dev", "hunter2hunter2")
    auth.create_org_for("ada@x.dev", "Acme Inc")
    return auth, db


def test_a_collector_token_still_means_ingest():
    print("a sign-in that names no scope mints an ingest token:")
    auth, db = scoped_store()
    token = approved_token(auth, "ada@x.dev")
    assert db.tables["token_scopes"] == [], "the default must write no scope row"
    assert auth.token_scopes(token) == {"ingest"}
    assert auth.collector_token_user(token) == "ada@x.dev"
    # which is exactly what a token minted before scopes existed looks like
    assert auth.read_viewer(token) is None, "an ingest token must not read"
    print("    no scope row, ingests, cannot read")


def test_a_read_token_reads_and_cannot_ingest():
    print("a read sign-in mints a token that can only read:")
    auth, db = scoped_store()
    token = approved_token(auth, "ada@x.dev", scope="read")
    assert auth.token_scopes(token) == {"read"}
    # /ingest asks for an ingest token by default, and must not get one here
    assert auth.collector_token_user(token) is None, "a read token must not ingest"
    viewer = auth.read_viewer(token)
    assert viewer["email"] == "ada@x.dev", viewer
    assert viewer["org_slug"] == "acme-inc", viewer
    assert viewer["database_id"] == "dbidacmeinc1", viewer
    print(f"    reads as {viewer['email']} in {viewer['org_slug']}, cannot ingest")


def test_scope_is_fixed_when_the_device_starts_not_when_it_is_approved():
    print("the approval page's scope is the one the device asked for:")
    auth, db = scoped_store()
    device_code, user_code = auth.start_device_auth("laptop", scope="read")
    assert auth.device_scopes(device_code) == {"read"}
    auth.approve_device(user_code, "ada@x.dev")
    token = auth.poll_device(device_code)[1]
    assert auth.token_scopes(token) == {"read"}
    # the device row and its scope are consumed together
    assert db.tables["device_codes"] == [] and db.tables["device_scopes"] == []
    print("    read asked, read granted, both rows consumed")


def test_unknown_scopes_are_refused_before_anything_is_written():
    print("an unknown scope is refused:")
    auth, db = scoped_store()
    before = len(db.tables["device_codes"])
    refuses(lambda: auth.start_device_auth("laptop", scope="admin"), "unknown scope")
    refuses(lambda: auth.start_device_auth("laptop", scope="ingest,admin"),
            "unknown scope")
    assert len(db.tables["device_codes"]) == before, "a refusal must mint no device"


def test_an_unknown_stored_scope_grants_nothing():
    """A token minted by a newer build and then rolled back carries a scope
    this build cannot read. Falling back to the default would silently promote
    a read-only credential into one that can write usage rows."""
    print("a token scope this build does not understand:")
    auth, db = scoped_store()
    token = approved_token(auth, "ada@x.dev", scope="read")
    # what a newer build would have written
    db.load("token_scopes", [{"token": token, "scope": "read,export"}], "upsert")
    auth.forget_token(token)
    assert auth.token_scopes(token) == frozenset(), "it must grant nothing"
    assert auth.collector_token_user(token) is None, "and must not ingest"
    assert auth.read_viewer(token) is None, "nor read"
    # but it stays listed, and revocable, from the admin page
    assert auth.describe_scopes("read,export") == ["export", "read"]
    assert auth.revoke_collector_token(token) == "ada@x.dev"
    print("    denied everywhere, still visible and revocable")


def test_describe_scopes_never_raises():
    """It feeds the admin page, which is the one place a strange token can be
    revoked from -- a 500 there would strand it."""
    print("the admin page tolerates any stored value:")
    for raw in ("read,export", "", None, "NONSENSE", "ingest, read", ",,,"):
        got = server.AuthStore.describe_scopes(raw)
        assert isinstance(got, list), (raw, got)
    assert server.AuthStore.describe_scopes("ingest, read") == ["ingest", "read"]
    print("    6 values described without raising")


def test_an_unknown_device_scope_fails_the_approval():
    """The opposite choice from a token: someone is looking at a page that just
    told them what they are granting, so mint nothing rather than something
    different from what it said."""
    print("a device asking for something unknown:")
    auth, db = scoped_store()
    device_code, user_code = auth.start_device_auth("laptop", scope="read")
    db.load("device_scopes",
            [{"device_code": device_code, "scope": "read,export"}], "upsert")
    refuses(lambda: auth.approve_device(user_code, "ada@x.dev"), "unknown scope")
    assert db.tables["collector_tokens"] == [], "no credential may be minted"


def test_one_approval_can_grant_both():
    """The client installs the background sync and the skill together, so it
    asks for both at once rather than making the person approve two devices."""
    print("a single sign-in granting ingest and read:")
    auth, db = scoped_store()
    token = approved_token(auth, "ada@x.dev", scope="ingest,read")
    assert auth.token_scopes(token) == {"ingest", "read"}
    # both capabilities, on the one credential
    assert auth.collector_token_user(token) == "ada@x.dev", "it must ingest"
    assert auth.read_viewer(token)["org_slug"] == "acme-inc", "and read"
    # stored in one canonical spelling, whatever order it was asked for
    assert db.tables["token_scopes"][0]["scope"] == "ingest,read"
    other = approved_token(auth, "ada@x.dev", scope="read,ingest")
    assert auth.token_scopes(other) == {"ingest", "read"}
    print("    ingest + read on one token, stored as 'ingest,read'")


def test_scope_parsing_is_order_and_whitespace_insensitive():
    print("scope strings are normalised:")
    p = server.AuthStore.parse_scopes
    assert p("read") == {"read"}
    assert p(" ingest , read ") == {"ingest", "read"}
    assert p("read,read") == {"read"}
    assert p(["ingest", "read"]) == {"ingest", "read"}
    # empty means the default, which is what a client predating scopes sends
    assert p("") == {"ingest"} and p(None) == {"ingest"} and p([]) == {"ingest"}
    assert server.AuthStore.join_scopes({"read", "ingest"}) == "ingest,read"
    print("    order, spacing, duplicates and emptiness all handled")


def test_a_failed_scope_write_mints_no_usable_token():
    """The scope is written before the credential, so a failure there leaves an
    orphan scope row rather than a live ingest token nobody asked for."""
    print("a scope write that fails mints nothing:")
    auth, db = scoped_store()
    device_code, user_code = auth.start_device_auth("laptop", scope="read")
    real_load = db.load

    def fail_on_token_scopes(table, rows, mode):
        if table == "token_scopes" and mode == "upsert":
            raise RuntimeError("hotdata is cold")
        real_load(table, rows, mode)

    db.load = fail_on_token_scopes
    try:
        auth.approve_device(user_code, "ada@x.dev")
        raise AssertionError("expected the scope write to propagate")
    except RuntimeError as e:
        print(f"    raised: {e}")
    finally:
        db.load = real_load
    assert db.tables["collector_tokens"] == [], \
        "no credential may survive a failed scope write"
    # and the device is still pending, so the person can simply try again
    assert auth.poll_device(device_code) is None
    print("    no token written, the device is still pending")


def test_renaming_the_org_drops_cached_read_viewers():
    print("a rename reaches read tokens too:")
    auth, db = scoped_store()
    token = approved_token(auth, "ada@x.dev", scope="read")
    assert auth.read_viewer(token)["org_name"] == "Acme Inc"
    auth.rename_org("acme-inc", "Acme Corp")
    assert auth.read_viewer(token)["org_name"] == "Acme Corp", \
        "the cached viewer carried the old name"
    print("    Acme Inc -> Acme Corp, seen immediately")


def test_revoking_a_read_token_takes_effect_at_once():
    print("revoking a read token drops the row and the cached viewer:")
    auth, db = scoped_store()
    token = approved_token(auth, "ada@x.dev", scope="read")
    assert auth.read_viewer(token), "it reads before the revoke"
    assert auth.revoke_collector_token(token) == "ada@x.dev"
    assert db.tables["collector_tokens"] == [], "the credential is gone"
    assert db.tables["token_scopes"] == [], "and so is its scope row"
    # a cached viewer outliving the credential is the bug this guards
    assert auth.read_viewer(token) is None, "it must stop reading immediately"
    print("    token, scope and cache all cleared")


def test_removing_a_member_revokes_their_read_token_too():
    print("removing a member takes their read token with them:")
    auth, db = scoped_store()
    auth.create_account("bob@x.dev", "hunter2hunter2")
    auth.join_org("bob@x.dev", "acme-inc")
    token = approved_token(auth, "bob@x.dev", scope="read")
    assert auth.read_viewer(token), "it reads while he is a member"
    auth.remove_user("bob@x.dev")
    assert db.tables["token_scopes"] == [], db.tables["token_scopes"]
    assert auth.read_viewer(token) is None, "a removed member reads nothing"
    print("    bob removed, his read token is dead")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed")
