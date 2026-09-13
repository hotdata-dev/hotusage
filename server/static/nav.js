'use strict';

// ---------------------------------------------------------------------------
// One header for every page: brand, page links, and an account menu. Pages
// declare which nav item is current with <body data-page="...">; the account
// menu holds identity and the things that are not page navigation.
// ---------------------------------------------------------------------------
(function () {
  // Nine cells warming toward the corner -- the shape of a usage heatmap. Drawn
  // with currentColor and the same series tokens the charts use, so it follows
  // the theme with no image asset and nothing extra to load.
  //
  // The token fills go in `style`, not in a fill="" presentation attribute: a
  // presentation attribute is a CSS declaration, so an engine that does not
  // resolve var() there drops it, and `fill` then inherits "none" from the root
  // svg -- a cell that silently vanishes. An inline style resolves everywhere.
  const MARK = `
    <svg class="mark" width="22" height="22" viewBox="0 0 32 32" fill="none" aria-hidden="true">
      <rect x="3"  y="3"  width="8" height="8" rx="2.5" fill="currentColor" opacity=".18"/>
      <rect x="12" y="3"  width="8" height="8" rx="2.5" fill="currentColor" opacity=".3"/>
      <rect x="21" y="3"  width="8" height="8" rx="2.5" style="fill: var(--s-out)" opacity=".55"/>
      <rect x="3"  y="12" width="8" height="8" rx="2.5" fill="currentColor" opacity=".3"/>
      <rect x="12" y="12" width="8" height="8" rx="2.5" style="fill: var(--s-cw)" opacity=".6"/>
      <rect x="21" y="12" width="8" height="8" rx="2.5" style="fill: var(--s-cr)" opacity=".8"/>
      <rect x="3"  y="21" width="8" height="8" rx="2.5" style="fill: var(--s-out)" opacity=".55"/>
      <rect x="12" y="21" width="8" height="8" rx="2.5" style="fill: var(--s-cr)" opacity=".8"/>
      <rect x="21" y="21" width="8" height="8" rx="2.5" style="fill: var(--s-in)"/>
    </svg>`;

  const PAGES = [
    { id: 'dashboard', href: '/', label: 'Dashboard' },
    { id: 'organization', href: '/admin', label: 'Organization' },
  ];

  function node(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === 'class') n.className = v;
      else if (k === 'text') n.textContent = v;
      else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v);
    }
    for (const c of kids) if (c != null) n.append(c);
    return n;
  }

  const current = document.body.dataset.page || '';
  const header = document.querySelector('header.top');
  if (!header) return;

  const links = node('nav', { class: 'navlinks' },
    ...PAGES.map((p) => node('a', {
      class: 'navlink' + (p.id === current ? ' current' : ''),
      href: p.href,
      text: p.label,
      ...(p.id === current ? { 'aria-current': 'page' } : {}),
    })));

  const orgList = node('div', { id: 'navOrgs' });
  const menu = node('div', { class: 'menu', hidden: '' },
    node('div', { class: 'menu-email', id: 'navEmail' }),
    node('div', { class: 'menu-sep' }),
    orgList,
    node('a', { class: 'menu-item', href: '/admin', text: 'Organization' }),
    node('a', { class: 'menu-item', href: '/logout', text: 'Log out' }));

  // Org switcher: shown only when the account belongs to more than one org.
  // Switching changes the ACTIVE org server-side (dashboard + ingest routing),
  // then reloads so every view re-reads it.
  fetch('/api/orgs').then((r) => (r.ok ? r.json() : null)).then((d) => {
    if (!d || !d.orgs || d.orgs.length < 2) return;
    orgList.append(node('div', { class: 'menu-email', text: 'Switch organization' }));
    for (const o of d.orgs) {
      const isActive = o.slug === d.active;
      orgList.append(node('button', {
        class: 'menu-item menu-org' + (isActive ? ' active' : ''),
        type: 'button',
        text: (isActive ? '✓ ' : '') + o.name,
        onclick: async (e) => {
          e.stopPropagation();
          if (isActive) { menu.hidden = true; return; }
          const r = await fetch('/api/switch-org', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ slug: o.slug }),
          });
          if (r.ok) { location.href = '/'; return; }
          const err = await r.json().catch(() => ({}));
          e.target.textContent = err.error || 'could not switch';
        },
      }));
    }
    orgList.append(node('div', { class: 'menu-sep' }));
  }).catch(() => {});

  const button = node('button', {
    class: 'account', id: 'navAccount', type: 'button', 'aria-haspopup': 'menu',
    onclick: (e) => { e.stopPropagation(); menu.hidden = !menu.hidden; },
  }, node('span', { id: 'navWho', text: 'Account' }), node('span', { class: 'caret', text: '▾' }));

  document.addEventListener('click', () => { menu.hidden = true; });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') menu.hidden = true; });

  const brand = node('a', { class: 'brand', href: '/' },
    node('h1', { text: 'hotusage' }));
  // innerHTML, not a parsed node: the mark is a literal above, never user input
  brand.prepend(Object.assign(document.createElement('span'),
                              { className: 'markwrap', innerHTML: MARK }));

  // The header is a full-width bar; its contents stay on the page's 1100px
  // column, so the rule under it runs edge to edge while nothing shifts.
  // Refresh is NOT here: it belongs beside the "fetched at" note it updates,
  // which lives at the right edge of the dashboard's filter row (index.html).
  header.replaceChildren(node('div', { class: 'topin' },
    brand,
    links,
    node('span', { class: 'spacer' }),
    node('div', { class: 'accountwrap' }, button, menu)));

  // pages call this once they know who is signed in
  window.setViewer = (email, org) => {
    document.getElementById('navWho').textContent = org ? `${org}` : email;
    document.getElementById('navEmail').textContent = email;
  };
})();
