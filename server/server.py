#!/usr/bin/env python3
"""hotusage server — central collection point + admin dashboard, hotdata-only.

Two planes, all in hotdata (no local database):

  system database (core.SYSTEM_DATABASE_ID, catalog `hotusage_system`)
      orgs / users / auth_sessions — accounts and login state. Each org row
      records the org's dedicated usage database id.

  one database per organization (catalog `hotusage`)
      sessions / requests / daily_usage — provisioned automatically when an
      org is created; collectors' rows are upserted into the reporting user's
      org database, and the dashboard reads only the viewer's org database.

Auth: dashboard login (scrypt passwords, 30-day cookie sessions); collectors
send `Authorization: Bearer $HOTUSAGE_INGEST_TOKEN` (unset = dev mode). Usage
reported by an email that is not a registered user is rejected (403) — there
is no org database to put it in.

Usage: python3 server.py [--port 8377] [--host 127.0.0.1] [--ttl 60]
                         [--system-database <dbid>]
       python3 server.py addorg <slug> [--name NAME]   # provisions the org db
       python3 server.py adduser <email> [--org SLUG]
       python3 server.py resetpw <email>
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import gzip
from concurrent.futures import ThreadPoolExecutor
import time
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import html
from urllib.parse import urlparse, parse_qs, quote

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core  # noqa: E402

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# column -> SQL type, used both to build typed parquet for loads and to keep
# upsert batches type-stable regardless of JSON inference
TABLES = {
    "sessions": {
        "key": ["user_email", "session_id"],
        "cols": [("user_email", "VARCHAR"), ("hostname", "VARCHAR"),
                 ("session_id", "VARCHAR"), ("provider", "VARCHAR"),
                 ("project", "VARCHAR"), ("cwd", "VARCHAR"), ("title", "VARCHAR"),
                 ("started_at", "TIMESTAMPTZ"), ("ended_at", "TIMESTAMPTZ"),
                 ("requests", "BIGINT"), ("models", "VARCHAR"),
                 ("input_tokens", "BIGINT"), ("output_tokens", "BIGINT"),
                 ("cache_read_tokens", "BIGINT"), ("cache_write_tokens", "BIGINT"),
                 ("cost_input", "DOUBLE"), ("cost_output", "DOUBLE"),
                 ("cost_cache_read", "DOUBLE"), ("cost_cache_write", "DOUBLE"),
                 ("cost_total", "DOUBLE"), ("peak_context_tokens", "BIGINT")],
    },
    "requests": {
        "key": ["user_email", "session_id", "seq"],
        "cols": [("user_email", "VARCHAR"), ("session_id", "VARCHAR"),
                 ("provider", "VARCHAR"), ("seq", "BIGINT"), ("ts", "TIMESTAMPTZ"),
                 ("context_tokens", "BIGINT"), ("output_tokens", "BIGINT")],
    },
    "daily_usage": {
        "key": ["user_email", "session_id", "day"],
        "cols": [("user_email", "VARCHAR"), ("session_id", "VARCHAR"),
                 ("provider", "VARCHAR"), ("day", "DATE"),
                 ("input_tokens", "BIGINT"), ("output_tokens", "BIGINT"),
                 ("cache_read_tokens", "BIGINT"), ("cache_write_tokens", "BIGINT"),
                 ("cost_input", "DOUBLE"), ("cost_output", "DOUBLE"),
                 ("cost_cache_read", "DOUBLE"), ("cost_cache_write", "DOUBLE")],
    },
    "orgs": {
        "key": ["slug"],
        "cols": [("slug", "VARCHAR"), ("name", "VARCHAR"),
                 ("database_id", "VARCHAR"), ("created_at", "TIMESTAMPTZ")],
    },
    "users": {
        "key": ["email"],
        "cols": [("email", "VARCHAR"), ("password_hash", "VARCHAR"),
                 ("org_slug", "VARCHAR"), ("created_at", "TIMESTAMPTZ")],
    },
    "auth_sessions": {
        "key": ["token"],
        "cols": [("token", "VARCHAR"), ("user_email", "VARCHAR"), ("expires_at", "DOUBLE")],
    },
    "invites": {
        "key": ["token"],
        "cols": [("token", "VARCHAR"), ("email", "VARCHAR"), ("org_slug", "VARCHAR"),
                 ("invited_by", "VARCHAR"), ("created_at", "TIMESTAMPTZ"),
                 ("expires_at", "DOUBLE")],
    },
    # Reusable team links, kept in their own table so the single-use `invites`
    # schema is untouched. `domain` (may be empty) restricts who can join;
    # `max_uses` 0 means unlimited.
    "team_invites": {
        "key": ["token"],
        "cols": [("token", "VARCHAR"), ("org_slug", "VARCHAR"), ("domain", "VARCHAR"),
                 ("invited_by", "VARCHAR"), ("created_at", "TIMESTAMPTZ"),
                 ("expires_at", "DOUBLE"), ("max_uses", "BIGINT"), ("uses", "BIGINT")],
    },
    # Which orgs a person belongs to. users.org_slug remains their ACTIVE org
    # (what ingest routes to and the dashboard shows); these rows are the set
    # they may switch among. Backfilled lazily from users.org_slug.
    "org_memberships": {
        "key": ["email", "org_slug"],
        "cols": [("email", "VARCHAR"), ("org_slug", "VARCHAR"),
                 ("created_at", "TIMESTAMPTZ")],
    },
    # Platform operators: may create and delete organizations from the UI.
    # Distinct from org_admins, which is scoped to one org -- creating an org
    # provisions a billable hotdata database, so it is not an org-level power.
    "system_admins": {
        "key": ["email"],
        "cols": [("email", "VARCHAR"), ("granted_at", "TIMESTAMPTZ")],
    },
    # Who may manage an org. Its own table rather than a column on `users`:
    # hotdata fixes a table's columns at first write, so adding one to a live
    # table is not possible. Absence of a row means "ordinary member".
    "org_admins": {
        "key": ["org_slug", "email"],
        "cols": [("org_slug", "VARCHAR"), ("email", "VARCHAR"),
                 ("granted_at", "TIMESTAMPTZ")],
    },
    # Usage stamps live apart from the credential: writing one must never be
    # able to re-create a token row that a sign-out or revoke just deleted.
    "collector_token_usage": {
        "key": ["token"],
        "cols": [("token", "VARCHAR"), ("last_used_at", "TIMESTAMPTZ")],
    },
    # Collector sign-in (device-authorization flow). A collector starts an
    # attempt, the person approves it in the browser while logged in, and the
    # collector polls until a token bound to their account is minted.
    "device_codes": {
        "key": ["device_code"],
        "cols": [("device_code", "VARCHAR"), ("user_code", "VARCHAR"),
                 ("hostname", "VARCHAR"), ("created_at", "TIMESTAMPTZ"),
                 ("expires_at", "DOUBLE"), ("approved_email", "VARCHAR"),
                 ("collector_token", "VARCHAR")],
    },
    # Per-user collector credentials: identity, not just admission. Ingest
    # authenticated with one of these reports as its owner and cannot claim
    # another colleague's address. `last_used_at` here is vestigial and always
    # NULL -- it exists only because the live table already declares it and
    # hotdata requires every column in an upsert; the real stamp lives in
    # collector_token_usage.
    "collector_tokens": {
        "key": ["token"],
        "cols": [("token", "VARCHAR"), ("user_email", "VARCHAR"),
                 ("hostname", "VARCHAR"), ("created_at", "TIMESTAMPTZ"),
                 ("last_used_at", "TIMESTAMPTZ")],
    },
}

USAGE_TABLES = ("sessions", "requests", "daily_usage")
SYSTEM_TABLES = ("orgs", "users", "auth_sessions", "invites", "team_invites",
                 "device_codes", "collector_tokens", "collector_token_usage",
                 "org_admins", "system_admins", "org_memberships")


# ---------------------------------------------------------------------------
# HotdataClient: SQL reads + key-based managed loads against one database.
# ---------------------------------------------------------------------------
class HotdataClient:
    def __init__(self, database_id):
        self.db = database_id
        self._client = None
        self.write_lock = threading.Lock()
        self._client_lock = threading.Lock()

    def client(self):
        """The one ApiClient for this database, built on first use.

        Locked, because gather() is the first thing to reach a cold client and
        it arrives on several threads at once: unlocked, each would see None,
        each would build an ApiClient with its own urllib3 pool, and all but
        the last would be dropped on the floor still holding their sockets.
        Checked twice so the warm path -- every call after the first -- stays
        a plain attribute read."""
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is None:
                import hotdata
                key = core.hotdata_api_key()
                if not key:
                    raise RuntimeError(
                        "no hotdata API key (HOTDATA_API_KEY or ~/.hotdata/hotdata.json)")
                self._client = hotdata.ApiClient(hotdata.Configuration(
                    host=os.environ.get("HOTDATA_API_HOST", "https://api.hotdata.dev"),
                    api_key=key,
                    workspace_id=os.environ.get("HOTDATA_WORKSPACE", core.WORKSPACE_ID),
                ))
        return self._client

    def sql(self, query, timeout=None):
        import hotdata
        client = self.client()
        resp = hotdata.QueryApi(client).query(
            hotdata.QueryRequest(sql=query), x_database_id=self.db,
            _request_timeout=timeout)
        cols, rows = resp.columns, list(resp.rows)
        if resp.truncated and resp.result_id:
            results = hotdata.ResultsApi(client)
            total = resp.total_row_count
            while total is None or len(rows) < total:
                r = results.get_result(resp.result_id, self.db, offset=len(rows))
                if getattr(r, "status", None) in ("pending", "processing"):
                    time.sleep(1)
                    continue
                more = r.rows or []
                if not more:
                    break
                rows.extend(more)
                total = getattr(r, "total_row_count", None) or total
        return [dict(zip(cols, r)) for r in rows]

    def rows(self, query):
        """sql() that treats declared-but-empty tables as empty results."""
        try:
            return self.sql(query)
        except Exception as e:
            msg = str(e).lower()
            if "not found" in msg or "has no data" in msg:
                return []
            raise

    def load(self, table, rows, mode):
        """Upload rows as typed parquet and apply them (upsert/delete/replace)."""
        if not rows:
            return
        import duckdb
        import hotdata
        spec = TABLES[table]
        select = ", ".join(f"{c}::{t} AS {c}" for c, t in spec["cols"]
                           if mode != "delete" or c in spec["key"])
        with self.write_lock, tempfile.TemporaryDirectory(prefix="hotusage-") as tmp:
            jl = os.path.join(tmp, "rows.jsonl")
            pq = os.path.join(tmp, "rows.parquet")
            with open(jl, "w") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")
            con = duckdb.connect()
            # naive->TIMESTAMPTZ casts must read as UTC or timestamps shift
            con.execute("SET timezone='UTC'")
            con.execute(f"COPY (SELECT {select} FROM read_json_auto('{jl}', sample_size=-1)) "
                        f"TO '{pq}' (FORMAT PARQUET)")
            fin = hotdata.UploadsApi(self.client()).upload_file(
                pq, content_type="application/parquet")
            hotdata.DatabasesApi(self.client()).load_database_table(
                self.db, "public", table,
                hotdata.LoadManagedTableRequest(mode=mode, upload_id=fin.upload_id,
                                                key=spec["key"]))

    def ensure_schema_and_tables(self, tables):
        import hotdata
        dbs = hotdata.DatabasesApi(self.client())
        try:
            dbs.add_database_schema(self.db, hotdata.AddManagedSchemaRequest(name="public"))
        except hotdata.exceptions.ApiException as e:
            if e.status not in (400, 409):
                raise
        for t in tables:
            try:
                dbs.add_database_table(
                    self.db, "public",
                    hotdata.AddManagedTableRequest(name=t, key=TABLES[t]["key"]))
            except hotdata.exceptions.ApiException as e:
                if e.status not in (400, 409):
                    raise

    def create_org_database(self, slug):
        import hotdata
        resp = hotdata.DatabasesApi(self.client()).create_database(
            hotdata.CreateDatabaseRequest(name=f"hotusage-{slug}",
                                          default_catalog=core.CATALOG))
        return resp.id


class ClientPool:
    def __init__(self):
        self.clients = {}
        self.lock = threading.Lock()

    def get(self, database_id):
        with self.lock:
            if database_id not in self.clients:
                self.clients[database_id] = HotdataClient(database_id)
            return self.clients[database_id]


def gather(tasks=None, /, **thunks):
    """Run independent zero-arg lookups at once and return {name: result}.

    Takes a dict, keyword thunks, or both -- a dict keyed by org slug cannot
    be splatted as keywords, since a slug may contain a hyphen.

    Every system-table read is a separate round trip to the hotdata API
    (~120 ms each), so a handler that needs a dozen of them spends seconds
    waiting in series. They do not depend on each other, so overlap them.
    An exception in any thunk propagates, as it would have in series.
    A thunk may gather() in turn, so the queries in flight can reach the
    product of the two fan-outs -- fine against an API that does not
    throttle concurrent queries, worth revisiting if that changes. The
    generated client's connection_pool_maxsize was 50 when measured against
    the hotdata python package 0.10.0 (2026-09-13; the CLI is versioned
    separately and is not this), so a fan-out of this shape reuses
    connections rather than reopening them; a bound here would only be
    needed if a single request's fan-out approached that. The Dockerfile
    pins no version, so re-measure before relying on the number."""
    work = {**(tasks or {}), **thunks}
    if not work:
        return {}
    with ThreadPoolExecutor(max_workers=min(len(work), 8)) as pool:
        futures = {name: pool.submit(fn) for name, fn in work.items()}
        return {name: f.result() for name, f in futures.items()}


# ---------------------------------------------------------------------------
# AuthStore: orgs, users, cookie sessions in the system database. Creating an
# org provisions its dedicated usage database.
# ---------------------------------------------------------------------------
SESSION_TTL = 30 * 24 * 3600
SYS = core.SYSTEM_CATALOG

def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return base64.b64encode(salt).decode() + "$" + base64.b64encode(h).decode()

def verify_password(password, stored):
    try:
        salt_b64, _ = stored.split("$", 1)
        return hmac.compare_digest(hash_password(password, base64.b64decode(salt_b64)), stored)
    except (ValueError, TypeError):
        return False

def sql_str(s):
    return "'" + str(s).replace("'", "''") + "'"


class AuthStore:
    def __init__(self, system, pool):
        self.sysdb = system          # HotdataClient for the system database
        self.pool = pool             # ClientPool for org databases
        self.token_cache = {}        # token -> (viewer dict, expires_at)
        self.route_cache = {}        # email -> ((org_slug, db_id), fetched_at)
        self.org_cache = {}          # org_slug -> (org dict, fetched_at)
        self.lock = threading.Lock()

    def get_org(self, slug, ttl=60):
        with self.lock:
            hit = self.org_cache.get(slug)
            if hit and time.time() - hit[1] < ttl:
                return hit[0]
        rows = self.sysdb.rows(
            f"SELECT slug, name, database_id FROM {SYS}.public.orgs WHERE slug = {sql_str(slug)}")
        org = rows[0] if rows else None
        with self.lock:
            self.org_cache[slug] = (org, time.time())
        return org

    def ensure_org(self, slug, name=None):
        """Returns the org's database id, provisioning a dedicated usage
        database for a new org. An org that already exists is returned as is --
        callers that must have created it want provision_org."""
        org = self.get_org(slug, ttl=0)
        if org:
            return org["database_id"]
        return self.provision_org(slug, name)

    def provision_org(self, slug, name=None):
        """Always provisions a fresh database and claims the slug for it.

        Unconditional by design: it is what lets a caller tell whether it won
        the race for a slug, by checking afterwards whose database the orgs row
        ended up pointing at."""
        if not re.match(r"^[a-z0-9][a-z0-9-]{0,60}$", slug):
            raise ValueError("org slug must be lowercase alphanumeric/hyphens")
        db_id = self.sysdb.create_org_database(slug)
        self.pool.get(db_id).ensure_schema_and_tables(USAGE_TABLES)
        self.sysdb.load("orgs", [{"slug": slug, "name": name or slug, "database_id": db_id,
                                  "created_at": datetime.now(timezone.utc).isoformat()}],
                        "upsert")
        with self.lock:
            self.org_cache.pop(slug, None)
        print(f"provisioned database {db_id} for org '{slug}'")
        return db_id

    def get_user(self, email):
        rows = self.sysdb.rows(
            "SELECT u.email, u.password_hash, u.org_slug, o.name AS org_name, "
            f"o.database_id FROM {SYS}.public.users u "
            f"LEFT JOIN {SYS}.public.orgs o ON o.slug = u.org_slug "
            f"WHERE u.email = {sql_str(email.strip().lower())}")
        return rows[0] if rows else None

    def create_user(self, email, password, org_slug):
        email = email.strip().lower()
        if self.get_user(email):
            raise ValueError(f"user {email} already exists")
        self.ensure_org(org_slug)
        # only the FIRST member: an org that has members but lost its admin
        # must not hand admin (and everyone's data) to whoever joins next --
        # that recovery is an explicit makeadmin, not an accident
        first_member = not self._member_rows(org_slug)
        self.sysdb.load("users", [{"email": email, "password_hash": hash_password(password),
                                   "org_slug": org_slug,
                                   "created_at": datetime.now(timezone.utc).isoformat()}],
                        "upsert")
        # an org someone can join but nobody can manage is a dead end, so the
        # first member of an empty org (UI-created, or CLI addorg) becomes
        # its admin
        self.add_membership(email, org_slug)
        if first_member:
            self.set_admin(email, org_slug)
        with self.lock:
            self.route_cache.pop(email, None)

    def set_password(self, email, password):
        # read every column, as switch_org does: the upsert must carry the whole
        # row, and stamping created_at with "now" would rewrite the join date
        rows = self.sysdb.rows(f"SELECT email, org_slug, created_at "
                               f"FROM {SYS}.public.users "
                               f"WHERE email = {sql_str(email.strip().lower())}")
        if not rows:
            raise ValueError(f"no such user: {email}")
        row = rows[0]
        self.sysdb.load("users", [{"email": row["email"],
                                   "password_hash": hash_password(password),
                                   "org_slug": row["org_slug"],
                                   "created_at": str(row["created_at"])}],
                        "upsert")

    def login(self, email, password):
        u = self.get_user(email)
        if not u or not verify_password(password, u["password_hash"]):
            return None
        token = secrets.token_urlsafe(32)
        expires = time.time() + SESSION_TTL
        self.sysdb.load("auth_sessions", [{"token": token, "user_email": u["email"],
                                           "expires_at": expires}], "upsert")
        viewer = {"email": u["email"], "org_slug": u["org_slug"],
                  "org_name": u["org_name"] or u["org_slug"],
                  "database_id": u["database_id"]}
        with self.lock:
            self.token_cache[token] = (viewer, expires)
        self._purge_expired()
        return token

    def logout(self, token):
        with self.lock:
            self.token_cache.pop(token, None)
        try:
            self.sysdb.load("auth_sessions", [{"token": token}], "delete")
        except Exception as e:
            print(f"warn: logout delete: {e}", file=sys.stderr)

    def _purge_expired(self):
        try:
            rows = self.sysdb.rows(f"SELECT token FROM {SYS}.public.auth_sessions "
                                   f"WHERE expires_at < {time.time()}")
            if rows:
                self.sysdb.load("auth_sessions", rows, "delete")
            inv = self.sysdb.rows(f"SELECT token FROM {SYS}.public.invites "
                                  f"WHERE expires_at < {time.time()}")
            if inv:
                self.sysdb.load("invites", inv, "delete")
            team = self.sysdb.rows(f"SELECT token FROM {SYS}.public.team_invites "
                                   f"WHERE expires_at < {time.time()}")
            if team:
                self.sysdb.load("team_invites", team, "delete")
            dev = self.sysdb.rows(f"SELECT device_code FROM {SYS}.public.device_codes "
                                  f"WHERE expires_at < {time.time()}")
            if dev:
                self.sysdb.load("device_codes", dev, "delete")
        except Exception as e:
            print(f"warn: session purge: {e}", file=sys.stderr)

    def user_for_token(self, token):
        if not token or not re.match(r"^[A-Za-z0-9_-]{20,64}$", token):
            return None
        with self.lock:
            hit = self.token_cache.get(token)
            if hit and hit[1] > time.time():
                return hit[0]
        rows = self.sysdb.rows(
            "SELECT s.expires_at, u.email, u.org_slug, o.name AS org_name, o.database_id "
            f"FROM {SYS}.public.auth_sessions s "
            f"JOIN {SYS}.public.users u ON u.email = s.user_email "
            f"LEFT JOIN {SYS}.public.orgs o ON o.slug = u.org_slug "
            f"WHERE s.token = {sql_str(token)}")
        if not rows or float(rows[0]["expires_at"]) < time.time():
            return None
        r = rows[0]
        viewer = {"email": r["email"], "org_slug": r["org_slug"],
                  "org_name": r["org_name"] or r["org_slug"],
                  "database_id": r["database_id"]}
        with self.lock:
            self.token_cache[token] = (viewer, float(r["expires_at"]))
        return viewer

    def route_for_email(self, email, ttl=60):
        """email -> (org_slug, database_id) or None if not a registered user."""
        email = email.strip().lower()
        with self.lock:
            hit = self.route_cache.get(email)
            if hit and time.time() - hit[1] < ttl:
                return hit[0]
        u = self.get_user(email)
        route = (u["org_slug"], u["database_id"]) if u and u["database_id"] else None
        with self.lock:
            self.route_cache[email] = (route, time.time())
        return route

    INVITE_TTL = 7 * 24 * 3600

    def create_account(self, email, password):
        """Self-serve signup: an account and nothing else.

        The account deliberately belongs to no org yet -- registration used to
        provision a database in the same request, which meant a failure there
        cost the person their signup. An org is a second step (or an invite),
        so the account exists first and outlives either."""
        email = email.strip().lower()
        if self.get_user(email):
            raise ValueError("that email is already registered")
        self.sysdb.load("users", [{"email": email,
                                   "password_hash": hash_password(password),
                                   "org_slug": "",
                                   "created_at": datetime.now(timezone.utc).isoformat()}],
                        "upsert")
        with self.lock:
            self.route_cache.pop(email, None)

    def create_org_for(self, email, org_name):
        """Create an org around an account that already exists, making it the
        first member, its admin, and its active org. Returns the slug.

        Refuses an existing slug -- joining an org goes through invites, never
        through guessing its slug."""
        email = email.strip().lower()
        name = (org_name or "").strip()[:60]
        slug = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")[:40]
        if len(name) < 2 or len(slug) < 2:
            raise ValueError("organization name must be at least 2 characters")
        if not self.get_user(email):
            raise ValueError("no such user")
        if self.get_org(slug, ttl=0):
            raise ValueError(f"an organization with the slug '{slug}' already exists")
        # provision_org, not ensure_org: ensure_org hands back an EXISTING
        # org's database, so a creation that lost the race would be given the
        # winner's id and sail through the check below.
        db_id = self.provision_org(slug, name)
        # two concurrent creations can both pass the get_org check and both
        # provision a database; the orgs upsert on slug picks one winner. Only
        # the one whose database actually landed may claim it -- the loser must
        # not add a stranger to the winner's org. The loser's database is left
        # behind, unreferenced: cheaper than a stranger inside someone's org.
        org = self.get_org(slug, ttl=0)
        if not org or org["database_id"] != db_id:
            raise ValueError(f"an organization with the slug '{slug}' already exists")
        first_member = not self._member_rows(slug)
        self.add_membership(email, slug)
        if first_member:
            self.set_admin(email, slug)  # whoever creates the org administers it
        self.switch_org(email, slug)
        return slug

    def create_invite(self, email, org_slug, invited_by):
        """Invite `email` into `org_slug`; returns the single-use token. A
        registered address is fine now -- accepting adds a membership."""
        email = email.strip().lower()
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            raise ValueError("that does not look like an email address")
        user = self.get_user(email)
        if user and org_slug in self.memberships(email):
            raise ValueError("they are already a member of this organization")
        token = secrets.token_urlsafe(32)
        self.sysdb.load("invites", [{
            "token": token, "email": email, "org_slug": org_slug,
            "invited_by": invited_by,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": time.time() + self.INVITE_TTL,
        }], "upsert")
        return token

    def get_invite(self, token):
        if not token or not re.match(r"^[A-Za-z0-9_-]{20,64}$", token):
            return None
        rows = self.sysdb.rows(
            f"SELECT i.token, i.email, i.org_slug, i.invited_by, i.expires_at, "
            f"o.name AS org_name FROM {SYS}.public.invites i "
            f"LEFT JOIN {SYS}.public.orgs o ON o.slug = i.org_slug "
            f"WHERE i.token = {sql_str(token)}")
        if not rows or float(rows[0]["expires_at"]) < time.time():
            return None
        if rows[0]["org_name"] is None:  # org deleted since the invite was minted
            return None
        return rows[0]

    def accept_invite(self, token, password=None, as_user=None):
        """Consume the invite. A new address needs a password and gets an
        account; an existing account must be signed in as the invited address
        (`as_user`) and gains a membership plus the switch. Returns the email."""
        inv = self.get_invite(token)
        if not inv:
            raise ValueError("this invite is invalid or has expired")
        email = inv["email"]
        if self.get_user(email):
            # possession of the link is not proof of the mailbox; the session is
            if (as_user or "").strip().lower() != email:
                raise ValueError("this invite belongs to an existing account - "
                                 "sign in as " + email + " first")
            self.join_org(email, inv["org_slug"])
        else:
            self.create_user(email, password, inv["org_slug"])
        self.sysdb.load("invites", [{"token": token}], "delete")
        return email

    def join_org(self, email, org_slug):
        """Add an existing account to another org and make it active. The
        first member of an empty org still becomes its admin. Emptiness is
        judged across BOTH tables: org_memberships is empty for orgs that
        predate it, and joining an established org must never grant admin."""
        # backfill FIRST: for an account predating org_memberships this read
        # writes their active org's row; adding the new membership before it
        # would make the set non-empty and silently drop their original org
        self.memberships(email)
        first_member = not self._member_rows(org_slug)
        self.ensure_org(org_slug)
        self.add_membership(email, org_slug)
        if first_member:
            self.set_admin(email, org_slug)
        self.switch_org(email, org_slug)

    # --- multi-org membership -------------------------------------------------
    def add_membership(self, email, org_slug):
        self.sysdb.load("org_memberships", [{
            "email": email.strip().lower(), "org_slug": org_slug,
            "created_at": datetime.now(timezone.utc).isoformat()}], "upsert")

    def memberships(self, email):
        """Org slugs this person belongs to. Accounts predating this table
        get their active org backfilled on first read."""
        email = email.strip().lower()
        rows = self.sysdb.rows(f"SELECT org_slug FROM {SYS}.public.org_memberships "
                               f"WHERE email = {sql_str(email)}")
        slugs = {r["org_slug"] for r in rows}
        if not slugs:
            user = self.get_user(email)
            # org_slug is empty for an account that has not made or joined an
            # org yet; there is nothing to backfill and "" is not a membership
            if user and user["org_slug"]:
                self.add_membership(email, user["org_slug"])
                slugs = {user["org_slug"]}
        return slugs

    def switch_org(self, email, org_slug):
        """Point the account's ACTIVE org (ingest routing, dashboard) at one
        of its memberships."""
        email = email.strip().lower()
        if org_slug not in self.memberships(email):
            raise ValueError("you are not a member of that organization")
        user = self.get_user(email)
        if not user:
            raise ValueError("no such user")
        if user["org_slug"] == org_slug:
            return
        rows = self.sysdb.rows(f"SELECT email, password_hash, org_slug, created_at "
                               f"FROM {SYS}.public.users WHERE email = {sql_str(email)}")
        row = rows[0]
        self.sysdb.load("users", [{"email": email,
                                   "password_hash": row["password_hash"],
                                   "org_slug": org_slug,
                                   "created_at": str(row["created_at"])}], "upsert")
        with self.lock:
            self.route_cache.pop(email, None)
            self.token_cache.clear()  # sessions carry the org they were minted in

    # --- platform operators --------------------------------------------------
    def is_system_admin(self, email):
        rows = self.sysdb.rows(f"SELECT email FROM {SYS}.public.system_admins "
                               f"WHERE email = {sql_str(email.strip().lower())}")
        return bool(rows)

    def set_system_admin(self, email, on=True):
        email = email.strip().lower()
        if on:
            self.sysdb.load("system_admins", [{
                "email": email,
                "granted_at": datetime.now(timezone.utc).isoformat()}], "upsert")
        else:
            self.sysdb.load("system_admins", [{"email": email}], "delete")

    def all_orgs(self):
        """Two reads, whatever the platform holds. A member count is the size
        of the union of users and org_memberships for that slug, which the
        engine can group into one row per org -- the per-org fan-out this
        replaced cost 2N+1 round trips, and pulling the two tables back whole
        would have cost a round trip per result page of *users*, since sql()
        pages through truncated results."""
        counted = (f"SELECT org_slug, COUNT(DISTINCT email) AS members FROM ("
                   f"SELECT org_slug, email FROM {SYS}.public.users "
                   f"UNION SELECT org_slug, email FROM {SYS}.public.org_memberships"
                   f") AS u GROUP BY org_slug")
        got = gather(
            orgs=lambda: self.sysdb.rows(f"SELECT slug, name, database_id, created_at "
                                         f"FROM {SYS}.public.orgs ORDER BY created_at"),
            counts=lambda: self.sysdb.rows(counted))
        counts = {r["org_slug"]: int(r["members"]) for r in got["counts"]}
        if not counts and got["orgs"]:
            # rows() turns a declared-but-empty table into an empty result, and
            # one query spanning both tables cannot degrade per table -- so an
            # empty org_memberships would zero every count. Re-read separately.
            counts = self._counts_from_full_reads()
        return [{**o, "members": counts.get(o["slug"], 0)} for o in got["orgs"]]

    def _counts_from_full_reads(self):
        both = gather(
            active=lambda: self.sysdb.rows(f"SELECT org_slug, email FROM {SYS}.public.users"),
            joined=lambda: self.sysdb.rows(f"SELECT org_slug, email "
                                           f"FROM {SYS}.public.org_memberships"))
        by_slug = {}
        for r in both["active"] + both["joined"]:
            by_slug.setdefault(r["org_slug"], set()).add(r["email"])
        return {slug: len(emails) for slug, emails in by_slug.items()}

    def create_org(self, name):
        """Platform-side org creation: provision the database, no first user.
        Returns the slug. The first person to join becomes its admin."""
        name = (name or "").strip()[:60]
        slug = re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-")[:40]
        if len(name) < 2 or len(slug) < 2:
            raise ValueError("organization name must be at least 2 characters")
        if self.get_org(slug, ttl=0):
            raise ValueError(f"an organization with the slug '{slug}' already exists")
        self.ensure_org(slug, name)
        return slug

    def delete_empty_org(self, slug):
        """Remove an org with no members. Its database is kept (delorg
        --delete-database is the CLI-only destructive path)."""
        org = self.get_org(slug, ttl=0)
        if not org:
            raise ValueError("no such organization")
        if self._member_rows(slug):
            raise ValueError("that organization still has members")
        for table, col in (("org_admins", "org_slug"), ("invites", "org_slug"),
                           ("team_invites", "org_slug"), ("org_memberships", "org_slug")):
            rows = self.sysdb.rows(f"SELECT * FROM {SYS}.public.{table} "
                                   f"WHERE {col} = {sql_str(slug)}")
            if rows:
                key = TABLES[table]["key"]
                self.sysdb.load(table, [{k: r[k] for k in key} for r in rows], "delete")
        self.sysdb.load("orgs", [{"slug": slug}], "delete")
        with self.lock:
            self.org_cache.pop(slug, None)
        return org.get("database_id")

    # --- who may manage the org ---------------------------------------------
    def is_admin(self, email, org_slug):
        rows = self.sysdb.rows(
            f"SELECT email FROM {SYS}.public.org_admins "
            f"WHERE org_slug = {sql_str(org_slug)} AND email = {sql_str(email)}")
        return bool(rows)

    def admins(self, org_slug):
        return {r["email"] for r in self.sysdb.rows(
            f"SELECT email FROM {SYS}.public.org_admins "
            f"WHERE org_slug = {sql_str(org_slug)}")}

    def set_admin(self, email, org_slug, on=True):
        email = email.strip().lower()
        if on:
            self.sysdb.load("org_admins", [{
                "org_slug": org_slug, "email": email,
                "granted_at": datetime.now(timezone.utc).isoformat()}], "upsert")
        else:
            self.sysdb.load("org_admins",
                            [{"org_slug": org_slug, "email": email}], "delete")

    def _member_rows(self, org_slug):
        return self._roster(org_slug)[0]

    def _roster(self, org_slug):
        """(member rows, emails ACTIVE in the org) from one pair of reads.

        Union of membership rows and active-org rows. Accounts predating
        org_memberships exist only in users, and the lazy backfill writes one
        row per user on their own read -- so neither table alone is the org's
        roster until every member has loaded a page. The active set is handed
        back because collector listing needs exactly that distinction, which
        the merge below throws away."""
        both = gather(
            active=lambda: self.sysdb.rows(
                f"SELECT email, created_at FROM {SYS}.public.users "
                f"WHERE org_slug = {sql_str(org_slug)}"),
            joined=lambda: self.sysdb.rows(
                f"SELECT email, created_at FROM {SYS}.public.org_memberships "
                f"WHERE org_slug = {sql_str(org_slug)}"))
        merged = {r["email"]: r for r in both["active"]}
        for r in both["joined"]:
            merged.setdefault(r["email"], r)
        rows = sorted(merged.values(), key=lambda r: (str(r["created_at"]), r["email"]))
        return rows, {r["email"] for r in both["active"]}

    def roster(self, org_slug):
        """Everything the admin page needs about who belongs to an org, from
        one wave of reads: the members, the org's admin set (so the viewer's
        own is_admin needs no second org_admins lookup) and the active emails
        (so org_collectors needs no second users lookup)."""
        got = gather(admins=lambda: self.admins(org_slug),
                     roster=lambda: self._roster(org_slug))
        rows, active = got["roster"]
        return {"members": [{**r, "is_admin": r["email"] in got["admins"]} for r in rows],
                "admins": got["admins"], "active": active}

    def members(self, org_slug):
        """Everyone who belongs to the org (active there or not)."""
        return self.roster(org_slug)["members"]

    def remove_from_org(self, email, org_slug):
        """Remove one membership. The account (logins, collectors) survives
        while other memberships remain -- an admin of one org has no authority
        over the target's access to another."""
        email = email.strip().lower()
        remaining = self.memberships(email) - {org_slug}
        if not remaining:
            self.remove_user(email)
            return
        self.set_admin(email, org_slug, False)
        self.sysdb.load("org_memberships",
                        [{"email": email, "org_slug": org_slug}], "delete")
        user = self.get_user(email)
        if user and user["org_slug"] == org_slug:
            # their active org is gone from under them; land on another
            self.switch_org(email, sorted(remaining)[0])

    def remove_user(self, email):
        """Delete a member: their logins, collector tokens and account. Their
        already-ingested usage stays in the org database."""
        email = email.strip().lower()
        toks = self.sysdb.rows(f"SELECT token FROM {SYS}.public.auth_sessions "
                               f"WHERE user_email = {sql_str(email)}")
        if toks:
            self.sysdb.load("auth_sessions", toks, "delete")
            with self.lock:
                for t in toks:
                    self.token_cache.pop(t["token"], None)
        ctoks = self.sysdb.rows(f"SELECT token FROM {SYS}.public.collector_tokens "
                                f"WHERE user_email = {sql_str(email)}")
        if ctoks:
            self.sysdb.load("collector_tokens", ctoks, "delete")
            try:
                self.sysdb.load("collector_token_usage", ctoks, "delete")
            except Exception as e:
                print(f"warn: usage row delete: {e}", file=sys.stderr)
        for slug in self.memberships(email):
            self.set_admin(email, slug, False)
            self.sysdb.load("org_memberships",
                            [{"email": email, "org_slug": slug}], "delete")
        # and the platform grant: otherwise whoever re-registers this address
        # at the public /register endpoint inherits system admin
        self.set_system_admin(email, False)
        self.sysdb.load("users", [{"email": email}], "delete")
        with self.lock:
            self.route_cache.pop(email, None)

    def rename_org(self, slug, name):
        # read every column: an upsert must carry the whole row, and get_org
        # deliberately selects only what routing needs
        rows = self.sysdb.rows(f"SELECT slug, name, database_id, created_at "
                               f"FROM {SYS}.public.orgs WHERE slug = {sql_str(slug)}")
        if not rows:
            raise ValueError("no such organization")
        row = rows[0]
        self.sysdb.load("orgs", [{"slug": row["slug"], "name": name,
                                  "database_id": row["database_id"],
                                  "created_at": str(row["created_at"])}], "upsert")
        with self.lock:
            self.org_cache.pop(slug, None)
            # sessions carry the org name they were minted with, so drop them
            # from the cache or the old name lingers until they expire
            self.token_cache.clear()

    # hotdata ids are opaque but well-shaped; refusing anything else keeps a
    # typo from repointing an org at nothing
    DB_ID_RE = re.compile(r"^dbid[a-z0-9]{12,60}$")

    def set_org_database(self, slug, database_id):
        """Point an org at a different usage database, overriding the one
        provisioned for it. Returns the id it replaced.

        Isolation between organizations IS this column -- the dashboard reads
        one database, not a filtered view of many -- so an id already claimed
        by another org is refused rather than quietly shared. The target is
        reached before the switch is recorded, so an unreachable id fails
        without leaving the org pointing at nothing."""
        database_id = (database_id or "").strip()
        if not self.DB_ID_RE.match(database_id):
            raise ValueError("that does not look like a hotdata database id")
        # the accounts database is well-formed and claimed by no org, so the
        # clash check below would wave it through -- and usage tables would be
        # created alongside users, orgs and collector_tokens
        if database_id == self.sysdb.db:
            raise ValueError("that is the system database, not a usage database")
        rows = self.sysdb.rows(f"SELECT slug, name, database_id, created_at "
                               f"FROM {SYS}.public.orgs")
        row = next((r for r in rows if r["slug"] == slug), None)
        if not row:
            raise ValueError("no such organization")
        if row["database_id"] == database_id:
            return database_id
        clash = next((r for r in rows
                      if r["database_id"] == database_id and r["slug"] != slug), None)
        if clash:
            raise ValueError(f"'{clash['slug']}' already reports into that database; "
                             f"two organizations sharing one is what org isolation "
                             f"prevents")
        target = self.pool.get(database_id)
        try:
            # probe first: ensure_schema_and_tables swallows 400 and 409 so that
            # "already exists" is not an error, which means it would also
            # swallow "no such database" and vouch for an id that does not
            # exist. sql(), not rows() -- rows() turns "not found" into [].
            target.sql("SELECT 1")
            target.ensure_schema_and_tables(USAGE_TABLES)
        except Exception as e:
            raise ValueError(f"could not reach that database: {e}")
        self.sysdb.load("orgs", [{"slug": row["slug"], "name": row["name"],
                                  "database_id": database_id,
                                  "created_at": str(row["created_at"])}], "upsert")
        with self.lock:
            self.org_cache.pop(slug, None)
            # both caches carry a database id for a signed-in viewer or a
            # reporting collector, so a stale entry would keep writing to the
            # database this call just replaced
            self.route_cache.clear()
            self.token_cache.clear()
        return row["database_id"]

    def revoke_any_invite(self, token, org_slug):
        """Revoke an invite of either kind, but only one belonging to `org_slug`."""
        for table in ("invites", "team_invites"):
            rows = self.sysdb.rows(f"SELECT token, org_slug FROM {SYS}.public.{table} "
                                   f"WHERE token = {sql_str(token)}")
            if rows and rows[0]["org_slug"] == org_slug:
                self.sysdb.load(table, [{"token": token}], "delete")
                return
        raise ValueError("no such invite in this organization")

    def revoke_collector_token_for_org(self, token, org_slug):
        email = self.collector_token_user(token)
        user = self.get_user(email) if email else None
        if not user or user["org_slug"] != org_slug:
            raise ValueError("no such collector in this organization")
        self.revoke_collector_token(token)

    def org_invites(self, org_slug):
        both = gather(
            single=lambda: self.sysdb.rows(
                f"SELECT token, email, expires_at FROM {SYS}.public.invites "
                f"WHERE org_slug = {sql_str(org_slug)}"),
            team=lambda: self.sysdb.rows(
                f"SELECT token, domain, max_uses, uses, expires_at "
                f"FROM {SYS}.public.team_invites WHERE org_slug = {sql_str(org_slug)}"))
        invites = [{**r, "kind": "single"} for r in both["single"]] + \
                  [{**r, "kind": "team"} for r in both["team"]]
        now = time.time()
        return [i for i in invites if float(i["expires_at"]) > now]

    def org_collectors(self, org_slug, active=None):
        # ACTIVE members only: a collector routes ingest to its owner's active
        # org, so a member active elsewhere reports elsewhere -- listing their
        # token here would expose a credential this org cannot even revoke.
        # `active` is that same set, when the caller has already read it.
        emails = sorted(active) if active is not None else [
            r["email"] for r in self.sysdb.rows(
                f"SELECT email FROM {SYS}.public.users "
                f"WHERE org_slug = {sql_str(org_slug)}")]
        if not emails:
            return []
        wanted = ", ".join(sql_str(e) for e in emails)
        toks = self.sysdb.rows(
            f"SELECT token, user_email, hostname, created_at "
            f"FROM {SYS}.public.collector_tokens WHERE user_email IN ({wanted})")
        tokens = ", ".join(sql_str(t["token"]) for t in toks) or "''"
        used = {u["token"]: u["last_used_at"] for u in self.sysdb.rows(
            f"SELECT token, last_used_at FROM {SYS}.public.collector_token_usage "
            f"WHERE token IN ({tokens})")}
        return [{**t, "last_used_at": used.get(t["token"])} for t in toks]

    # --- reusable team links -------------------------------------------------
    TEAM_INVITE_TTL = 30 * 24 * 3600
    MAX_TEAM_INVITE_USES = 1000
    MAX_TEAM_INVITE_TTL = 90 * 24 * 3600

    def create_team_invite(self, org_slug, invited_by, domain="",
                           max_uses=0, expires_days=None):
        """A link several people can use. `domain` (optional) restricts joiners
        to that email domain; `max_uses` 0 means unlimited. Returns the token."""
        domain = (domain or "").strip().lower().lstrip("@")
        if domain and not re.match(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$", domain):
            raise ValueError("that does not look like an email domain")
        try:
            max_uses = min(max(0, int(max_uses or 0)), self.MAX_TEAM_INVITE_USES)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("max uses must be a whole number")
        ttl = self.TEAM_INVITE_TTL
        if expires_days:
            try:
                ttl = int(expires_days) * 86400
            except (TypeError, ValueError):
                raise ValueError("expiry must be a whole number of days")
            ttl = max(86400, min(ttl, self.MAX_TEAM_INVITE_TTL))
        token = secrets.token_urlsafe(32)
        self.sysdb.load("team_invites", [{
            "token": token, "org_slug": org_slug, "domain": domain,
            "invited_by": invited_by,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": time.time() + ttl,
            "max_uses": max_uses, "uses": 0,
        }], "upsert")
        return token

    def get_team_invite(self, token):
        if not token or not re.match(r"^[A-Za-z0-9_-]{20,64}$", token):
            return None
        rows = self.sysdb.rows(
            f"SELECT t.token, t.org_slug, t.domain, t.invited_by, t.created_at, "
            f"t.expires_at, t.max_uses, t.uses, o.name AS org_name "
            f"FROM {SYS}.public.team_invites t "
            f"LEFT JOIN {SYS}.public.orgs o ON o.slug = t.org_slug "
            f"WHERE t.token = {sql_str(token)}")
        if not rows or float(rows[0]["expires_at"]) < time.time():
            return None
        inv = rows[0]
        if inv["org_name"] is None:  # org deleted since the link was minted
            return None
        if inv["max_uses"] and int(inv["uses"]) >= int(inv["max_uses"]):
            return None
        return inv

    def accept_team_invite(self, token, email, password, as_user=None):
        """Join `token`'s org as `email`. The link stays usable until it expires
        or hits max_uses."""
        inv = self.get_team_invite(token)
        if not inv:
            raise ValueError("this invite link is invalid, expired, or fully used")
        email = (email or "").strip().lower()
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            raise ValueError("that does not look like an email address")
        if inv["domain"] and email.rsplit("@", 1)[1] != inv["domain"]:
            raise ValueError(f"this link only accepts @{inv['domain']} addresses")
        if self.get_user(email):
            if (as_user or "").strip().lower() != email:
                raise ValueError("that email already has an account - sign in first")
            if inv["org_slug"] in self.memberships(email):
                raise ValueError("you are already a member of this organization")
            self.join_org(email, inv["org_slug"])
        else:
            self.create_user(email, password, inv["org_slug"])
        # Best-effort use counter: concurrent joins can read the same value and
        # let a link go a use or two past max_uses. The domain restriction is
        # the real control; tighten this if the cap ever needs to be exact.
        self.sysdb.load("team_invites", [{**{k: inv[k] for k in
                                             ("token", "org_slug", "domain", "invited_by",
                                              "expires_at", "max_uses")},
                                          "created_at": str(inv["created_at"]),
                                          "uses": int(inv["uses"]) + 1}], "upsert")
        return email

    def revoke_team_invite(self, token):
        self.sysdb.load("team_invites", [{"token": token}], "delete")

    # --- collector sign-in (device authorization) ---------------------------
    DEVICE_CODE_TTL = 10 * 60
    DEVICE_POLL_INTERVAL = 3
    # no vowels and no look-alikes (0/O, 1/I/L), so a code read off one screen
    # and compared on another cannot be mistyped into a different valid code
    USER_CODE_ALPHABET = "ACDEFGHJKMNPQRTUVWXY3479"

    def start_device_auth(self, hostname):
        """Begin a sign-in attempt. Returns (device_code, user_code)."""
        device_code = secrets.token_urlsafe(32)
        user_code = "-".join(
            "".join(secrets.choice(self.USER_CODE_ALPHABET) for _ in range(4))
            for _ in range(2))
        self.sysdb.load("device_codes", [{
            "device_code": device_code, "user_code": user_code,
            "hostname": (hostname or "")[:80],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "expires_at": time.time() + self.DEVICE_CODE_TTL,
            "approved_email": "", "collector_token": "",
        }], "upsert")
        return device_code, user_code

    def _device_row(self, column, value):
        if not value or not re.match(r"^[A-Za-z0-9_-]{4,64}$", value):
            return None
        rows = self.sysdb.rows(f"SELECT device_code, user_code, hostname, expires_at, "
                               f"approved_email, collector_token "
                               f"FROM {SYS}.public.device_codes "
                               f"WHERE {column} = {sql_str(value)}")
        if not rows or float(rows[0]["expires_at"]) < time.time():
            return None
        return rows[0]

    def get_device_by_user_code(self, user_code):
        return self._device_row("user_code", (user_code or "").strip().upper())

    def approve_device(self, user_code, email, hostname_confirm=None):
        """The signed-in person approves an attempt: mint a collector token
        bound to their account and hand it to the waiting collector."""
        row = self.get_device_by_user_code(user_code)
        if not row:
            raise ValueError("this sign-in request is invalid or has expired")
        if row["approved_email"]:
            raise ValueError("this sign-in request was already approved")
        token = secrets.token_urlsafe(32)
        self.sysdb.load("collector_tokens", [{
            "token": token, "user_email": email, "hostname": row["hostname"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "last_used_at": None,
        }], "upsert")
        self.sysdb.load("device_codes", [{**row, "approved_email": email,
                                          "collector_token": token,
                                          "created_at": datetime.now(timezone.utc).isoformat()}],
                        "upsert")
        return token

    def poll_device(self, device_code):
        """Collector side: None while pending, (email, token) once approved.
        The row is consumed on success so the token is handed out only once."""
        row = self._device_row("device_code", device_code)
        if not row:
            raise ValueError("this sign-in request is invalid or has expired")
        if not row["approved_email"]:
            return None
        self.sysdb.load("device_codes", [{"device_code": row["device_code"]}], "delete")
        return row["approved_email"], row["collector_token"]

    def revoke_collector_token(self, token):
        """Sign out one collector. Returns the address it belonged to."""
        email = self.collector_token_user(token)
        if not email:
            return None
        self.sysdb.load("collector_tokens", [{"token": token}], "delete")
        try:
            self.sysdb.load("collector_token_usage", [{"token": token}], "delete")
        except Exception as e:
            print(f"warn: usage row delete: {e}", file=sys.stderr)
        return email

    # An admin wants to know which machines are still reporting, but a write
    # per ingest would be absurd, so the stamp is refreshed at most hourly.
    LAST_USED_RESOLUTION = 3600

    def collector_token_user(self, token):
        """The account a collector token belongs to, or None."""
        if not token or not re.match(r"^[A-Za-z0-9_-]{20,64}$", token):
            return None
        # Never join here: hotdata errors on a declared-but-empty table, and a
        # LEFT JOIN against the (initially empty) usage table would empty this
        # result and reject a perfectly good credential.
        rows = self.sysdb.rows(f"SELECT token, user_email FROM {SYS}.public.collector_tokens "
                               f"WHERE token = {sql_str(token)}")
        if not rows:
            return None
        try:
            # inside the guard: reading the stamp is as fallible as writing it,
            # and neither may stand between a valid token and its owner
            self._touch_token(token)
        except Exception as e:
            print(f"warn: last_used_at: {e}", file=sys.stderr)
        return rows[0]["user_email"]

    @staticmethod
    def _age_seconds(value):
        """Seconds since `value`, which the store hands back as a datetime
        (aware or naive) or a string. None when it cannot be read as a time --
        the caller then treats the stamp as stale, which is the safe way to be
        wrong about a usage timestamp."""
        if value is None:
            return None
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
        if not isinstance(value, datetime):
            return None
        now = datetime.now(timezone.utc)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return (now - value).total_seconds()

    def _touch_token(self, token):
        """Refresh the usage stamp for `token` if it has gone stale. The caller
        wraps this: nothing here may fail an authentication."""
        prev = self.sysdb.rows(
            f"SELECT last_used_at FROM {SYS}.public.collector_token_usage "
            f"WHERE token = {sql_str(token)}")
        age = self._age_seconds(prev[0]["last_used_at"]) if prev else None
        if age is not None and age < self.LAST_USED_RESOLUTION:
            return
        # a row here grants nothing; it is swept when its token is revoked,
        # and an orphan is harmless
        self.sysdb.load("collector_token_usage", [{
            "token": token,
            "last_used_at": datetime.now(timezone.utc).isoformat(),
        }], "upsert")

    def seed(self):
        """First boot: system tables; org hotdata (with its dedicated database)
        and eddie@hotdata.dev as the first user."""
        self.sysdb.ensure_schema_and_tables(SYSTEM_TABLES)
        if self.sysdb.rows(f"SELECT email FROM {SYS}.public.users LIMIT 1"):
            return None
        password = secrets.token_urlsafe(12)
        self.ensure_org("hotdata", "hotdata")
        self.create_user("eddie@hotdata.dev", password, "hotdata")
        self.set_admin("eddie@hotdata.dev", "hotdata")
        self.set_system_admin("eddie@hotdata.dev")
        return password


# ---------------------------------------------------------------------------
# StorePool: cached dashboard reads, one cache per org database.
# ---------------------------------------------------------------------------
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")

class HotdataStore:
    def __init__(self, hd, ttl=60):
        self.hd = hd
        self.ttl = ttl
        self.cache = {}
        self.encoded = {}
        self.lock = threading.Lock()

    def invalidate(self):
        with self.lock:
            self.cache.clear()
            self.encoded.clear()

    def encoded_payload(self, key, build, fresh=False):
        """Gzipped JSON for one viewer, cached beside the rows it is built
        from. Repeat loads then cost a dict lookup instead of a few MB of
        serialisation and compression."""
        now = time.time()
        with self.lock:
            hit = self.encoded.get(key)
            if hit and not fresh and now - hit[0] < self.ttl:
                return hit[1]
        body = gzip.compress(json.dumps(build()).encode(), 6)
        with self.lock:
            self.encoded[key] = (now, body)
        return body

    def _cached(self, key, fn, fresh=False):
        with self.lock:
            hit = self.cache.get(key)
            if hit and not fresh and time.time() - hit[0] < self.ttl:
                return hit[1]
        val = fn()
        with self.lock:
            self.cache[key] = (time.time(), val)
        return val

    def data(self, fresh=False, days=None):
        key = f"data:{days or 'all'}"
        return self._cached(key, lambda: self._fetch_data(days), fresh)

    def _fetch_data(self, days=None):
        since = ""
        if days:
            cut = (datetime.now(timezone.utc) - timedelta(days=int(days))).date().isoformat()
            since = f" WHERE ended_at >= DATE '{cut}'"
        sess_rows = self.hd.rows(f"SELECT * FROM {core.CATALOG}.public.sessions{since}")
        daily_since = f" WHERE day >= DATE '{cut}'" if days else ""
        daily_rows = self.hd.rows(
            f"SELECT * FROM {core.CATALOG}.public.daily_usage{daily_since}")
        sessions = [{
            "id": r["session_id"],
            "user": r.get("user_email"),
            "host": r.get("hostname"),
            "provider": r["provider"],
            "project": r["project"],
            "title": r["title"],
            "start": r["started_at"],
            "end": r["ended_at"],
            "requests": r["requests"],
            "models": [m for m in (r["models"] or "").split(",") if m],
            "in": r["input_tokens"],
            "out": r["output_tokens"],
            "cr": r["cache_read_tokens"],
            "cw": r["cache_write_tokens"],
            "cin": r["cost_input"],
            "cout": r["cost_output"],
            "ccr": r["cost_cache_read"],
            "ccw": r["cost_cache_write"],
            "cost": r["cost_total"],
            "peakCtx": r["peak_context_tokens"],
        } for r in sess_rows]
        sessions.sort(key=lambda s: s["end"] or "", reverse=True)
        daily = [{
            "s": r["session_id"],
            "d": str(r["day"])[:10],
            "in": r["input_tokens"],
            "out": r["output_tokens"],
            "cr": r["cache_read_tokens"],
            "cw": r["cache_write_tokens"],
            "cin": r["cost_input"],
            "cout": r["cost_output"],
            "ccr": r["cost_cache_read"],
            "ccw": r["cost_cache_write"],
        } for r in daily_rows]
        return {
            "generatedAt": datetime.now(timezone.utc).isoformat(),
            "source": f"hotdata {self.hd.db}",
            "sessions": sessions,
            "daily": daily,
        }

    def detail(self, session_id):
        if not SESSION_ID_RE.match(session_id):
            return None
        def fetch():
            rows = self.hd.rows(
                f"SELECT ts, context_tokens, output_tokens FROM {core.CATALOG}.public.requests "
                f"WHERE session_id = '{session_id}' ORDER BY seq")
            return [{"t": r["ts"], "ctx": r["context_tokens"], "out": r["output_tokens"]}
                    for r in rows]
        def fetch_cwd():
            rows = self.hd.rows(
                f"SELECT cwd FROM {core.CATALOG}.public.sessions "
                f"WHERE session_id = '{session_id}' LIMIT 1")
            return rows[0]["cwd"] if rows else ""
        # cwd is a long string on every row and is only ever shown here, so it
        # rides with the expansion rather than with the whole list. It also
        # decides whether the session exists at all: a session with no request
        # rows yet is real, just not chartable.
        cwd = self._cached("cwd:" + session_id, fetch_cwd)
        if not cwd:
            return None
        return {"id": session_id, "detail": self._cached("detail:" + session_id, fetch),
                "cwd": cwd}


class StorePool:
    def __init__(self, pool, ttl):
        self.pool = pool
        self.ttl = ttl
        self.stores = {}
        self.lock = threading.Lock()

    def get(self, database_id):
        with self.lock:
            if database_id not in self.stores:
                self.stores[database_id] = HotdataStore(self.pool.get(database_id), self.ttl)
            return self.stores[database_id]


# ---------------------------------------------------------------------------
# Ingest: route by the reporting user's org and upsert into that org's db.
# ---------------------------------------------------------------------------
def apply_ingest(auth, pool, payload):
    user = (payload.get("user_email") or "").strip().lower()
    host = payload.get("hostname") or ""
    if not user:
        raise ValueError("user_email is required")
    route = auth.route_for_email(user)
    if not route:
        # registration creates the account before any org, so "no route" now
        # has two causes and only one of them needs an admin
        if auth.get_user(user):
            raise PermissionError(
                f"{user} has no organization yet; create or join one "
                f"(sign in and follow /setup) before their usage is accepted")
        raise PermissionError(
            f"{user} is not a registered user; an admin must add them "
            f"(server.py adduser {user} --org <slug>) before their usage is accepted")
    org_slug, db_id = route
    sessions = payload.get("sessions") or []
    if not sessions:
        return 0, org_slug, db_id
    ids = {s["session_id"] for s in sessions}
    hd = pool.get(db_id)
    hd.load("sessions", [{**s, "user_email": user, "hostname": host} for s in sessions],
            "upsert")
    hd.load("requests", [{**r, "user_email": user} for r in (payload.get("requests") or [])
                         if r["session_id"] in ids], "upsert")
    hd.load("daily_usage", [{**r, "user_email": user} for r in (payload.get("daily") or [])
                            if r["session_id"] in ids], "upsert")
    return len(sessions), org_slug, db_id


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    pool = None        # ClientPool (org databases)
    stores = None      # StorePool
    auth = None        # AuthStore (system database)
    token = None       # ingest bearer token ('' = dev mode, accept all)
    max_body = 64 * 1024 * 1024

    # Below this, framing and headers cost more than the bytes saved.
    GZIP_MIN = 1024

    def _accepts_gzip(self):
        return "gzip" in self.headers.get("Accept-Encoding", "").lower()

    def _send(self, code, body, ctype, extra=None, gzipped=False, cache=None):
        extra = list(extra or [])
        if gzipped:
            extra.append(("Content-Encoding", "gzip"))
        elif len(body) >= self.GZIP_MIN and self._accepts_gzip():
            # the dashboard payload is a few MB of repetitive JSON; nothing
            # sits in front of this server to compress it (the App Runner
            # hostnames are DNS-only, not proxied), so do it here
            body = gzip.compress(body, 6)
            extra.append(("Content-Encoding", "gzip"))
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # no-store is the right default: API responses are private, per-viewer
        # data. Static assets override it -- see _static.
        self.send_header("Cache-Control", cache or "no-store")
        for k, v in (extra or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _redirect(self, location, extra=None):
        self.send_response(302)
        self.send_header("Location", location)
        for k, v in (extra or []):
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _cookie_token(self):
        header = self.headers.get("Cookie", "")
        for part in header.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "hotusage_session":
                return v
        return None

    def _viewer(self):
        return self.auth.user_for_token(self._cookie_token())

    # naive per-instance, per-IP limiter for the unauthenticated write paths
    # (register + invite accept both create users; register also provisions a
    # hotdata database). App Runner may run several instances, so the real
    # ceiling is limit x instances -- still enough to blunt abuse.
    _rate = {}
    _rate_lock = threading.Lock()
    RATE_LIMIT, RATE_WINDOW = 5, 3600
    INVITE_RATE_LIMIT = 30

    def _rate_limited(self, bucket, limit=None, record=True):
        # rightmost X-Forwarded-For entry: proxies (App Runner/ALB) append the
        # real peer to a client-supplied header, so position 0 is spoofable.
        fwd = self.headers.get("X-Forwarded-For", "")
        ip = fwd.split(",")[-1].strip() if fwd.strip() else self.client_address[0]
        key = f"{bucket}:{ip}"
        now = time.time()
        with Handler._rate_lock:
            for k in [k for k, v in Handler._rate.items()
                      if now - v[-1] >= self.RATE_WINDOW]:
                del Handler._rate[k]
            hits = [t for t in Handler._rate.get(key, []) if now - t < self.RATE_WINDOW]
            if len(hits) >= (limit or self.RATE_LIMIT):
                Handler._rate[key] = hits
                return True
            # record=False asks "would this be limited?" without spending a hit,
            # for callers that only want to charge for work they actually did
            if record:
                hits.append(now)
            Handler._rate[key] = hits
        return False

    def _public_origin(self):
        """(scheme, host) as the outside world sees us. Behind App Runner the
        proxy sets both headers; run directly (dev) there is no TLS, so `http`
        is the honest default rather than a link that cannot be opened."""
        host = self.headers.get("X-Forwarded-Host", self.headers.get("Host", ""))
        proto = self.headers.get("X-Forwarded-Proto", "http")
        return proto, host

    def _page(self, name, subs=None):
        """Serve a static page with {{PLACEHOLDER}} substitution, HTML-escaped."""
        fp = os.path.join(STATIC_DIR, name)
        with open(fp, encoding="utf-8") as f:
            body = f.read()
        for k, v in (subs or {}).items():
            body = body.replace("{{" + k + "}}", html.escape(str(v)))
        self._send(200, body.encode(), "text/html; charset=utf-8")

    def _read_form(self):
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > 64 * 1024:
            return {}
        body = self.rfile.read(length).decode(errors="replace")
        return {k: v[0] for k, v in parse_qs(body).items()}

    @staticmethod
    def _safe_next(value):
        """Only same-site absolute paths: never bounce a login to another host.
        A backslash counts as a slash here -- browsers normalise `/\\evil.com`
        to `//evil.com` before resolving Location, so `//` alone is not enough."""
        value = value or ""
        # `$` would match before a trailing newline, letting a CR/LF ride into
        # the Location header (response splitting), so anchor with \Z and
        # reject control characters outright.
        if re.match(r"^/(\Z|[^/\\])", value) and not re.search(r"[\x00-\x1f\x7f]", value):
            return value
        return "/"

    def _handle_login(self):
        form = self._read_form()
        nxt = self._safe_next(form.get("next", "/"))
        token = self.auth.login(form.get("email", ""), form.get("password", ""))
        if not token:
            time.sleep(0.3)  # soften brute force
            self._redirect("/login?err=1&next=" + quote(nxt))
            return
        cookie = (f"hotusage_session={token}; HttpOnly; SameSite=Lax; Path=/; "
                  f"Max-Age={SESSION_TTL}")
        self._redirect(nxt, extra=[("Set-Cookie", cookie)])

    def _handle_register(self):
        if self._rate_limited("register"):
            self._redirect("/register?err=Too+many+attempts%3B+try+again+later")
            return
        form = self._read_form()
        email = (form.get("email") or "").strip().lower()
        password = form.get("password") or ""
        try:
            if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
                raise ValueError("that does not look like an email address")
            if len(password) < 8:
                raise ValueError("password must be at least 8 characters")
            self.auth.create_account(email, password)
        except ValueError as e:
            self._redirect("/register?err=" + quote(str(e)))
            return
        token = self.auth.login(email, password)
        cookie = (f"hotusage_session={token}; HttpOnly; SameSite=Lax; Path=/; "
                  f"Max-Age={SESSION_TTL}")
        # the account exists now; _needs_org sends them on to make one
        self._redirect("/setup", extra=[("Set-Cookie", cookie)])

    def _handle_setup(self):
        """Create the org for a signed-in account that has none."""
        viewer = self._viewer()
        if not viewer:
            self._redirect("/login?next=%2Fsetup")
            return
        form = self._read_form()
        nxt = self._safe_next(form.get("next", "/"))
        if viewer.get("org_slug"):  # already belongs somewhere: nothing to make
            self._redirect(nxt)
            return
        # its own bucket: creating an org provisions a database, which is the
        # expensive half of signup, but it should not eat the registration
        # budget a team behind one NAT is sharing. Checked without recording --
        # see the record below.
        if self._rate_limited("setup", record=False):
            self._redirect("/setup?err=Too+many+attempts%3B+try+again+later")
            return
        try:
            self.auth.create_org_for(viewer["email"], form.get("org_name", ""))
        except ValueError as e:
            # a refusal costs a name, not a database, and must not count: every
            # other page sends this account back here, so spending the budget on
            # five taken names would leave it able to reach nothing but /logout
            self._redirect("/setup?err=" + quote(str(e)))
            return
        self._rate_limited("setup")  # the database is what the budget is for
        self._redirect(nxt)

    def _handle_invite_accept(self, token):
        # a team link is meant to be used by a whole team, often behind one
        # office NAT, so this bucket is looser than register's
        if self._rate_limited("invite", limit=self.INVITE_RATE_LIMIT):
            self._json({"error": "too many attempts; try again later"}, 429)
            return
        form = self._read_form()
        password = form.get("password") or ""
        viewer = self._viewer()
        as_user = viewer["email"] if viewer else None
        try:
            inv = self.auth.get_invite(token)
            joining_existing = (
                (inv and self.auth.get_user(inv["email"])) or
                (not inv and form.get("email") and
                 self.auth.get_user(form.get("email", ""))))
            if not joining_existing and len(password) < 8:
                raise ValueError("password must be at least 8 characters")
            if inv:
                email = self.auth.accept_invite(token, password, as_user=as_user)
            else:
                # reusable team link: the joiner supplies their own address
                email = self.auth.accept_team_invite(
                    token, form.get("email", ""), password, as_user=as_user)
        except ValueError as e:
            self._redirect(f"/invite/{quote(token, safe='')}?err=" + quote(str(e))
                           + "&email=" + quote(form.get("email", "")[:120]))
            return
        if viewer and viewer["email"] == email:
            # already signed in; switch_org cleared the token cache, so the
            # next request re-reads the new active org
            self._redirect("/")
            return
        session = self.auth.login(email, password)
        cookie = (f"hotusage_session={session}; HttpOnly; SameSite=Lax; Path=/; "
                  f"Max-Age={SESSION_TTL}")
        self._redirect("/", extra=[("Set-Cookie", cookie)])

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/register":
            try:
                self._handle_register()
            except Exception as e:
                print(f"error: /register: {e}", file=sys.stderr)
                self._json({"error": "registration failed"}, 500)
            return
        if path == "/setup":
            try:
                self._handle_setup()
            except Exception as e:
                print(f"error: /setup: {e}", file=sys.stderr)
                self._redirect("/setup?err=could+not+create+that+organization")
            return
        if path.startswith("/invite/"):
            try:
                self._handle_invite_accept(path.rsplit("/", 1)[1])
            except Exception as e:
                print(f"error: /invite: {e}", file=sys.stderr)
                self._json({"error": "invite failed"}, 500)
            return
        if path == "/api/invite":
            try:
                viewer = self._viewer()
                if not viewer:
                    self._json({"error": "unauthorized"}, 401)
                    return
                # inviting grows the org: management, so admins only
                if not self.auth.is_admin(viewer["email"], viewer["org_slug"]):
                    self._json({"error": "only an organization admin can invite"}, 403)
                    return
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length)) if 0 < length <= 4096 else {}
                proto, host = self._public_origin()
                if payload.get("kind") == "team":
                    token = self.auth.create_team_invite(
                        viewer["org_slug"], viewer["email"],
                        domain=payload.get("domain", ""),
                        max_uses=payload.get("max_uses", 0),
                        expires_days=payload.get("expires_days"))
                    inv = self.auth.get_team_invite(token)
                    self._json({"ok": True, "kind": "team",
                                "link": f"{proto}://{host}/invite/{token}",
                                "domain": inv["domain"],
                                "max_uses": int(inv["max_uses"]),
                                "expires_days": round(
                                    (float(inv["expires_at"]) - time.time()) / 86400)})
                    return
                token = self.auth.create_invite(
                    payload.get("email", ""), viewer["org_slug"], viewer["email"])
                self._json({"ok": True, "kind": "single",
                            "link": f"{proto}://{host}/invite/{token}",
                            "expires_days": AuthStore.INVITE_TTL // 86400})
            except ValueError as e:
                self._json({"error": str(e)}, 400)
            except Exception as e:
                print(f"error: /api/invite: {e}", file=sys.stderr)
                self._json({"error": str(e)[:300]}, 500)
            return
        if path == "/api/device/start":
            try:
                if self._rate_limited("device", limit=self.INVITE_RATE_LIMIT):
                    self._json({"error": "too many attempts; try again later"}, 429)
                    return
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length)) if 0 < length <= 4096 else {}
                device_code, user_code = self.auth.start_device_auth(
                    payload.get("hostname", ""))
                proto, host = self._public_origin()
                self._json({"device_code": device_code, "user_code": user_code,
                            "verification_url": f"{proto}://{host}/device?code={user_code}",
                            "expires_in": AuthStore.DEVICE_CODE_TTL,
                            "interval": AuthStore.DEVICE_POLL_INTERVAL})
            except Exception as e:
                print(f"error: /api/device/start: {e}", file=sys.stderr)
                self._json({"error": "could not start sign-in"}, 500)
            return
        if path.startswith("/api/admin/"):
            try:
                viewer = self._viewer()
                if not viewer:
                    self._json({"error": "unauthorized"}, 401)
                    return
                org = viewer["org_slug"]
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if 0 < length <= 4096 else {}
                action = path[len("/api/admin/"):]
                target = (body.get("email") or "").strip().lower()

                # platform actions: gated on system admin, not org admin.
                # set-org-database belongs here rather than with the org-admin
                # actions: an org admin who pointed their org at another org's
                # database would be reading that org's usage.
                if action in ("create-org", "delete-org", "set-org-database"):
                    if not self.auth.is_system_admin(viewer["email"]):
                        self._json({"error": "only a system admin can manage organizations"}, 403)
                        return
                    if action == "create-org":
                        owner = (body.get("owner_email") or "").strip().lower()
                        if owner:  # refuse before the database exists
                            # an existing account is fine: accepting adds the
                            # new org as a membership
                            if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", owner):
                                raise ValueError("that does not look like an email address")
                        slug = self.auth.create_org(body.get("name", ""))
                        out = {"ok": True, "slug": slug}
                        if owner:
                            # a single-use invite whose acceptor, being the
                            # org's first member, becomes its admin
                            token = self.auth.create_invite(owner, slug, viewer["email"])
                            proto, host = self._public_origin()
                            out["invite_link"] = f"{proto}://{host}/invite/{token}"
                        self._json(out)
                    elif action == "delete-org":
                        if body.get("slug") == viewer["org_slug"]:
                            raise ValueError("you cannot delete your own organization")
                        db = self.auth.delete_empty_org(body.get("slug", ""))
                        self._json({"ok": True, "database_kept": db})
                    else:
                        previous = self.auth.set_org_database(
                            (body.get("slug") or "").strip(),
                            body.get("database_id", ""))
                        self._json({"ok": True, "previous": previous})
                    return

                if not self.auth.is_admin(viewer["email"], org):
                    self._json({"error": "only an organization admin can do that"}, 403)
                    return

                if action == "remove-user":
                    if target == viewer["email"]:
                        raise ValueError("you cannot remove yourself")
                    if not self.auth.get_user(target) or \
                            org not in self.auth.memberships(target):
                        raise ValueError("that person is not in this organization")
                    # an org admin's writ ends at their own org: removal here
                    # strips THIS membership; the account itself dies only
                    # when this was its last org
                    self.auth.remove_from_org(target, org)
                    self._json({"ok": True})

                elif action == "set-admin":
                    if not self.auth.get_user(target) or \
                            org not in self.auth.memberships(target):
                        raise ValueError("that person is not in this organization")
                    on = bool(body.get("admin"))
                    # an org with no admin can never be managed again
                    if not on and self.auth.admins(org) == {target}:
                        raise ValueError("an organization needs at least one admin")
                    self.auth.set_admin(target, org, on)
                    self._json({"ok": True})

                elif action == "revoke-invite":
                    self.auth.revoke_any_invite(body.get("token", ""), org)
                    self._json({"ok": True})

                elif action == "revoke-token":
                    self.auth.revoke_collector_token_for_org(body.get("token", ""), org)
                    self._json({"ok": True})

                elif action == "rename-org":
                    name = (body.get("name") or "").strip()[:60]
                    if len(name) < 2:
                        raise ValueError("organization name must be at least 2 characters")
                    self.auth.rename_org(org, name)
                    self._json({"ok": True, "name": name})

                else:
                    self._json({"error": "not found"}, 404)
            except ValueError as e:
                self._json({"error": str(e)}, 400)
            except Exception as e:
                print(f"error: {path}: {e}", file=sys.stderr)
                self._json({"error": "that did not work"}, 500)
            return
        if path == "/api/switch-org":
            try:
                viewer = self._viewer()
                if not viewer:
                    self._json({"error": "unauthorized"}, 401)
                    return
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if 0 < length <= 4096 else {}
                self.auth.switch_org(viewer["email"], body.get("slug", ""))
                self._json({"ok": True, "active": body.get("slug", "")})
            except ValueError as e:
                self._json({"error": str(e)}, 400)
            except Exception as e:
                print(f"error: /api/switch-org: {e}", file=sys.stderr)
                self._json({"error": "could not switch"}, 500)
            return
        if path == "/api/collector/signout":
            try:
                auth = self.headers.get("Authorization", "")
                presented = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
                email = self.auth.revoke_collector_token(presented)
                if not email:
                    # already gone, or a shared token that owns no row: the
                    # collector is signed out either way, so do not fail it
                    self._json({"ok": True, "revoked": False})
                    return
                print(f"signout: collector token for {email} revoked")
                self._json({"ok": True, "revoked": True, "user_email": email})
            except Exception as e:
                print(f"error: /api/collector/signout: {e}", file=sys.stderr)
                self._json({"error": "could not sign out"}, 500)
            return
        if path == "/api/device/poll":
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length)) if 0 < length <= 4096 else {}
                result = self.auth.poll_device(payload.get("device_code", ""))
                if not result:
                    self._json({"status": "pending"})
                    return
                email, token = result
                self._json({"status": "approved", "user_email": email, "token": token})
            except ValueError as e:
                self._json({"status": "expired", "error": str(e)}, 400)
            except Exception as e:
                print(f"error: /api/device/poll: {e}", file=sys.stderr)
                self._json({"error": "could not check sign-in"}, 500)
            return
        if path == "/device/approve":
            try:
                viewer = self._viewer()
                if not viewer:
                    self._redirect("/login")
                    return
                form = self._read_form()
                code = form.get("user_code", "")
                try:
                    self.auth.approve_device(code, viewer["email"])
                except ValueError as e:
                    self._redirect("/device?err=" + quote(str(e)))
                    return
                self._redirect("/device?ok=1")
            except Exception as e:
                print(f"error: /device/approve: {e}", file=sys.stderr)
                self._json({"error": "approval failed"}, 500)
            return
        if path == "/login":
            try:
                self._handle_login()
            except Exception as e:
                print(f"error: /login: {e}", file=sys.stderr)
                self._json({"error": "login failed"}, 500)
            return
        if path != "/ingest":
            self._json({"error": "not found"}, 404)
            return
        try:
            # Two credentials are accepted. A per-user collector token (minted
            # by the sign-in flow) also *identifies* the reporter, so it
            # overrides whatever address the payload claims. The shared ingest
            # token only admits: it cannot say who is reporting, so the claimed
            # address stands. Prefer the former.
            presented = self.headers.get("Authorization", "")[len("Bearer "):] \
                if self.headers.get("Authorization", "").startswith("Bearer ") else ""
            shared = bool(self.token) and presented == self.token
            owner = None if shared or not presented \
                else self.auth.collector_token_user(presented)
            if not owner and self.token and not shared:
                self._json({"error": "unauthorized"}, 401)
                return
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0 or length > self.max_body:
                self._json({"error": "bad content length"}, 400)
                return
            payload = json.loads(self.rfile.read(length))
            if owner:
                payload["user_email"] = owner
            n, org_slug, db_id = apply_ingest(self.auth, self.pool, payload)
            if n:
                self.stores.get(db_id).invalidate()
            self._json({"ok": True, "sessions": n, "org": org_slug})
            print(f"ingest: {payload.get('user_email')}@{payload.get('hostname')}: "
                  f"{n} sessions -> org '{org_slug}' ({db_id})")
        except PermissionError as e:
            self._json({"error": str(e)}, 403)
        except (ValueError, KeyError) as e:
            self._json({"error": f"bad payload: {e}"}, 400)
        except Exception as e:
            print(f"error: /ingest: {e}", file=sys.stderr)
            self._json({"error": str(e)[:500]}, 500)

    # /healthz is public and unrated, and each deep probe costs one upstream
    # call, so the result is cached: many probes, at most one call per window.
    _health_cache = (0.0, None)
    _health_ttl = 30

    def _hotdata_health(self):
        """Coarse backend status for /healthz?deep=1 - never leaks details."""
        ts, cached = Handler._health_cache
        if cached and time.time() - ts < Handler._health_ttl:
            return cached
        status = self._probe_hotdata()
        Handler._health_cache = (time.time(), status)
        return status

    def _probe_hotdata(self):
        if not core.hotdata_api_key():
            return "no_api_key"
        try:
            # no table is named, so nothing here can be legitimately "missing"
            self.auth.sysdb.sql("SELECT 1 AS ok", timeout=8)
            return "ok"
        except Exception as e:
            msg = str(e).lower()
            if "401" in msg or "unauthorized" in msg or "forbidden" in msg or "403" in msg:
                return "auth_rejected"
            if "not found" in msg or "404" in msg:
                # the query names no table: a not-found means hotdata rejected
                # the database id or the workspace, which is a real failure
                return "db_not_found"
            if "timed out" in msg or "timeout" in msg or "connection" in msg:
                return "unreachable"
            return "error"

    # Only the windows the dashboard offers. Each distinct value pins a
    # payload and a row list in caches that never evict, so an arbitrary
    # integer here is a memory-exhaustion lever for any signed-in member.
    WINDOWS = (7, 30, 90)

    @classmethod
    def _window_days(cls, query):
        """`?days=N` bounds the first load; anything else means everything."""
        raw = (parse_qs(query).get("days", [""])[0] or "").strip().lower()
        if not raw or raw == "all":
            return None
        try:
            want = int(raw)
        except ValueError:
            return None
        # snap up to the nearest offered window; wider than the largest is
        # simply "everything", which is what the All time view asks for
        return next((w for w in cls.WINDOWS if w >= want), None)

    def _org_payload(self, viewer, fresh=False, days=None):
        """The dashboard payload: the viewer's org database, whole."""
        if not viewer.get("database_id"):
            return {"generatedAt": datetime.now(timezone.utc).isoformat(),
                    "source": "no org database",
                    "viewer": {"email": viewer["email"], "org": viewer["org_name"]},
                    "sessions": [], "daily": []}
        data = self.stores.get(viewer["database_id"]).data(fresh=fresh, days=days)
        return {**data, "windowDays": days,
                "viewer": {"email": viewer["email"], "org": viewer["org_name"]}}

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        fresh = parse_qs(parsed.query).get("fresh", ["0"])[0] == "1"
        try:
            # public routes
            if path == "/healthz":
                # liveness only (no hotdata round-trip): App Runner polls this.
                # ?deep=1 additionally reports whether the hotdata backend is
                # usable, classified rather than echoed, so a deployment can be
                # diagnosed without shell or log access. No secret or raw error
                # text is ever returned.
                if parse_qs(parsed.query).get("deep", ["0"])[0] == "1":
                    self._json({"ok": True, "hotdata": self._hotdata_health()})
                else:
                    self._json({"ok": True})
                return
            if path == "/login":
                nxt = self._safe_next(parse_qs(parsed.query).get("next", ["/"])[0])
                self._page("login.html", {"NEXT": nxt})
                return
            if path == "/register":
                err = parse_qs(parsed.query).get("err", [""])[0]
                self._page("register.html", {"ERROR": err[:200]})
                return
            if path == "/setup":
                viewer = self._viewer()
                if not viewer:
                    self._redirect("/login?next=%2Fsetup")
                    return
                if viewer.get("org_slug"):  # already in one: nothing to ask for
                    self._redirect("/")
                    return
                q = parse_qs(parsed.query)
                self._page("setup.html", {
                    "ERROR": q.get("err", [""])[0][:200], "EMAIL": viewer["email"],
                    "NEXT": self._safe_next(q.get("next", ["/"])[0])})
                return
            if path.startswith("/invite/"):
                token = path.rsplit("/", 1)[1]
                err = parse_qs(parsed.query).get("err", [""])[0]
                inv = self.auth.get_invite(token)
                if inv:  # single-use: the address is fixed by the inviter
                    viewer = self._viewer()
                    mode = "new"
                    if self.auth.get_user(inv["email"]):
                        mode = "join" if viewer and viewer["email"] == inv["email"] \
                            else "login"
                    self._page("invite.html", {"TOKEN": token, "EMAIL": inv["email"],
                                               "FIXED": "1", "HINT": "", "MODE": mode,
                                               "ORG": inv["org_name"] or inv["org_slug"],
                                               "ERROR": err[:200]})
                    return
                team = self.auth.get_team_invite(token)
                if team:  # reusable link: the joiner types their own address
                    hint = (f"Use your @{team['domain']} email address."
                            if team["domain"] else "")
                    typed = parse_qs(parsed.query).get("email", [""])[0][:120]
                    self._page("invite.html", {"TOKEN": token, "EMAIL": typed,
                                               "FIXED": "", "HINT": hint, "MODE": "new",
                                               "ORG": team["org_name"] or team["org_slug"],
                                               "ERROR": err[:200]})
                    return
                self._page("invite.html", {"TOKEN": "", "EMAIL": "", "ORG": "",
                                           "FIXED": "", "HINT": "", "MODE": "new",
                                           "ERROR": "This invite is invalid, expired, "
                                                    "or fully used."})
                return
            if path == "/admin":
                viewer = self._viewer()
                if not viewer:
                    self._redirect("/login?next=" + quote(self.path))
                    return
                if not viewer.get("org_slug"):  # nothing to administer yet
                    self._redirect("/setup?next=" + quote(self.path))
                    return
                self._static("admin.html")
                return
            if path == "/device":
                q = parse_qs(parsed.query)
                viewer = self._viewer()
                if not viewer:
                    # sign in first, then come back to this exact approval
                    self._redirect("/login?next=" + quote(self.path))
                    return
                # a collector reports into its owner's active org, so there has
                # to be one before a device can be approved at all. Carry the
                # code through: it is in the URL the collector printed, and
                # losing it means going back to the terminal for it.
                if not viewer.get("org_slug"):
                    self._redirect("/setup?next=" + quote(self.path))
                    return
                if q.get("ok"):
                    self._page("device.html", {"STATE": "done", "CODE": "", "HOST": "",
                                               "EMAIL": viewer["email"], "ERROR": ""})
                    return
                code = (q.get("code", [""])[0] or "").strip().upper()
                row = self.auth.get_device_by_user_code(code)
                if not row or row["approved_email"]:
                    self._page("device.html", {"STATE": "bad", "CODE": code, "HOST": "",
                                               "EMAIL": viewer["email"],
                                               "ERROR": q.get("err", [""])[0][:200] or
                                               "That sign-in request is invalid, "
                                               "expired, or already approved."})
                    return
                self._page("device.html", {"STATE": "confirm", "CODE": code,
                                           "HOST": row["hostname"] or "an unnamed machine",
                                           "EMAIL": viewer["email"],
                                           "ERROR": q.get("err", [""])[0][:200]})
                return
            if path.startswith("/static/"):
                self._static(path[len("/static/"):])
                return
            if path == "/logout":
                token = self._cookie_token()
                if token:
                    self.auth.logout(token)
                self._redirect("/login", extra=[
                    ("Set-Cookie", "hotusage_session=; Path=/; Max-Age=0")])
                return

            viewer = self._viewer()
            if not viewer:
                if path.startswith("/api/"):
                    self._json({"error": "unauthorized"}, 401)
                else:
                    self._redirect("/login")
                return

            # registration creates the account alone, so a viewer can be signed
            # in with no org. Everything below reads or writes one org's data,
            # so ask for the org first rather than letting each handler meet an
            # empty slug.
            if not viewer.get("org_slug"):
                if path.startswith("/api/"):
                    self._json({"error": "you do not belong to an organization yet"}, 409)
                else:
                    self._redirect("/setup?next=" + quote(self.path))
                return

            if path == "/" or path == "/index.html":
                self._static("index.html")
            elif path == "/api/data":
                db = viewer.get("database_id")
                days = self._window_days(parsed.query)
                if db and self._accepts_gzip():
                    body = self.stores.get(db).encoded_payload(
                        f"{viewer['email']}:{days or 'all'}",
                        lambda: self._org_payload(viewer, fresh=fresh, days=days),
                        fresh=fresh)
                    self._send(200, body, "application/json", gzipped=True)
                else:
                    self._json(self._org_payload(viewer, fresh=fresh, days=days))
            elif path == "/api/admin/state":
                org, email = viewer["org_slug"], viewer["email"]
                # the privileged reads stay behind the role checks, so this is
                # two waves rather than one: what the viewer may see first,
                # then the reads that answer depends on it. Wave one is a
                # roster read, which already carries the admin set and the
                # active emails that wave two would otherwise re-query.
                who = gather(is_sysadmin=lambda: self.auth.is_system_admin(email),
                             roster=lambda: self.auth.roster(org))
                roster = who["roster"]
                is_admin = email in roster["admins"]
                payload = {
                    "viewer": {"email": email, "isAdmin": is_admin},
                    "org": {"slug": org, "name": viewer["org_name"],
                            "database": viewer.get("database_id")},
                    "members": roster["members"],
                }
                extra = {}
                if is_admin:  # invite tokens are credentials: admins only
                    extra["invites"] = lambda: self.auth.org_invites(org)
                    extra["collectors"] = lambda: self.auth.org_collectors(
                        org, active=roster["active"])
                if who["is_sysadmin"]:
                    extra["allOrgs"] = lambda: self.auth.all_orgs()
                    payload["viewer"]["isSystemAdmin"] = True
                payload.update(gather(extra))
                self._json(payload)
            elif path == "/api/orgs":
                slugs = sorted(self.auth.memberships(viewer["email"]))
                found = gather({s: (lambda s=s: self.auth.get_org(s)) for s in slugs})
                orgs = [{"slug": s, "name": (found[s] or {}).get("name") or s}
                        for s in slugs]
                self._json({"active": viewer["org_slug"], "orgs": orgs})
            elif path == "/api/status":
                self._json({"ok": True, "org": viewer["org_slug"],
                            "orgDatabase": viewer.get("database_id")})
            elif path.startswith("/api/session/"):
                sid = path.rsplit("/", 1)[1]
                if not viewer.get("database_id"):
                    self._json({"error": "session not found"}, 404)
                    return
                d = self.stores.get(viewer["database_id"]).detail(sid)
                if d:
                    self._json(d)
                else:
                    self._json({"error": "session not found"}, 404)
            else:
                self._send(404, b"not found", "text/plain")
        except BrokenPipeError:
            pass
        except Exception as e:
            print(f"error: {path}: {e}", file=sys.stderr)
            try:
                self._json({"error": str(e)[:500]}, 500)
            except BrokenPipeError:
                pass

    # Gzipped static assets, keyed by (path, mtime) so a deploy (new file,
    # new mtime) invalidates naturally. Small and bounded: one entry per file
    # in static/.
    _static_cache = {}
    _static_lock = threading.Lock()

    def _static(self, name):
        fp = os.path.normpath(os.path.join(STATIC_DIR, name))
        # separator-suffixed: a bare prefix check would admit a sibling
        # directory like static_other
        if not fp.startswith(STATIC_DIR + os.sep) or not os.path.isfile(fp):
            self._send(404, b"not found", "text/plain")
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css",
            ".js": "application/javascript",
            ".svg": "image/svg+xml",
        }.get(os.path.splitext(fp)[1], "application/octet-stream")
        # Assets change only on deploy, and App Runner replaces the instance
        # then, so a short public max-age is safe and spares every navigation
        # a re-download (no-store also forced this server to re-gzip each
        # request). HTML stays no-store: it is tiny and login-adjacent.
        is_html = fp.endswith(".html")
        cache = None if is_html else "public, max-age=300"
        mtime = os.path.getmtime(fp)
        key = (fp, mtime)
        with Handler._static_lock:
            hit = Handler._static_cache.get(key)
        if hit is None:
            with open(fp, "rb") as f:
                raw = f.read()
            hit = (raw, gzip.compress(raw, 6) if len(raw) >= self.GZIP_MIN else None)
            with Handler._static_lock:
                # drop stale mtimes for this path, then remember the new one
                for k in [k for k in Handler._static_cache if k[0] == fp]:
                    del Handler._static_cache[k]
                Handler._static_cache[key] = hit
        raw, gz = hit
        if gz is not None and self._accepts_gzip():
            self._send(200, gz, ctype, cache=cache, gzipped=True,
                       extra=[("Vary", "Accept-Encoding")])
        else:
            self._send(200, raw, ctype, cache=cache,
                       extra=[("Vary", "Accept-Encoding")])

    def log_message(self, fmt, *args):
        pass


