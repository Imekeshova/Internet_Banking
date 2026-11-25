// Run the real analytics client with controlled network responses and a tiny DOM.
// No browser dependencies or real account/database access are required.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '..', 'analytics.js'), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));

function dashboard() {
  const document = new EventTarget();
  document.documentElement = { lang: 'en' };
  document.hidden = false;
  document.activeElement = null;

  // This DOM double parses rendered buttons only, not application source code.
  // Replacing markup removes focus just as replacing browser DOM nodes does.
  class Element extends EventTarget {
    constructor(attributes = {}) {
      super();
      this.id = attributes.id || '';
      this.className = attributes.class || '';
      this.dataset = {};
      if (attributes['data-stat-detail']) this.dataset.statDetail = attributes['data-stat-detail'];
      this.children = [];
      this.scrollTop = 0;
      this.markup = '';
    }
    setAttribute(name, value) { this[name] = value; }
    focus() { document.activeElement = this; }
    contains(element) { return this === element || this.children.includes(element); }
    get innerHTML() { return this.markup; }
    set innerHTML(markup) {
      if (this.contains(document.activeElement)) document.activeElement = null;
      this.markup = markup;
      this.children = [];
      for (const tag of markup.matchAll(/<button\b([^>]*)>/g)) {
        const attributes = Object.fromEntries([...tag[1].matchAll(/([\w-]+)="([^"]*)"/g)]
          .map(match => [match[1], match[2]]));
        this.children.push(new Element(attributes));
      }
      if (markup.includes('class="stat-leaders"')) {
        this.children.push(new Element({ class: 'stat-leaders' }));
      }
    }
    replaceChildren() { this.innerHTML = ''; }
    querySelectorAll(selector) {
      if (selector.startsWith('#')) return this.children.filter(child => child.id === selector.slice(1));
      if (selector.startsWith('.')) return this.children.filter(child => child.className.split(' ').includes(selector.slice(1)));
      if (selector === '[data-stat-detail]') return this.children.filter(child => child.dataset.statDetail);
      throw new Error(`Unexpected selector: ${selector}`);
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  }

  let hidden = true;
  let observer;
  let panel;
  const email = { textContent: '' };
  const bank = {
    classList: { contains: name => name === 'hidden' && hidden },
    querySelector: selector => {
      assert.equal(selector, '.greeting');
      return { after: element => { panel = element; } };
    },
  };
  document.createElement = () => new Element();
  document.getElementById = id => ({ bankView: bank, userEmail: email, analyticsPanel: panel })[id]
    || panel?.querySelector('#' + id);

  const requests = [];
  const I18N = { en: {}, ru: {}, kk: {} };
  const escape = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;').replaceAll('"', '&quot;');
  const context = vm.createContext({
    document, I18N, AbortController, Event,
    MutationObserver: class { constructor(callback) { observer = callback; } observe() {} },
    setInterval: () => 1,
    esc: escape,
    money: amount => `${Number(amount).toFixed(2)} KZT`,
    date: value => value,
    monthName: value => value.slice(0, 7),
    t: (key, values = {}) => (I18N[document.documentElement.lang][key] || key)
      .replace(/\{(\w+)\}/g, (_, name) => values[name] ?? `{${name}}`),
    showDetails: () => {},
    api: (url, options) => {
      assert.equal(url, '/api/analytics');
      return new Promise((resolve, reject) => requests.push({ resolve, reject, signal: options.signal }));
    },
  });
  vm.runInContext(source, context);

  return {
    panel, document, requests,
    fire(type) { document.dispatchEvent(new Event(type)); },
    signIn(identity) { email.textContent = identity; hidden = false; observer(); },
    signOut() {
      document.dispatchEvent(new Event('banksessionchange'));
      email.textContent = '';
      hidden = true;
      observer();
    },
    async resolve(index, data) { requests[index].resolve(data); await settle(); },
    async reject(index) { requests[index].reject(new Error('Network unavailable')); await settle(); },
  };
}

function statistics(total, recipient = 'Recipient') {
  const transfer = { requestId: 'TR-' + '1'.repeat(32), amount: total, recipient,
    completedAt: '2026-11-24T10:00:00+00:00' };
  return {
    asOf: '2026-11-24T10:00:00+00:00',
    week: { totalAmount: total, count: 1 },
    month: { totalAmount: total, count: 1, largest: transfer, smallest: transfer,
      frequentRecipients: [{ name: recipient, count: 1, totalAmount: total }] },
  };
}

test('Signing out clears analytics and ignores a late successful response', async () => {
  const app = dashboard();
  app.signIn('first@example.test');
  assert.equal(app.requests.length, 1);
  app.signOut();
  assert.equal(app.requests[0].signal.aborted, true);
  assert.equal(app.panel.innerHTML, '');
  // The response still resolves to exercise the guard even if cancellation races.
  await app.resolve(0, statistics(1234, 'Private recipient'));
  assert.equal(app.panel.innerHTML, '');
});

test('A prior user response cannot overwrite the next user statistics', async () => {
  const app = dashboard();
  app.signIn('first@example.test');
  app.signOut();
  app.signIn('second@example.test');
  assert.equal(app.requests.length, 2);
  await app.resolve(1, statistics(200, 'Second recipient'));
  await app.resolve(0, statistics(999, 'Private first recipient'));
  assert.ok(app.panel.innerHTML.includes('200.00 KZT'));
  assert.ok(app.panel.innerHTML.includes('Second recipient'));
  assert.ok(!app.panel.innerHTML.includes('999.00 KZT'));
  assert.ok(!app.panel.innerHTML.includes('Private first recipient'));
});

test('Refresh failure preserves the last totals and successful recovery removes the warning', async () => {
  const app = dashboard();
  app.signIn('first@example.test');
  await app.resolve(0, statistics(1200));
  app.fire('bankrefresh');
  await app.reject(1);
  assert.ok(app.panel.innerHTML.includes('1200.00 KZT'));
  assert.ok(app.panel.innerHTML.includes('Could not refresh statistics'));
  app.fire('bankrefresh');
  await app.resolve(2, statistics(2300));
  assert.ok(app.panel.innerHTML.includes('2300.00 KZT'));
  assert.ok(!app.panel.innerHTML.includes('Could not refresh statistics'));
});

test('Smallest detail keeps keyboard focus when largest refers to the same transfer', async () => {
  const app = dashboard();
  app.signIn('first@example.test');
  await app.resolve(0, statistics(100));
  const smallest = app.panel.querySelector('#statSmallestDetail');
  assert.ok(smallest, 'The smallest transfer must have an independently focusable button');
  smallest.focus();
  app.fire('bankrefresh');
  await app.resolve(1, statistics(100));
  assert.equal(app.document.activeElement?.id, 'statSmallestDetail');
  assert.notEqual(app.document.activeElement, smallest, 'Focus follows the replacement DOM node');
  app.document.documentElement.lang = 'ru';
  app.fire('languagechange');
  assert.equal(app.document.activeElement?.id, 'statSmallestDetail');
});
