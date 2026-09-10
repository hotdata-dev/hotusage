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
import time
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

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
}

USAGE_TABLES = ("sessions", "requests", "daily_usage")
SYSTEM_TABLES = ("orgs", "users", "auth_sessions")


# ---------------------------------------------------------------------------
# HotdataClient: SQL reads + key-based managed loads against one database.
# ---------------------------------------------------------------------------
class HotdataClient:
    def __init__(self, database_id):
        self.db = database_id
        self._client = None
        self.write_lock = threading.Lock()

    def client(self):
        if self._client is None:
            import hotdata
            key = core.hotdata_api_key()
            if not key:
                raise RuntimeError("no hotdata API key (HOTDATA_API_KEY or ~/.hotdata/hotdata.json)")
            self._client = hotdata.ApiClient(hotdata.Configuration(
                host=os.environ.get("HOTDATA_API_HOST", "https://api.hotdata.dev"),
                api_key=key,
                workspace_id=os.environ.get("HOTDATA_WORKSPACE", core.WORKSPACE_ID),
            ))
        return self._client

    def sql(self, query):
        import hotdata
        client = self.client()
        resp = hotdata.QueryApi(client).query(
            hotdata.QueryRequest(sql=query), x_database_id=self.db)
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
        database for a new org."""
        if not re.match(r"^[a-z0-9][a-z0-9-]{0,60}$", slug):
            raise ValueError("org slug must be lowercase alphanumeric/hyphens")
        org = self.get_org(slug, ttl=0)
        if org:
            return org["database_id"]
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
        self.sysdb.load("users", [{"email": email, "password_hash": hash_password(password),
                                   "org_slug": org_slug,
                                   "created_at": datetime.now(timezone.utc).isoformat()}],
                        "upsert")
        with self.lock:
            self.route_cache.pop(email, None)

    def set_password(self, email, password):
        u = self.get_user(email)
        if not u:
            raise ValueError(f"no such user: {email}")
        self.sysdb.load("users", [{"email": u["email"], "password_hash": hash_password(password),
                                   "org_slug": u["org_slug"],
                                   "created_at": datetime.now(timezone.utc).isoformat()}],
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

    def seed(self):
        """First boot: system tables; org hotdata (with its dedicated database)
        and eddie@hotdata.dev as the first user."""
        self.sysdb.ensure_schema_and_tables(SYSTEM_TABLES)
        if self.sysdb.rows(f"SELECT email FROM {SYS}.public.users LIMIT 1"):
            return None
        password = secrets.token_urlsafe(12)
        self.ensure_org("hotdata", "hotdata")
        self.create_user("eddie@hotdata.dev", password, "hotdata")
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
        self.lock = threading.Lock()

    def invalidate(self):
        with self.lock:
            self.cache.clear()

    def _cached(self, key, fn, fresh=False):
        with self.lock:
            hit = self.cache.get(key)
            if hit and not fresh and time.time() - hit[0] < self.ttl:
                return hit[1]
        val = fn()
        with self.lock:
            self.cache[key] = (time.time(), val)
        return val

    def data(self, fresh=False):
        return self._cached("data", self._fetch_data, fresh)

    def _fetch_data(self):
        sess_rows = self.hd.rows(f"SELECT * FROM {core.CATALOG}.public.sessions")
        daily_rows = self.hd.rows(f"SELECT * FROM {core.CATALOG}.public.daily_usage")
        sessions = [{
            "id": r["session_id"],
            "user": r.get("user_email"),
            "host": r.get("hostname"),
            "provider": r["provider"],
            "project": r["project"],
            "cwd": r["cwd"],
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
        detail = self._cached("detail:" + session_id, fetch)
        if not detail:
            return None
        return {"id": session_id, "detail": detail}


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

    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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

    def _read_form(self):
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > 64 * 1024:
            return {}
        body = self.rfile.read(length).decode(errors="replace")
        return {k: v[0] for k, v in parse_qs(body).items()}

    def _handle_login(self):
        form = self._read_form()
        token = self.auth.login(form.get("email", ""), form.get("password", ""))
        if not token:
            time.sleep(0.3)  # soften brute force
            self._redirect("/login?err=1")
            return
        cookie = (f"hotusage_session={token}; HttpOnly; SameSite=Lax; Path=/; "
                  f"Max-Age={SESSION_TTL}")
        self._redirect("/", extra=[("Set-Cookie", cookie)])

    def do_POST(self):
        path = urlparse(self.path).path
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
            if self.token:
                auth = self.headers.get("Authorization", "")
                if auth != "Bearer " + self.token:
                    self._json({"error": "unauthorized"}, 401)
                    return
            length = int(self.headers.get("Content-Length", 0))
            if length <= 0 or length > self.max_body:
                self._json({"error": "bad content length"}, 400)
                return
            payload = json.loads(self.rfile.read(length))
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

    def _hotdata_health(self):
        """Coarse backend status for /healthz?deep=1 - never leaks details."""
        if not core.hotdata_api_key():
            return "no_api_key"
        try:
            self.auth.sysdb.sql("SELECT 1 AS ok")
            return "ok"
        except Exception as e:
            msg = str(e).lower()
            if "401" in msg or "unauthorized" in msg or "forbidden" in msg or "403" in msg:
                return "auth_rejected"
            if "not found" in msg or "has no data" in msg:
                return "ok"  # reachable; the probe table just isn't a table
            if "timed out" in msg or "timeout" in msg or "connection" in msg:
                return "unreachable"
            return "error"

    def _org_payload(self, viewer, fresh=False):
        """The dashboard payload: the viewer's org database, whole."""
        if not viewer.get("database_id"):
            return {"generatedAt": datetime.now(timezone.utc).isoformat(),
                    "source": "no org database",
                    "viewer": {"email": viewer["email"], "org": viewer["org_name"]},
                    "sessions": [], "daily": []}
        data = self.stores.get(viewer["database_id"]).data(fresh=fresh)
        return {**data,
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
                self._static("login.html")
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

            if path == "/" or path == "/index.html":
                self._static("index.html")
            elif path == "/api/data":
                self._json(self._org_payload(viewer, fresh=fresh))
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

    def _static(self, name):
        fp = os.path.normpath(os.path.join(STATIC_DIR, name))
        if not fp.startswith(STATIC_DIR) or not os.path.isfile(fp):
            self._send(404, b"not found", "text/plain")
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css",
            ".js": "application/javascript",
        }.get(os.path.splitext(fp)[1], "application/octet-stream")
        with open(fp, "rb") as f:
            self._send(200, f.read(), ctype)

    def log_message(self, fmt, *args):
        pass


