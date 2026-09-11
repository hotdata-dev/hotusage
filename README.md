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
  repo (`hotusage-collector`).

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

Admins grow an org from the **Organization** page, which mints two kinds of
link. Nothing is emailed yet — you share the link yourself. Unauthenticated
register/invite endpoints are rate limited (5/hour per IP).

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

## Organization page

`/admin` (linked from the header) is the self-serve version of the admin CLI:
rename the org, see every member, invite people, and revoke invites or
signed-in collectors. Whoever creates an organization administers it; admins
can promote or demote anyone, and the last admin cannot be demoted or removed.

Ordinary members see the roster but no invite tokens and no management
controls — the server enforces this, not the page. From the CLI:

```bash
.venv/bin/python server/server.py makeadmin jane@acme.com
.venv/bin/python server/server.py unadmin jane@acme.com
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

**Sign Out** in the collector's menu (or `hotusage-collector signout`) revokes
that machine's token and clears it locally; other machines stay signed in.

```bash
.venv/bin/python server/server.py listtokens            # who is signed in, from where, last used
.venv/bin/python server/server.py revoketoken <token>   # or: --user <email> for all of theirs
```

`last used` is refreshed at most hourly per token — enough to spot a machine
that has stopped reporting, without a write on every ingest.

## Managing users

```bash
.venv/bin/python server/server.py listusers --org all            # everyone (or --org <slug>)
.venv/bin/python server/server.py adduser jane@acme.com --org acme   # prints initial password
.venv/bin/python server/server.py resetpw jane@acme.com              # prints new password
.venv/bin/python server/server.py deluser jane@acme.com              # + revokes their logins
```

Onboarding a teammate is an invite from the Organization page, not these
commands: they open the link, choose a password, run the collector installer,
and it signs them in. The CLI path exists for scripted setup and recovery —
`adduser` prints an initial password, and there is no self-serve reset.

**Ingest is rejected (403) for emails that are not registered users** — there
is no org database to route them to. The collector surfaces the error in its
menu status; add the user and the next sync succeeds. Deleting a user stops
future ingest and revokes logins, but their already-ingested rows stay in the
org database.

## System administration

- **Stores.** Everything lives in one hotdata workspace (`core.py`):
  - `hotusage-system` — `orgs` (slug, name, `database_id`), `users` (scrypt
    password hashes), `auth_sessions` (login tokens). Private operational
    data; keep out of analytics and don't widen query access to this database.
  - one usage database per org (catalog `hotusage`): `sessions`, `requests`,
    `daily_usage`, keyed by user_email + session_id (+ seq / + day). Writes
    are idempotent key-based upserts, so collectors can safely re-send.
  - Each database documents itself:
    `hotdata databases context show DATAMODEL --database <id>`.
- **Secrets.** Collectors should sign in (per-user tokens, revocable from the
  Organization page). `HOTUSAGE_INGEST_TOKEN` remains as a shared fallback —
  it only admits, so anyone holding it can report as any registered
  colleague; rotate by restarting the server with a new value. The hotdata
  workspace API key comes from `HOTUSAGE_HOTDATA_API` / `HOTDATA_API_KEY` or
  `~/.hotdata/hotdata.json`.
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
daemon). One line installs it, registers a login service, and signs the
person in:

```bash
curl -fsSL https://raw.githubusercontent.com/hotdata-dev/hotusage-collector/main/install.sh | sh
```

Config lives in `~/.hotusage/collector.json` (mode 0600 — it holds a token);
`collector-state.json` tracks per-session fingerprints so only changed
sessions are re-sent.

## Notes

- Claude usage is deduped by API `message.id`; Codex `token_count` events are
  cumulative so sums use deltas (`input_tokens` includes cached — split into
  uncached input + cache read); OpenCode comes from its sqlite message log.
- "Context" per request = what the model saw (input + cache read + cache write
  for Claude/OpenCode; last `input_tokens` for Codex).
- Timestamps: parquet is written with DuckDB pinned to `SET timezone='UTC'` —
  without it, naive-timestamp casts shift by the local UTC offset on load.
