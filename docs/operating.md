# hotusage server — operating

Everything the [README](../README.md) leaves out: running the server, the
platform-operator powers, the CLI, and the parts of the design worth knowing
before changing anything.

## Architecture in one paragraph

Pure Python standard library — `ThreadingHTTPServer` plus a hand-rolled router;
the only dependencies are the `hotdata` SDK and `duckdb`. There is no local
database. All state lives in [hotdata](https://hotdata.dev): one *system*
database for accounts (orgs, users, sessions, invites, tokens) and one dedicated
database **per organization** for usage, so each org's data is physically
isolated rather than filtered out of a shared table.

## Running it

```bash
uv venv && uv pip install hotdata duckdb
export HOTDATA_API_KEY=<workspace key>             # or ~/.hotdata/hotdata.json
export HOTUSAGE_INGEST_TOKEN=<shared secret>       # unset = dev mode (accepts all)
.venv/bin/python server/server.py --host 0.0.0.0 --port 8377
```

Or use the Dockerfile. Bind `0.0.0.0` only behind a VPN or reverse proxy. First
boot seeds an initial admin user and prints its generated password once.

Signing up is self-serve: `/register` creates the account, `/setup` asks for an
organization name and provisions its database — that person becomes the org's
first member and admin. Anyone invited to an existing org skips `/setup`.
Registration and invite endpoints are rate limited.

`/healthz` answers liveness checks; `/healthz?deep=1` also reports whether
hotdata is reachable, classified rather than echoed, so a deployment can be
diagnosed without shell or log access.

## The read API

Besides the dashboard the server exposes a small read-only API, which is what
the client's `summary`/`chart`/`users` commands call: `GET /api/data`,
`/api/session/<id>`, `/api/orgs`, `/api/status`.

Two allow-lists back that, both on `Handler`: `READ_TOKEN_PATHS` matches
`/api/data`, `/api/orgs` and `/api/status` exactly, and `READ_TOKEN_PREFIXES`
holds the single prefix `/api/session/`, which has to match by prefix because
the session id is in the path.

What matters is that it is an allow-list at all rather than "anything under
`/api/`": every other route either mutates something or hands back a credential
(invite links, machine tokens), and a token minted for reporting must reach
neither. Bearer credentials are never sent by a browser on its own, so these
routes need no CSRF defence beyond staying read-only.

## Tokens and scopes

A per-user token carries a **set** of scopes:

- `ingest` — report usage
- `read` — query the organization through the API

A token with no scope row predates scopes and means `ingest` alone, so every
already-deployed client keeps working untouched. The client asks for both at
once: it runs the background sync and the agent skill on one machine, and two
approvals for one laptop would be ceremony rather than security. A server-side
script that should report but never read (or the reverse) can still be issued
one scope. `listtokens` shows what each holds.

Both failure modes are closed rather than open. A stored scope this build cannot
parse grants **nothing** — falling back to the default would silently promote a
read-only credential into one that can write usage rows. A device asking for an
unknown scope fails the approval outright, rather than minting a token for
different powers than the page just described to the person clicking Approve.

**Per-user tokens identify their owner**: ingest signed in as someone reports as
that account whatever the payload claims. The shared `HOTUSAGE_INGEST_TOKEN`
still works but only *admits* — anyone holding it can report as any registered
colleague — so prefer sign-in.

## Platform operators (system admins)

Creating or deleting an organization provisions or strands a hotdata database,
so it is a platform power, distinct from org admin. System admins get an **All
organizations** card on `/admin`: every org with member counts, a create form
(optionally with an owner email, which mints an invite whose acceptor becomes
that org's admin), and delete for empty orgs.

```bash
.venv/bin/python server/server.py makesysadmin eddie@hotdata.dev
```

## CLI reference

Everything on the Organization page has a CLI equivalent, for scripted setup and
recovery. All commands run against the system database and take effect within
the server's ~60s caches.

```bash
# organizations
server.py listorgs                        # slug, user count, database id
server.py addorg acme --name "Acme Inc"   # provisions the org's database
server.py delorg acme                     # refuses while users remain; keeps the database
server.py delorg acme --delete-database   # also destroys its usage data

# users
server.py listusers --org all             # everyone (or --org <slug>)
server.py adduser jane@acme.com --org acme   # prints initial password
server.py resetpw jane@acme.com              # prints new password
server.py deluser jane@acme.com              # + revokes their logins and machines
server.py makeadmin jane@acme.com            # org admin (unadmin to demote)
server.py makesysadmin jane@acme.com         # platform operator (unsysadmin to revoke)

# invites and signed-in machines
server.py listinvites                     # both kinds, with uses + days left
server.py revokeinvite <token>
server.py listtokens                      # who is signed in, from where, scope, last used
server.py revoketoken <token>             # or: --user <email> for all of theirs
```

There is no self-serve password reset — `resetpw` is the recovery path. An org
that loses its last admin is recovered with `makeadmin`, never by whoever
happens to join next.

## Pricing happens on the client

The server never prices anything; it stores the `cost_*` columns the ingest
payload already carries. To reprice a provider, edit `rates_claude` /
`rates_claude_fast` / `rates_openai` in **hotusage-client's** `src/core.rs`.

The copy in this repo's `core.py` is a leftover from the original Python
collector, is not what the dashboard shows, and has already drifted (it has no
fast-mode branch). It is labelled as unused at the top of its pricing block.

For **actual** spend rather than list-price equivalents: the spend report at
`claude.ai/admin-settings/usage` exports per-user, per-model cost as a daily
CSV, and Claude Code's OpenTelemetry export emits `claude_code.cost.usage` in
real dollars per user. Neither is something this server can derive.

## Backups

hotdata forks are cheap deep copies:

```bash
hotdata databases fork <dbid> --name <label>
```

Snapshot an org database — or the system database — before a risky change.

## Tests

```bash
for t in server/tests/test_*.py; do python3 "$t"; done
```

Standard library only, no hotdata account needed: `AuthStore` runs against a
stateful fake system database, and `test_http_read_token.py` drives the real
handler over a loopback socket to assert a read token cannot ingest and an
ingest token cannot read. CI runs all of them before the image is built.
