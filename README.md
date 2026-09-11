# hotusage

Company-wide usage analytics for AI coding agents (Claude Code, Codex,
OpenCode). Per-user collectors feed a central server; everything is stored in
hotdata — a system database for accounts, plus one dedicated database per
organization:

```
[collector]  menu bar app / daemon, one per user (separate repo: hotusage-collector)
     |  POST /ingest  (changed sessions only, Bearer token)
     v
[server] --- accounts/auth --> hotdata `hotusage-system` db (orgs, users, sessions)
     |
     +------ usage rows ------> one hotdata db PER ORGANIZATION (routed by the
     |                          reporting user's org; provisioned on `addorg`)
     +------ dashboard: each viewer reads only their org's database
```

- **server/** — receives ingests and upserts them into the reporting user's
  org database (sdk-python parquet loads with key-based upsert; no local
  database anywhere). Serves the login-gated admin dashboard at `/`, which
  reads the viewer's org database with user / tool / project / date filters.
- **collector** — native Rust menu bar/daemon agent, one per user, in its own
  repo (`hotusage-collector`). A Python reference implementation lives here in
  `collector/`.

Not collected: **Cursor** (stores no token counts locally; usage is
server-side at cursor.com) and **Gemini CLI** (keeps no usage history).

## Server setup

```bash
uv venv && uv pip install hotdata duckdb           # once
export HOTUSAGE_INGEST_TOKEN=<shared secret>       # unset = dev mode (accepts all)
export HOTDATA_API_KEY=<workspace key>             # or ~/.hotdata/hotdata.json
.venv/bin/python server/server.py --host 0.0.0.0 --port 8377
```

Flags: `--system-database <dbid>` (accounts db, default in `core.py`), `--ttl`
(dashboard read cache seconds, default 60). Org usage databases are resolved
at runtime from the system db's `orgs.database_id`. Run `--host 0.0.0.0` only
behind a VPN (e.g. tailscale) or reverse proxy.

First boot seeds org `hotdata` with user `eddie@hotdata.dev` and prints a
generated initial password once.

## Self-serve registration and invites

Anyone can create an organization at `/register` (linked from the login page):
org name + email + password provisions the org's dedicated database and signs
them in. Slugs are derived from the org name; a taken slug is refused —
joining an existing org goes through invites, never through guessing its slug.

Members grow an org with **Invite teammate** in the dashboard header, which
mints two kinds of link. Nothing is emailed yet — you share the link yourself.
Unauthenticated register/invite endpoints are rate limited (5/hour per IP).

- **Single-use invite** — enter one teammate's email; the link is bound to
  that address and valid 7 days. It dies the moment it is used.
- **Team link** — reusable: anyone who opens it picks their own email and
  password and joins your org. Optionally restrict it to an email domain
  (`acme.com`) and/or a maximum number of uses (blank/0 = unlimited);
  default expiry is 30 days, 90 max. Restrict it to your domain unless you
  are sharing it privately — anyone holding an unrestricted link can join
  and read the org's usage.

The use counter on a team link is best-effort: simultaneous joins can push it
a use or two past the cap. The domain restriction is the real control.

Outstanding links, and revoking one:

```bash
.venv/bin/python server/server.py listinvites          # both kinds, with uses + days left
.venv/bin/python server/server.py revokeinvite <token> # kills it immediately
```

## Managing organizations

Every user belongs to exactly one organization. Each organization owns a
dedicated hotdata database — isolation between orgs is physical, not a query
filter. All admin commands run against the system database and take effect
immediately (the server picks changes up within its ~60s caches).

```bash
.venv/bin/python server/server.py listorgs                       # slug, user count, database id
.venv/bin/python server/server.py addorg acme --name "Acme Inc"  # provisions the org's database
.venv/bin/python server/server.py delorg acme                    # refuses while users remain
.venv/bin/python server/server.py delorg acme --delete-database  # also destroys its usage data
```

`addorg` creates the org's hotdata database (catalog `hotusage`), declares the
usage tables with their upsert keys, and records the database id on the org
row. `delorg` keeps the database unless you pass `--delete-database`.

## Collector sign-in

Teammates do not need the shared ingest token any more. In the collector's menu
bar, **Sign In...** opens the browser, they log in (or accept an invite first),
and confirm that the code on the approval page matches the one in the menu. The
server then mints a collector token bound to their account and the collector
stores it. Headless machines run `hotusage-collector signin`, which prints the
same URL and code.

A collector token *identifies* its owner: ingest authenticated with one reports
as that account no matter what address the payload claims. The shared
`HOTUSAGE_INGEST_TOKEN` still works for existing installs, but it only admits —
anyone holding it can report as any registered colleague — so prefer sign-in.

```bash
.venv/bin/python server/server.py listtokens            # who is signed in, from where
.venv/bin/python server/server.py revoketoken <token>   # or: --user <email> for all of theirs
```

## Managing users

```bash
.venv/bin/python server/server.py listusers --org all            # everyone (or --org <slug>)
.venv/bin/python server/server.py adduser jane@acme.com --org acme   # prints initial password
.venv/bin/python server/server.py resetpw jane@acme.com              # prints new password
.venv/bin/python server/server.py deluser jane@acme.com              # + revokes their logins
```

Onboarding a teammate (the **Invite teammate** button in the dashboard is
the usual path; the CLI equivalent is):

1. `adduser jane@acme.com --org acme` — send them the printed initial
   password (they can ask you to `resetpw` any time; there is no self-serve
   reset).
2. They install the collector (see the `hotusage-collector` repo:
   `hotusage-collector install`) and set `user_email: jane@acme.com` plus the
   server URL and ingest token in `~/.hotusage/collector.json`.
3. Their next sync flows into the org's database and they can log in to the
   dashboard.

**Ingest is rejected (403) for emails that are not registered users** — there
is no org database to route them to. The collector surfaces the error in its
menu status; add the user and the next sync succeeds. Deleting a user stops
future ingest and revokes logins, but their already-ingested rows stay in the
org database.

## System administration

- **Stores.** Everything lives in hotdata (prod Default Workspace):
  - `hotusage-system` — `orgs` (slug, name, `database_id`), `users` (scrypt
    password hashes), `auth_sessions` (login tokens). Private operational
    data; keep out of analytics and don't widen query access to this database.
  - one usage database per org (catalog `hotusage`): `sessions`, `requests`,
    `daily_usage`, keyed by user_email + session_id (+ seq / + day). Writes
    are idempotent key-based upserts, so collectors can safely re-send.
  - Each database documents itself:
    `hotdata databases context show DATAMODEL --database <id>`.
- **Secrets.** `HOTUSAGE_INGEST_TOKEN` is the shared collector credential —
  rotate by restarting the server with a new value and updating collector
  configs (the old token stops working immediately). The hotdata workspace API
  key comes from `HOTDATA_API_KEY` or `~/.hotdata/hotdata.json`; it is the
  only credential the server needs.
- **Sessions.** Dashboard logins last 30 days; `deluser` revokes a user's
  sessions, and expired sessions are purged opportunistically on login.
  Rotating a password does not revoke existing sessions — delete the user's
  rows from `auth_sessions` if that matters.
- **Health.** `/api/status` (authenticated) returns the viewer's org database
  id. Server logs (stdout) show every ingest with its routed org and any
  hotdata errors.
- **Cold starts.** Each org database has its own query worker that scales to
  zero; the first dashboard load or ingest for an idle org takes ~10-20s while
  it wakes. Normal, not a hang.
- **Backups / experiments.** hotdata database forks are cheap deep copies:
  `hotdata databases fork <dbid> --name <label>` snapshots an org database (or
  the system database) before risky changes.
- **Costs shown are estimates** at provider API list prices (rate table in
  `core.py` — update it when providers reprice; cache read 0.1x input for
  Anthropic, model-specific cached rates for OpenAI). Subscription plans don't
  bill per token. Unknown models price at $0.

## Collector

See the `hotusage-collector` repo (Rust; macOS menu bar, Windows tray, Linux
daemon; `install` registers it as a login service). Config lives in
`~/.hotusage/collector.json`; state in `collector-state.json` tracks
per-session fingerprints so only changed sessions are re-sent. The Python
reference implementation in `collector/collector.py` shares both files and the
wire format (verified field-for-field against the Rust port).

## Notes

- Claude usage is deduped by API `message.id`; Codex `token_count` events are
  cumulative so sums use deltas (`input_tokens` includes cached — split into
  uncached input + cache read); OpenCode comes from its sqlite message log.
- "Context" per request = what the model saw (input + cache read + cache write
  for Claude/OpenCode; last `input_tokens` for Codex).
- Timestamps: parquet is written with DuckDB pinned to `SET timezone='UTC'` —
  without it, naive-timestamp casts shift by the local UTC offset on load.
