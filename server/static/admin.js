'use strict';

// ---------------------------------------------------------------------------
// Organization admin: members, invites, signed-in collectors.
// Everything here is org-scoped server-side; this page only draws what
// /api/admin/state returns for the logged-in viewer.
// ---------------------------------------------------------------------------
const $ = (sel) => document.querySelector(sel);
// Table headers and placeholder rows are in admin.html so the page has its
// layout before /api/admin/state answers; every render only swaps the body.
const tbody = (sel) => document.querySelector(sel + ' tbody');

function el(tag, attrs, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  for (const c of children) if (c != null) n.append(c);
  return n;
}

let state = null;

// Placeholder rows are dropped the moment the answer lands or fails -- never
// left animating under an error banner. Removing only the placeholders (rather
// than emptying the bodies) keeps rendered data intact when a later action
// fails.
function clearPlaceholders() {
  for (const r of document.querySelectorAll('.admintable tr.placeholder')) r.remove();
}

// An error belongs beside the control that raised it: the banner at the top of
// the page is off-screen by the time someone is acting on the third card.
// '#err' remains the fallback, and is where a failed page load reports.
const ERROR_BOXES = ['#err', '#inviteErr', '#orgErr', '#dbErr'];

function clearErrors() {
  for (const sel of ERROR_BOXES) {
    const box = $(sel);
    if (box) { box.textContent = ''; box.hidden = true; }
  }
}

function showError(msg, boxSel) {
  const box = (boxSel && $(boxSel)) || $('#err');
  box.textContent = msg;
  box.hidden = !msg;
  if (msg) clearPlaceholders();
}

