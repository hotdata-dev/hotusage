# hotusage-server

The hosted half of hotusage: company-wide usage analytics for AI coding agents
(Claude Code, Codex, OpenCode). The [client](../hotusage-client) runs on each
person's machine and reports their local agent history here; the dashboard
shows sessions, tokens, and estimated cost for your whole organization — by
user, tool, project, and date. The same client also queries this server's API
so a coding agent can answer questions about the numbers.

Most people never install this — it runs at hotusage.ai. Everything below is
for running or operating it.

Not collected: **Cursor** and **Gemini CLI** — neither stores token counts
locally, so there is nothing to report.

All data lives in [hotdata](https://hotdata.dev): one system database for
accounts, plus one dedicated database per organization, so each org's usage is
physically isolated.

## For team members

Your admin sends you an invite link (or a reusable team link). Open it, pick a
password, and you're in. Then install the client:

```bash
curl -fsSL https://raw.githubusercontent.com/hotdata-dev/hotusage-client/main/install.sh | sh
```

That one line installs `hotusage` (macOS menu bar, Windows tray, or Linux
daemon), registers it as a login service, installs the agent skill for Claude
Code and Codex, and walks you through sign-in: your browser opens, you confirm
the code shown in the menu, and the machine starts reporting as you. Headless
machines run `hotusage signin` instead.

From then on it syncs automatically — only changed sessions are re-sent. Open
the dashboard at your server's URL to see your team's usage, or ask your coding
agent ("what did we spend on Claude Code last month?"). **Sign Out** in the
menu (or `hotusage signout`) stops that machine from reporting *and* from
reading; other machines stay signed in.

## The read API

Besides the dashboard, the server exposes a small read-only API that the client
uses to answer questions about usage: `GET /api/data`, `/api/session/<id>`,
`/api/orgs` and `/api/status`, authenticated with a `read`-scoped bearer token
(`Handler.READ_TOKEN_PATHS` is the allow-list). Tokens are minted by the same
device flow collectors use; see **Tokens are scoped** below.

## For organization admins

Everything is on the **Organization** page (`/admin`, linked from the header):

- **Invite people.** Two kinds of link, shared by you (nothing is emailed):
  - *Single-use invite* — bound to one email address, valid 7 days, dies when
    used.
  - *Team link* — reusable: anyone who opens it joins your org with their own
    email and password. You can restrict it to an email domain (`acme.com`)
    and/or cap its uses; default expiry 30 days, 90 max. **Restrict it to your
    domain unless sharing privately** — anyone holding an open link can join
    and read your org's usage.
- **Manage members.** See everyone, promote or demote admins (the last admin
  can't be demoted), and remove people. Removing someone revokes their logins
  and collectors; their already-reported usage stays.
- **Revoke access.** Outstanding invite links and signed-in machines are listed
  with revoke buttons. The machines list shows each one, what it may do
  (*Reports + reads* for a current client, *Reports usage* for one signed in
  before 0.4.0), and when it was last active — handy for spotting a machine
  that stopped reporting.
- **Rename the org.**

A person can belong to several organizations; the account menu switches which
one is *active* (the org their dashboard shows and their collector reports
into). Inviting an already-registered address just adds a membership.

## Running the server

```bash
uv venv && uv pip install hotdata duckdb
export HOTDATA_API_KEY=<workspace key>             # or ~/.hotdata/hotdata.json
export HOTUSAGE_INGEST_TOKEN=<shared secret>       # unset = dev mode (accepts all)
.venv/bin/python server/server.py --host 0.0.0.0 --port 8377
```

Or use the Dockerfile. Run `--host 0.0.0.0` only behind a VPN (e.g. tailscale)
or reverse proxy. First boot seeds an initial admin user and prints its
generated password once.

Signing up is self-serve from the login page: `/register` creates the account,
then `/setup` asks for an organization name and provisions its database — you
become that org's first member and admin. Anyone invited to an existing org
skips `/setup` entirely. Registration and invite endpoints are rate limited.

`/healthz` answers liveness checks; `/healthz?deep=1` also reports whether the
hotdata backend is reachable.

**Note on cold starts:** an idle org's database worker scales to zero, so the
first dashboard load or ingest after a quiet spell can take ~10–20 seconds.
Normal, not a hang.

## Platform operators (system admins)

Creating or deleting organizations provisions or strands hotdata databases, so
it's a platform power, distinct from org admin. System admins get an **All
organizations** card on `/admin`: every org with member counts, a create form
(optionally with an owner email, which mints an invite whose acceptor becomes
the org's admin), and delete for empty orgs. Grant the role from the CLI:

```bash
.venv/bin/python server/server.py makesysadmin eddie@hotdata.dev
```

## CLI reference

Everything on the Organization page has a CLI equivalent, for scripted setup
and recovery. All commands run against the system database and take effect
within the server's ~60s caches.

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
server.py deluser jane@acme.com              # + revokes their logins and collectors
server.py makeadmin jane@acme.com            # org admin (unadmin to demote)
server.py makesysadmin jane@acme.com         # platform operator (unsysadmin to revoke)

# invites and signed-in machines
server.py listinvites                     # both kinds, with uses + days left
server.py revokeinvite <token>
server.py listtokens                      # who is signed in, from where, scope, last used
server.py revoketoken <token>             # or: --user <email> for all of theirs
```

There is no self-serve password reset — `resetpw` is the recovery path. An org
that loses its last admin is recovered with `makeadmin`, never by whoever joins
next.

## Good to know

- **Dollar figures are API list-price equivalents, not a bill.** They are
  derived from token counts at provider list prices (Anthropic cache reads at
  0.1× input, 5-minute cache writes at 1.25× and 1-hour at 2×, and fast mode at
  its premium rate; OpenAI cached input discounted per model; unknown models
  $0).
  **Subscription plans — Max, Team, Enterprise — charge a flat per-seat fee and
  bill none of this**, so an org can show tens of thousands here while paying a
  few hundred. Use these figures to compare people, projects and trends; for
  actual spend use the report at `claude.ai/admin-settings/usage`, or Claude
  Code's OpenTelemetry export (`claude_code.cost.usage`) for real per-user cost.
  **Pricing happens entirely on the client**: the server never prices anything,
  it stores the `cost_*` columns the ingest payload already carries. To reprice
  a provider, edit `rates_claude` / `rates_claude_fast` / `rates_openai` in
  hotusage-client's `src/core.rs`. The copy in this repo's `core.py` is a
  leftover from the original Python collector and is not what the dashboard
  shows.
- **Usage from unregistered emails is rejected** (403) — the collector shows
  the error in its menu; once the person is invited, the next sync succeeds.
- **Per-user tokens identify their owner**: ingest signed in as someone reports
  as that account, whatever the payload claims. The shared
  `HOTUSAGE_INGEST_TOKEN` still works but only admits — anyone holding it can
  report as any registered colleague — so prefer sign-in.
- **Tokens are scoped.** A token carries a set: `ingest` reports usage, `read`
  queries the org through the API. A token with no scope row predates scopes
  and means `ingest` alone, so every already-deployed collector keeps working
  untouched. The client asks for both at once — it runs the background sync and
  the agent skill on one machine, and two approvals for one laptop would be
  ceremony rather than security. A server-side script that should report but
  never read (or vice versa) can still be given one scope; `listtokens` shows
  what each holds.
- **Backups**: hotdata forks are cheap deep copies —
  `hotdata databases fork <dbid> --name <label>` snapshots an org database (or
  the system database) before risky changes.
- **Client internals** live in the
  [`hotusage-client`](https://github.com/hotdata-dev/hotusage-client) repo
  (Rust). Its config is `~/.hotusage/collector.json` (mode 0600 — it holds a
  token); the filename predates the rename and is kept so deployed machines
  keep their sign-in.
