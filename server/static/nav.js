'use strict';

// ---------------------------------------------------------------------------
// One header for every page: brand, page links, and an account menu. Pages
// declare which nav item is current with <body data-page="...">; the account
// menu holds identity and the things that are not page navigation.
// ---------------------------------------------------------------------------
(function () {
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

  const menu = node('div', { class: 'menu', hidden: '' },
    node('div', { class: 'menu-email', id: 'navEmail' }),
    node('div', { class: 'menu-sep' }),
    node('a', { class: 'menu-item', href: '/admin', text: 'Organization' }),
    node('a', { class: 'menu-item', href: '/logout', text: 'Log out' }));

  const button = node('button', {
    class: 'account', id: 'navAccount', type: 'button', 'aria-haspopup': 'menu',
    onclick: (e) => { e.stopPropagation(); menu.hidden = !menu.hidden; },
  }, node('span', { id: 'navWho', text: 'Account' }), node('span', { class: 'caret', text: '▾' }));

  document.addEventListener('click', () => { menu.hidden = true; });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') menu.hidden = true; });

  header.replaceChildren(
    node('a', { class: 'brand', href: '/' },
      node('h1', { text: 'hotusage' })),
    links,
    node('span', { class: 'spacer' }),
    ...(current === 'dashboard'
      ? [node('button', { class: 'refresh', id: 'refresh', type: 'button', text: 'Refresh' })]
      : []),
    node('div', { class: 'accountwrap' }, button, menu));

  // pages call this once they know who is signed in
  window.setViewer = (email, org) => {
    document.getElementById('navWho').textContent = org ? `${org}` : email;
    document.getElementById('navEmail').textContent = email;
  };
})();