async function api(path, body) {
  const r = await fetch(path, {
    method: body ? 'POST' : 'GET',
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (r.status === 401) { location.href = '/login'; throw new Error('signed out'); }
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

const day = (v) => (v ? String(v).slice(0, 10) : '-');
const stampText = (v) => String(v).slice(0, 16).replace('T', ' ');

function row(...cells) {
  return el('tr', {}, ...cells.map((c) =>
    el('td', {}, typeof c === 'string' ? document.createTextNode(c) : c)));
}

function action(label, danger, onclick) {
  return el('button',
    { class: danger ? 'linkbtn danger' : 'linkbtn', type: 'button', text: label, onclick });
}

async function act(fn, boxSel) {
  clearErrors();
  try { await fn(); await load(); } catch (e) { showError(e.message, boxSel); }
}

// An empty table is a header row over nothing. Hide the table and show one
// line that says what would fill it -- the same shape the dashboard uses.
function setEmpty(tableSel, emptySel, isEmpty) {
  $(tableSel).hidden = isEmpty;
  $(emptySel).hidden = !isEmpty;
}

// ---------------------------------------------------------------------------
// State you can read without reading: role chips and collector liveness dots.
// ---------------------------------------------------------------------------
const DAY_MS = 86400000;

const roleChip = (isAdmin) => el('span', {
  class: isAdmin ? 'chip admin' : 'chip', text: isAdmin ? 'Admin' : 'Member',
});

function stampMs(v) {
  if (!v) return null;
  // the store hands these back as ISO strings; tolerate a space separator
  const t = Date.parse(String(v).replace(' ', 'T'));
  return Number.isNaN(t) ? null : t;
}

function ago(ms) {
  const s = Math.max(0, Date.now() - ms) / 1000;
  if (s < 3600) return Math.max(1, Math.round(s / 60)) + 'm ago';
  if (s < 86400) return Math.round(s / 3600) + 'h ago';
  return Math.round(s / 86400) + 'd ago';
}

// Green while it is still reporting, amber once it has gone quiet for a day,
// hollow if it never reported at all. The server refreshes this stamp at most
// hourly per token, so the exact value rides in the title rather than the cell.
function syncCell(v) {
  const ms = stampMs(v);
  const cls = ms === null ? 'never' : (Date.now() - ms < DAY_MS ? 'ok' : 'stale');
  const title = ms === null
    ? 'this collector has not reported yet'
    : `last reported ${stampText(v)} UTC (this stamp refreshes at most hourly)`;
  return el('span', { class: 'sync', title },
    el('span', { class: 'dot ' + cls }),
    el('span', { text: ms === null ? 'never' : ago(ms) }));
}

// ---------------------------------------------------------------------------
// Renders
// ---------------------------------------------------------------------------
function renderMembers() {
  const t = tbody('#members');
  t.replaceChildren();
  const admin = state.viewer.isAdmin;
  for (const m of state.members) {
    const self = m.email === state.viewer.email;
    const controls = el('div', { class: 'rowactions' });
    if (admin) {
      const b = action(m.is_admin ? 'Make member' : 'Make admin', false, () =>
        act(() => api('/api/admin/set-admin', { email: m.email, admin: !m.is_admin })));
      controls.append(b);
      if (!self) {
        const r = action('Remove', true, () => {
          if (!confirm(`Remove ${m.email} from this organization? If this is their only organization, their account and collectors are removed too.`)) return;
          act(() => api('/api/admin/remove-user', { email: m.email }));
        });
        controls.append(r);
      }
    }
    t.append(row(
      m.email + (self ? ' (you)' : ''),
      roleChip(m.is_admin),
      day(m.created_at),
      controls));
  }
  $('#memberCount').textContent =
    `${state.members.length} member${state.members.length === 1 ? '' : 's'}`;
}

function renderInvites() {
  const t = tbody('#invites');
  t.replaceChildren();
  const invites = state.invites || [];
  setEmpty('#invites', '#invitesEmpty', !invites.length);
  for (const i of invites) {
    const who = i.kind === 'single' ? i.email : (i.domain ? '@' + i.domain : 'any address');
    const uses = i.kind === 'single' ? 'single use'
      : `${i.uses}/${i.max_uses ? i.max_uses : 'unlimited'}`;
    const days = Math.max(0, Math.round((Number(i.expires_at) * 1000 - Date.now()) / 86400000));
    const revoke = action('Revoke', true, () => act(() =>
      api('/api/admin/revoke-invite', { token: i.token })));
    t.append(row(who, i.kind === 'single' ? 'Single-use' : 'Team link', uses,
      `${days}d left`, el('div', { class: 'rowactions' }, revoke)));
  }
}

function renderCollectors() {
  const t = tbody('#collectors');
  t.replaceChildren();
  const rows = state.collectors || [];
  setEmpty('#collectors', '#collectorsEmpty', !rows.length);
  for (const c of rows) {
    const revoke = action('Revoke', true, () => {
      if (!confirm(`Revoke the collector on ${c.hostname || 'that machine'}? It stops reporting.`)) return;
      act(() => api('/api/admin/revoke-token', { token: c.token }));
    });
    // when it signed in answers no routine question; last sync does, so the
    // join date rides along as the machine's title
    const machine = el('span', {
      text: c.hostname || '?', title: 'signed in ' + day(c.created_at),
    });
    t.append(row(c.user_email, machine, syncCell(c.last_used_at),
      el('div', { class: 'rowactions' }, revoke)));
  }
}

function renderOrgs() {
  const t = tbody('#orgs');
  t.replaceChildren();
  const orgs = state.allOrgs || [];
  for (const o of orgs) {
    const controls = el('div', { class: 'rowactions' });
    if (!o.members && o.slug !== state.org.slug) {
      controls.append(action('Delete', true, () => {
        if (!confirm(`Delete '${o.slug}'? Its database is kept.`)) return;
        act(() => api('/api/admin/delete-org', { slug: o.slug }), '#orgErr');
      }));
    }
    t.append(row(
      o.name + (o.slug === state.org.slug ? ' (yours)' : ''),
      o.slug, String(o.members), day(o.created_at), controls));
  }
}

function renderDbPicker() {
  const sel = $('#dbOrg');
  const keep = sel.value;
  sel.replaceChildren(...(state.allOrgs || []).map((o) =>
    el('option', { value: o.slug, text: o.name || o.slug })));
  sel.value = (state.allOrgs || []).some((o) => o.slug === keep) ? keep : state.org.slug;
  syncDbId();
}

// show what the chosen org points at today, so the field is an edit of the
// current value rather than a blank waiting to be guessed at
function syncDbId() {
  const slug = $('#dbOrg').value;
  const org = (state.allOrgs || []).find((o) => o.slug === slug);
  $('#dbId').value = (org && org.database_id) || '';
}

// ---------------------------------------------------------------------------
// Invite dialog: both link kinds behind one action.
// ---------------------------------------------------------------------------
let inviteKind = 'single';

function setInviteKind(kind) {
  inviteKind = kind;
  for (const b of document.querySelectorAll('#inviteKind button')) {
    b.setAttribute('aria-pressed', String(b.dataset.kind === kind));
  }
  $('#invitePaneSingle').hidden = kind !== 'single';
  $('#invitePaneTeam').hidden = kind === 'single';
  $('#linkOut').hidden = true;
  clearErrors();
  (kind === 'single' ? $('#inviteEmail') : $('#teamDomain')).focus();
}

function openInvite() {
  clearErrors();
  $('#linkOut').hidden = true;
  $('#inviteEmail').value = '';
  $('#inviteModal').hidden = false;
  (inviteKind === 'single' ? $('#inviteEmail') : $('#teamDomain')).focus();
}

function closeInvite() {
  $('#inviteModal').hidden = true;
  $('#inviteOpen').focus();
}

function showLink(link, note) {
  $('#linkValue').value = link;
  $('#linkOut').hidden = false;
  $('#linkValue').title = note || '';
  $('#linkValue').select();
}

async function load() {
  state = await api('/api/admin/state');
  clearPlaceholders();  // anything that throws below must not leave them pulsing
  window.setViewer(state.viewer.email, state.org.name);
  $('#orgName').value = state.org.name;
  $('#orgMeta').textContent = `slug ${state.org.slug} - database ${state.org.database || 'none'}`;
  const admin = state.viewer.isAdmin;
  $('#readonly').hidden = admin;
  $('#orgName').disabled = !admin;
  $('#orgSave').disabled = !admin;
  for (const n of document.querySelectorAll('.adminonly')) n.hidden = !admin;
  const sys = !!state.viewer.isSystemAdmin;
  for (const n of document.querySelectorAll('.sysonly')) n.hidden = !sys;
  renderMembers();
  if (admin) { renderInvites(); renderCollectors(); }
  if (sys) { renderOrgs(); renderDbPicker(); }
}

// ---------------------------------------------------------------------------
// Wiring
// ---------------------------------------------------------------------------
$('#orgSave').addEventListener('click', () => act(async () => {
  await api('/api/admin/rename-org', { name: $('#orgName').value.trim() });
}));

$('#inviteOpen').addEventListener('click', openInvite);
$('#inviteCancel').addEventListener('click', closeInvite);
$('#inviteKind').addEventListener('click', (e) => {
  const btn = e.target.closest('button');
  if (btn) setInviteKind(btn.dataset.kind);
});
$('#inviteModal').addEventListener('click', (e) => {
  if (e.target === $('#inviteModal')) closeInvite();  // the backdrop, not the card
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !$('#inviteModal').hidden) closeInvite();
});
$('#inviteCreate').addEventListener('click', async () => {
  clearErrors();
  try {
    const r = inviteKind === 'single'
      ? await api('/api/invite', { email: $('#inviteEmail').value.trim() })
      : await api('/api/invite', {
        kind: 'team',
        domain: $('#teamDomain').value.trim(),
        max_uses: Number($('#teamMaxUses').value) || 0,
      });
    if (inviteKind === 'single') $('#inviteEmail').value = '';
    showLink(r.link, r.kind === 'team'
      ? `reusable, expires in ${r.expires_days} days`
      : `single use, expires in ${r.expires_days} days`);
    // the dialog stays open holding the link; refresh the list behind it
    load().catch((e) => showError(e.message, '#inviteErr'));
  } catch (e) {
    showError(e.message, '#inviteErr');
  }
});

$('#orgCreate').addEventListener('click', () => act(async () => {
  const r = await api('/api/admin/create-org', {
    name: $('#newOrgName').value.trim(),
    owner_email: $('#newOrgOwner').value.trim(),
  });
  $('#newOrgName').value = ''; $('#newOrgOwner').value = '';
  $('#orgLinkOut').hidden = true;  // never show a previous org's link
  if (r.invite_link) {
    $('#orgLinkValue').value = r.invite_link;
    $('#orgLinkValue').title = 'send this to the owner; they join as the org admin';
    $('#orgLinkOut').hidden = false;
    $('#orgLinkValue').select();
  }
}, '#orgErr'));

$('#dbOrg').addEventListener('change', syncDbId);
$('#dbSave').addEventListener('click', () => {
  const slug = $('#dbOrg').value;
  const id = $('#dbId').value.trim();
  const org = (state.allOrgs || []).find((o) => o.slug === slug) || {};
  if (id === (org.database_id || '')) {
    showError('that is already its database', '#dbErr');
    return;
  }
  if (!confirm(`Point '${slug}' at ${id}?\n\nIts dashboard and its collectors both `
             + `switch to that database. Usage already reported stays in `
             + `${org.database_id || 'the old database'} and will not appear on the `
             + `dashboard any more.`)) return;
  act(() => api('/api/admin/set-org-database', { slug, database_id: id }), '#dbErr');
});

function copyField(sel) {
  const f = $(sel);
  f.select();
  if (navigator.clipboard) navigator.clipboard.writeText(f.value);
  else document.execCommand('copy');
}
$('#orgLinkCopy').addEventListener('click', () => copyField('#orgLinkValue'));
$('#linkCopy').addEventListener('click', () => copyField('#linkValue'));

load().catch((e) => showError(e.message));