ADMIN_VERBS = ("adduser", "addorg", "resetpw", "deluser", "delorg",
               "listusers", "listorgs", "listinvites", "revokeinvite",
               "listtokens", "revoketoken", "makeadmin", "unadmin",
               "makesysadmin", "unsysadmin")

def drop_usage_rows(auth, rows):
    """Best-effort cleanup of usage stamps. The credential delete is what
    revokes access; a leftover stamp row grants nothing, so it must never
    abort the work that follows it."""
    try:
        auth.sysdb.load("collector_token_usage", rows, "delete")
    except Exception as e:
        print(f"warn: usage row delete: {e}", file=sys.stderr)


def user_admin_cli(argv):
    """Admin subcommands against the system database; returns True if handled."""
    if not argv or argv[0] not in ADMIN_VERBS:
        return False
    verb = argv[0]
    ap = argparse.ArgumentParser(prog=f"server.py {verb}")
    if verb == "revoketoken":
        # optional: `revoketoken <token>` or `revoketoken --user <email>`
        ap.add_argument("target", nargs="?", default=None, help="the collector token")
    elif verb not in ("listusers", "listorgs", "listinvites", "listtokens"):
        ap.add_argument("target", help="email (user verbs) or org slug (org verbs)")
    ap.add_argument("--org", default="hotdata",
                    help="org slug for adduser / filter for listusers (default hotdata)")
    ap.add_argument("--name", default=None, help="display name for addorg")
    ap.add_argument("--user", default=None,
                    help="revoketoken: revoke every collector token for this address")
    ap.add_argument("--delete-database", action="store_true",
                    help="delorg only: also delete the org's usage database (destroys data)")
    ap.add_argument("--system-database", default=core.SYSTEM_DATABASE_ID)
    a = ap.parse_args(argv[1:])
    pool = ClientPool()
    auth = AuthStore(pool.get(a.system_database), pool)
    auth.sysdb.ensure_schema_and_tables(SYSTEM_TABLES)

    if verb == "addorg":
        db_id = auth.ensure_org(a.target, a.name)
        print(f"org '{a.target}' ready (database {db_id})")

    elif verb == "adduser":
        password = secrets.token_urlsafe(12)
        auth.create_user(a.target, password, a.org)
        print(f"created {a.target} in org '{a.org}'\ninitial password: {password}")

    elif verb == "resetpw":
        password = secrets.token_urlsafe(12)
        auth.set_password(a.target, password)
        print(f"new password for {a.target}: {password}")

    elif verb == "deluser":
        email = a.target.strip().lower()
        user = auth.get_user(email)
        if not user:
            sys.exit(f"no such user: {email}")
        toks = auth.sysdb.rows(f"SELECT token FROM {SYS}.public.auth_sessions "
                               f"WHERE user_email = {sql_str(email)}")
        if toks:
            auth.sysdb.load("auth_sessions", toks, "delete")
        toks = auth.sysdb.rows(f"SELECT token FROM {SYS}.public.collector_tokens "
                               f"WHERE user_email = {sql_str(email)}")
        if toks:
            auth.sysdb.load("collector_tokens", toks, "delete")
            drop_usage_rows(auth, toks)
        for slug in auth.memberships(email):
            auth.set_admin(email, slug, False)
            auth.sysdb.load("org_memberships",
                            [{"email": email, "org_slug": slug}], "delete")
        auth.set_system_admin(email, False)
        auth.sysdb.load("users", [{"email": email}], "delete")
        print(f"deleted {email} ({len(toks)} collector token(s) revoked; their "
              f"already-ingested usage stays in the org database)")

    elif verb == "delorg":
        org = auth.get_org(a.target, ttl=0)
        if not org:
            sys.exit(f"no such org: {a.target}")
        members = auth._member_rows(a.target)
        if members:
            sys.exit(f"org '{a.target}' still has {len(members)} member(s): "
                     + ", ".join(m["email"] for m in members)
                     + "\ndelete them first (server.py deluser <email>)")
        held = auth.sysdb.rows(f"SELECT email, org_slug FROM {SYS}.public.org_memberships "
                               f"WHERE org_slug = {sql_str(a.target)}")
        if held:
            auth.sysdb.load("org_memberships", held, "delete")
        grants = auth.sysdb.rows(f"SELECT org_slug, email FROM {SYS}.public.org_admins "
                                 f"WHERE org_slug = {sql_str(a.target)}")
        if grants:
            auth.sysdb.load("org_admins", grants, "delete")
        auth.sysdb.load("orgs", [{"slug": a.target}], "delete")
        if a.delete_database and org.get("database_id"):
            import hotdata
            hotdata.DatabasesApi(auth.sysdb.client()).delete_database(org["database_id"])
            print(f"deleted org '{a.target}' AND its database {org['database_id']}")
        else:
            print(f"deleted org '{a.target}'; its database {org.get('database_id')} was kept "
                  f"(pass --delete-database to remove it)")

    elif verb == "listorgs":
        orgs = auth.sysdb.rows(
            f"SELECT o.slug, o.name, o.database_id, count(u.email) AS users "
            f"FROM {SYS}.public.orgs o LEFT JOIN {SYS}.public.users u ON u.org_slug = o.slug "
            f"GROUP BY o.slug, o.name, o.database_id ORDER BY o.slug")
        for o in orgs:
            print(f"{o['slug']:<20} {o['users']:>3} user(s)  db={o['database_id']}  ({o['name']})")
        if not orgs:
            print("no orgs")

    elif verb == "listusers":
        where = f"WHERE org_slug = {sql_str(a.org)}" if a.org != "all" else ""
        users = auth.sysdb.rows(f"SELECT email, org_slug, created_at "
                                f"FROM {SYS}.public.users {where} ORDER BY email")
        for u in users:
            print(f"{u['email']:<36} org={u['org_slug']}  since {str(u['created_at'])[:10]}")
        if not users:
            print(f"no users (org filter: {a.org}; use --org all for everyone)")

    elif verb == "listinvites":
        single = auth.sysdb.rows(
            f"SELECT token, email, org_slug, expires_at FROM {SYS}.public.invites "
            f"ORDER BY expires_at")
        team = auth.sysdb.rows(
            f"SELECT token, org_slug, domain, max_uses, uses, expires_at "
            f"FROM {SYS}.public.team_invites ORDER BY expires_at")
        for i in single:
            left = (float(i["expires_at"]) - time.time()) / 86400
            print(f"single  {i['token']}  {i['email']}  org={i['org_slug']}  "
                  f"{left:.1f}d left")
        for t in team:
            left = (float(t["expires_at"]) - time.time()) / 86400
            cap = f"{t['uses']}/{t['max_uses']}" if t["max_uses"] else f"{t['uses']}/unlimited"
            dom = f"@{t['domain']}" if t["domain"] else "any domain"
            print(f"team    {t['token']}  {dom}  org={t['org_slug']}  used {cap}  "
                  f"{left:.1f}d left")
        if not single and not team:
            print("no outstanding invites")

    elif verb in ("makesysadmin", "unsysadmin"):
        user = auth.get_user(a.target)
        if not user:
            sys.exit(f"no such user: {a.target}")
        auth.set_system_admin(user["email"], verb == "makesysadmin")
        print(f"{user['email']} is {'now' if verb == 'makesysadmin' else 'no longer'} "
              f"a system admin")

    elif verb in ("makeadmin", "unadmin"):
        user = auth.get_user(a.target)
        if not user:
            sys.exit(f"no such user: {a.target}")
        on = verb == "makeadmin"
        if not on and auth.admins(user["org_slug"]) == {user["email"]}:
            sys.exit(f"{user['email']} is the only admin of '{user['org_slug']}'")
        auth.set_admin(user["email"], user["org_slug"], on)
        print(f"{user['email']} is now {'an admin' if on else 'a member'} "
              f"of '{user['org_slug']}'")

    elif verb == "listtokens":
        toks = auth.sysdb.rows(
            f"SELECT token, user_email, hostname, created_at "
            f"FROM {SYS}.public.collector_tokens ORDER BY user_email, created_at")
        # merged in Python, not joined: see collector_token_user
        used_by = {u["token"]: u["last_used_at"] for u in auth.sysdb.rows(
            f"SELECT token, last_used_at FROM {SYS}.public.collector_token_usage")}
        for t in toks:
            stamp = used_by.get(t["token"])
            used = str(stamp)[:16] if stamp else "never"
            print(f"{t['user_email']:<32} {t['hostname'] or '?':<20} "
                  f"since {str(t['created_at'])[:10]}  last used {used:<16}  {t['token']}")
        if not toks:
            print("no collector tokens (nobody has signed in a collector yet)")

    elif verb == "revoketoken":
        if not a.user and not a.target:
            sys.exit("pass a token, or --user <email> to revoke all of theirs")
        if a.user:
            rows = auth.sysdb.rows(f"SELECT token FROM {SYS}.public.collector_tokens "
                                   f"WHERE user_email = {sql_str(a.user.strip().lower())}")
            if not rows:
                sys.exit(f"no collector tokens for {a.user}")
            auth.sysdb.load("collector_tokens", rows, "delete")
            drop_usage_rows(auth, rows)
            print(f"revoked {len(rows)} collector token(s) for {a.user}")
        else:
            rows = auth.sysdb.rows(f"SELECT token FROM {SYS}.public.collector_tokens "
                                   f"WHERE token = {sql_str(a.target)}")
            if not rows:
                sys.exit("no such collector token")
            auth.sysdb.load("collector_tokens", [{"token": a.target}], "delete")
            drop_usage_rows(auth, [{"token": a.target}])
            print(f"revoked collector token {a.target}")

    elif verb == "revokeinvite":
        # match the raw rows, so an exhausted or expired link (which the live
        # getters correctly hide) can still be cleaned out of the table
        for table, label in (("invites", "single-use invite"), ("team_invites", "team link")):
            rows = auth.sysdb.rows(f"SELECT token FROM {SYS}.public.{table} "
                                   f"WHERE token = {sql_str(a.target)}")
            if rows:
                if table == "team_invites":
                    auth.revoke_team_invite(a.target)
                else:
                    auth.sysdb.load(table, [{"token": a.target}], "delete")
                print(f"revoked {label} {a.target}")
                break
        else:
            sys.exit("no such invite")
    return True


