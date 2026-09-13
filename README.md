# hotusage

Company-wide usage analytics for AI coding agents (Claude Code, Codex,
OpenCode). A small collector runs on each person's machine and reports their
local agent history to a central server; the dashboard shows sessions, tokens,
and estimated cost for your whole organization — by user, tool, project, and
date.

Not collected: **Cursor** and **Gemini CLI** — neither stores token counts
locally, so there is nothing to report.

All data lives in [hotdata](https://hotdata.dev): one system database for
accounts, plus one dedicated database per organization, so each org's usage is
physically isolated.

## For team members

Your admin sends you an invite link (or a reusable team link). Open it, pick a
password, and you're in. Then install the collector:

```bash
curl -fsSL https://raw.githubusercontent.com/hotdata-dev/hotusage-collector/main/install.sh | sh
```

That one line installs the collector (macOS menu bar, Windows tray, or Linux
daemon), registers it as a login service, and walks you through sign-in: your
browser opens, you confirm the code shown in the menu, and the collector starts
reporting as you. Headless machines run `hotusage-collector signin` instead.

From then on it syncs automatically — only changed sessions are re-sent. Open
the dashboard at your server's URL to see your team's usage. **Sign Out** in
the collector menu (or `hotusage-collector signout`) stops that machine from
reporting; other machines stay signed in.

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
- **Revoke access.** Outstanding invite links and signed-in collectors are
  listed with revoke buttons. The collectors list shows each machine and when
  it last reported — handy for spotting one that stopped.
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

# invites and collectors
server.py listinvites                     # both kinds, with uses + days left
server.py revokeinvite <token>
server.py listtokens                      # who is signed in, from where, last used
server.py revoketoken <token>             # or: --user <email> for all of theirs
```

There is no self-serve password reset — `resetpw` is the recovery path. An org
that loses its last admin is recovered with `makeadmin`, never by whoever joins
next.

## Good to know

- **Costs are estimates** at provider API list prices (Anthropic cache reads
  at 0.1× input; OpenAI cached input discounted per model). Subscription plans
  don't bill per token; unknown models price at $0. The rate table lives in
  `core.py` — update it when providers reprice.
- **Usage from unregistered emails is rejected** (403) — the collector shows
  the error in its menu; once the person is invited, the next sync succeeds.
- **Collector tokens identify their owner**: ingest signed in as someone
  reports as that account, whatever the payload claims. The shared
  `HOTUSAGE_INGEST_TOKEN` still works but only admits — anyone holding it can
  report as any registered colleague — so prefer sign-in.
- **Backups**: hotdata forks are cheap deep copies —
  `hotdata databases fork <dbid> --name <label>` snapshots an org database (or
  the system database) before risky changes.
- **Collector internals** live in the
  [`hotusage-collector`](https://github.com/hotdata-dev/hotusage-collector)
  repo (Rust). Its config is `~/.hotusage/collector.json` (mode 0600 — it
  holds a token).