ADMIN_VERBS = ("adduser", "addorg", "resetpw", "deluser", "delorg",
               "listusers", "listorgs")

def user_admin_cli(argv):
    """Admin subcommands against the system database; returns True if handled."""
    if not argv or argv[0] not in ADMIN_VERBS:
        return False
    verb = argv[0]
    ap = argparse.ArgumentParser(prog=f"server.py {verb}")
    if verb not in ("listusers", "listorgs"):
        ap.add_argument("target", help="email (user verbs) or org slug (org verbs)")
    ap.add_argument("--org", default="hotdata",
                    help="org slug for adduser / filter for listusers (default hotdata)")
    ap.add_argument("--name", default=None, help="display name for addorg")
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
        if not auth.get_user(email):
            sys.exit(f"no such user: {email}")
        toks = auth.sysdb.rows(f"SELECT token FROM {SYS}.public.auth_sessions "
                               f"WHERE user_email = {sql_str(email)}")
        if toks:
            auth.sysdb.load("auth_sessions", toks, "delete")
        auth.sysdb.load("users", [{"email": email}], "delete")
        print(f"deleted {email} (their already-ingested usage stays in the org database)")

    elif verb == "delorg":
        org = auth.get_org(a.target, ttl=0)
        if not org:
            sys.exit(f"no such org: {a.target}")
        members = auth.sysdb.rows(f"SELECT email FROM {SYS}.public.users "
                                  f"WHERE org_slug = {sql_str(a.target)}")
        if members:
            sys.exit(f"org '{a.target}' still has {len(members)} user(s): "
                     + ", ".join(m["email"] for m in members)
                     + "\ndelete them first (server.py deluser <email>)")
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