def main():
    if user_admin_cli(sys.argv[1:]):
        return

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8377)
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address; use 0.0.0.0 (behind a VPN/proxy) for company-wide collectors")
    ap.add_argument("--system-database", default=core.SYSTEM_DATABASE_ID,
                    help="hotdata database holding orgs/users/auth_sessions")
    ap.add_argument("--ttl", type=int, default=60, help="seconds to cache hotdata reads")
    args = ap.parse_args()

    Handler.pool = ClientPool()
    Handler.auth = AuthStore(Handler.pool.get(args.system_database), Handler.pool)

    # Seed must not block or crash boot: with hotdata cold/unreachable (or the
    # key not yet configured) the server still serves /healthz and retries in
    # the background; requests that need hotdata fail visibly until it's up.
    def try_seed():
        try:
            seeded_pw = Handler.auth.seed()
            if seeded_pw:
                print("=" * 62)
                print("seeded org 'hotdata' with first user eddie@hotdata.dev")
                print(f"initial password: {seeded_pw}")
                print("(change it with: python3 server/server.py resetpw eddie@hotdata.dev)")
                print("=" * 62)
            return True
        except Exception as e:
            print(f"warn: seed deferred (hotdata not reachable yet?): {str(e)[:300]}",
                  file=sys.stderr)
            return False

    if not try_seed():
        def seed_retry():
            while not try_seed():
                time.sleep(60)
        threading.Thread(target=seed_retry, daemon=True, name="seed-retry").start()
    Handler.stores = StorePool(Handler.pool, args.ttl)
    Handler.token = os.environ.get("HOTUSAGE_INGEST_TOKEN", "")
    if not Handler.token:
        print("warn: HOTUSAGE_INGEST_TOKEN unset - accepting unauthenticated ingest (dev mode)",
              file=sys.stderr)

    print(f"hotusage server: system database {args.system_database} (accounts + auth)")
    print("hotusage server: per-org usage databases resolved from the orgs table")
    print(f"hotusage server: listening on http://{args.host}:{args.port}/  (Ctrl-C to stop)")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nhotusage server: stopped")
