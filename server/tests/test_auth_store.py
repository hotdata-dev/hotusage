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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed")
