# hotusage

Company-wide usage analytics for AI coding agents — Claude Code, Codex and
OpenCode. See who is using them, on which projects, with which models, and what
it would cost, across your whole team.

This is the server. It runs at **hotusage.ai**, so most people never install
it — you just need an invite.

## Getting your team on it

Your admin sends you an invite link. Open it, pick a password, then install the
client on each machine:

```bash
curl -fsSL https://raw.githubusercontent.com/hotdata-dev/hotusage-client/main/install.sh | sh
```

Your browser opens, you approve the machine, and it starts reporting. From then
on it syncs by itself. Headless machines run `hotusage signin` instead, which
prints a URL and a code to approve from any browser.

Two ways to see the numbers: open the **dashboard**, or ask your coding agent —
the installer teaches Claude Code and Codex to answer questions like *"what did
we spend on Claude Code last month?"*

## The dashboard

Filter by person, project, tool and date range. The daily chart stacks either by
token type or **by person**, in tokens or dollars, with a table view of the same
numbers.

**The dollar figures are API list-price equivalents, not a bill.** They are
worked out from token counts at published rates. If your team is on a Max, Team
or Enterprise plan you pay a flat per-seat fee and none of this — an
organization can easily show tens of thousands here while paying a few hundred.
Use them to compare people, projects and trends; for actual spend see the spend
report at `claude.ai/admin-settings/usage`, which exports per-user cost as a
daily CSV.

## Running an organization

Everything is on the **Organization** page, linked from the header.

**Inviting people.** Two kinds of link, which you share yourself — nothing is
emailed:

- a *single-use invite* bound to one address, valid 7 days
- a *team link* anyone can use to join with their own email and password

You can restrict a team link to an email domain and cap how many times it is
used; it expires after 30 days by default, 90 at most. **Restrict it to your
domain unless you are sharing it privately** — anyone holding an open link can
join and read your organization's usage.

**Managing members.** Promote or demote admins, and remove people. Removing
someone revokes their logins and their machines; usage they already reported
stays.

**Revoking access.** Outstanding invites and signed-in machines are both listed
with revoke buttons. Each machine shows what it may do and when it was last
active, which is how you spot one that has quietly stopped reporting.

Someone can belong to several organizations; the account menu switches which one
is active — the org their dashboard shows and their machines report into.

## Good to know

- **Usage from an address that isn't a member is rejected.** The person's
  client shows the error; once you invite them, their next sync succeeds.
- **Cursor and Gemini CLI are not covered.** Neither keeps local token counts,
  so there is nothing to read.
- **First load after a quiet spell can take 10–20 seconds.** An idle
  organization's database scales to zero and has to wake up. Normal, not a hang.
- **Each organization's usage is physically separate** — its own database, not a
  filtered view of a shared one.

---

Running your own instance, or working on hotusage itself?
See [`docs/operating.md`](docs/operating.md).
