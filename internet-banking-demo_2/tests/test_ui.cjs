// Dependency-free localization checks. Run: node --test tests/test_ui.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const analytics = fs.readFileSync(path.join(__dirname, '..', 'analytics.js'), 'utf8');
const catalogBlock = html.match(/\/\/ i18n-catalog:start([\s\S]*?)\/\/ i18n-catalog:end/);
assert.ok(catalogBlock, 'The translation catalog must have an isolated test boundary');
const context = vm.createContext({});
vm.runInContext(`${catalogBlock[1]}\nglobalThis.catalog = I18N;`, context);
const analyticsCatalog = analytics.match(/\/\/ analytics-catalog:start([\s\S]*?)\/\/ analytics-catalog:end/);
assert.ok(analyticsCatalog, 'Analytics translations must also have a test boundary');
vm.runInContext(analyticsCatalog[1], context);
const catalog = context.catalog;
const keys = Object.keys(catalog.en).sort();
const parameters = text => [...new Set([...text.matchAll(/\{(\w+)\}/g)].map(m => m[1]))].sort();

test('English, Russian and Kazakh have the same nonempty translation keys', () => {
  assert.deepEqual(Object.keys(catalog).sort(), ['en', 'kk', 'ru']);
  for (const [language, messages] of Object.entries(catalog)) {
    assert.deepEqual(Object.keys(messages).sort(), keys, `${language}: missing or extra translations`);
    for (const [key, value] of Object.entries(messages)) {
      assert.equal(typeof value, 'string', `${language}.${key} must be text`);
      assert.ok(value.trim(), `${language}.${key} cannot be empty`);
    }
  }
});

test('Translated messages preserve interpolation parameters', () => {
  for (const key of keys) {
    for (const language of ['ru', 'kk']) {
      assert.deepEqual(parameters(catalog[language][key]), parameters(catalog.en[key]),
        `${language}.${key}: values such as amounts, names or dates would disappear`);
    }
  }
});

test('Russian and Kazakh text survives UTF-8 file writing', () => {
  for (const language of ['ru', 'kk']) {
    for (const [key, value] of Object.entries(catalog[language])) {
      assert.match(value, /[\u0400-\u04ff]/u, `${language}.${key}: translated letters are missing`);
      assert.doesNotMatch(value, /\?{2,}/, `${language}.${key}: text was replaced by question marks`);
    }
  }
  assert.match(catalog.kk.language, /[\u0406\u0456\u049a\u049b\u04d8\u04d9\u04b0\u04b1\u04e8\u04e9]/u);
});

test('Every literal translation key used by markup or JavaScript exists', () => {
  const sources = html + analytics;
  const referenced = [
    ...[...sources.matchAll(/data-i18n(?:-[\w-]+)?="([\w.-]+)"/g)].map(m => m[1]),
    ...[...sources.matchAll(/\bt\(\s*['"]([\w.-]+)['"]/g)].map(m => m[1]),
  ];
  assert.ok(referenced.length > 20, 'Expected the application to reference its catalog');
  for (const key of new Set(referenced)) {
    assert.ok(Object.hasOwn(catalog.en, key), `Unknown translation key: ${key}`);
  }
});

test('Status filtering preserves canonical API values in every language', () => {
  const select = html.match(/<select\b[^>]*id="historyStatus"[^>]*>([\s\S]*?)<\/select>/);
  assert.ok(select, 'History status selector must exist');
  const values = [...select[1].matchAll(/<option\b[^>]*value="([^"]*)"/g)].map(m => m[1]);
  assert.deepEqual(values, ['', 'Pending', 'Completed', 'Failed', 'Scheduled', 'Cancelled']);
});

test('Language switching uses direct accessible buttons and persists selection', () => {
  const languages = [...html.matchAll(/data-language="(en|ru|kk)"/g)].map(m => m[1]);
  assert.deepEqual(languages, ['en', 'ru', 'kk']);
  assert.match(html, /role="group"[^>]*id="languageControl"/);
  assert.match(html, /button\.onclick\s*=\s*\(\)\s*=>\s*setLanguage\(button\.dataset\.language\)/);
  assert.match(html, /button\.setAttribute\('aria-pressed',\s*String\(button\.dataset\.language === language\)\)/);
  assert.doesNotMatch(html, /id="languageSelect"/);
});

test('Inline application scripts parse before reaching the browser', () => {
  for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) {
    new vm.Script(match[1]);
  }
  new vm.Script(analytics);
});

// Execute the application's functions against a small DOM adapter. No server,
// installed browser, or real customer database is needed for these regressions.
function appHarness(now = Date.parse('2026-11-25T10:20:59Z')) {
  const elements = [];
  function element(tag, attributes = '') {
    const attrs = Object.fromEntries([...attributes.matchAll(/([\w-]+)="([^"]*)"/g)].map(match => [match[1], match[2]]));
    const classes = new Set((attrs.class || '').split(/\s+/).filter(Boolean));
    let value = attrs.value || '', markup = '', content = '';
    const node = {
      id: attrs.id || '', tagName: tag.toUpperCase(), type: attrs.type || '',
      min: attrs.min || '', max: attrs.max || '', minLength: Number(attrs.minlength || 0),
      disabled: false, required: false, children: [], dataset: {}, htmlWrites: 0,
      validity: {valid: true, customError: false},
      classList: {
        contains: name => classes.has(name),
        add: name => classes.add(name), remove: name => classes.delete(name),
        toggle(name, on) { if (on ?? !classes.has(name)) classes.add(name); else classes.delete(name); },
      },
      setAttribute(name, content) { attrs[name] = String(content); },
      getAttribute(name) { return attrs[name]; },
      setCustomValidity(message) { this.validationMessage = message; this.validity.customError = Boolean(message); },
      append(...children) { this.children.push(...children); },
      focus() { document.activeElement = this; },
      get textContent() { return content + this.children.map(child => typeof child === 'string' ? child : child.textContent).join(''); },
      set textContent(next) {
        if (this.children.includes(document.activeElement)) document.activeElement = null;
        content = String(next); markup = ''; this.children = [];
      },
      get value() { return value; }, set value(next) { value = String(next); },
      get innerHTML() { return markup; }, set innerHTML(next) {
        if (this.children.includes(document.activeElement)) document.activeElement = null;
        markup = next; content = ''; this.children = []; this.htmlWrites++;
        if (tag === 'select') value = next.match(/<option value="([^"]*)"/)?.[1] || '';
      },
    };
    for (const [name, content] of Object.entries(attrs)) {
      if (name.startsWith('data-')) node.dataset[name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = content;
    }
    elements.push(node);
    return node;
  }
  for (const match of html.matchAll(/<(\w+)\b([^>]*)>/g)) element(match[1], match[2]);
  const byId = new Map(elements.filter(node => node.id).map(node => [node.id, node]));
  const storage = new Map(), events = [];
  const document = {
    documentElement: {}, title: '', activeElement: null,
    getElementById: id => byId.get(id), createElement: tag => element(tag),
    dispatchEvent: event => events.push(event.type),
    querySelectorAll(selector) {
      if (selector === 'input,select,textarea') return elements.filter(node => ['INPUT','SELECT','TEXTAREA'].includes(node.tagName));
      if (selector === '#authForm input') return ['authName','authEmail','authPassword','authPasswordConfirm'].map(id => byId.get(id));
      const attribute = selector.match(/^\[([^\]]+)\]$/)?.[1];
      return attribute ? elements.filter(node => node.getAttribute(attribute) !== undefined) : [];
    },
  };
  const sandbox = vm.createContext({
    document, localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)},
    Date: class extends Date { static now() { return now; } },
    Event: class { constructor(type) { this.type = type; } },
  });
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
  const setupEnd = script.indexOf("document.querySelectorAll('[data-language]').forEach(button => { button.onclick");
  assert.ok(setupEnd > 0, 'App setup boundary must exist');
  vm.runInContext(script.slice(0, setupEnd), sandbox);
  return {get: id => byId.get(id), run: code => vm.runInContext(code, sandbox), document, storage, events};
}

test('Switching language preserves entered transfer data and canonical filter values', () => {
  const app = appHarness();
  app.run("accounts = [{id:1,label:'Everyday account',number:'KZ1',name:'Malika',balance:1000}]; currentId = 1; recipients = [{id:2,name:'Amina',number:'KZ2'},{id:3,name:'Dias',number:'KZ3'}]; renderRecipients();");
  const entered = {amount:'1250.50', description:'На подарок <3', recipient:'3', transferMode:'scheduled', scheduledFor:'2026-11-27T14:30', historyStatus:'Completed'};
  for (const [id, value] of Object.entries(entered)) app.get(id).value = value;
  for (const language of ['ru','kk','en']) {
    app.run(`setLanguage('${language}')`);
    assert.equal(app.document.documentElement.lang, language);
    assert.equal(app.storage.get('northstar.language'), language);
    assert.equal(app.document.title, catalog[language].pageTitle);
    for (const [id, value] of Object.entries(entered)) assert.equal(app.get(id).value, value, `${language}: ${id} changed`);
    const selected = app.document.querySelectorAll('[data-language]').filter(button => button.getAttribute('aria-pressed') === 'true');
    assert.deepEqual(selected.map(button => button.dataset.language), [language]);
  }
  app.run("setLanguage('unsupported')");
  assert.equal(app.document.documentElement.lang, 'en');
});

test('Immediate transfers disable the hidden scheduled date constraint', () => {
  const app = appHarness();
  app.get('scheduledFor').value = '2020-01-01T00:00';
  app.get('transferMode').value = 'scheduled';
  app.run('renderTransferMode()');
  assert.equal(app.get('scheduledFor').disabled, false);
  assert.equal(app.get('scheduledFor').required, true);
  app.get('transferMode').value = 'now';
  app.run('renderTransferMode()');
  assert.equal(app.get('scheduledFor').disabled, true);
  assert.equal(app.get('scheduledFor').required, false);
  assert.equal(app.get('scheduledField').classList.contains('hidden'), true);
});

test('Earliest selectable scheduled minute meets the backend 30-second lead time', () => {
  for (const seconds of [0, 29, 30, 31, 59]) {
    const now = Date.parse(`2026-11-25T10:20:${String(seconds).padStart(2,'0')}Z`);
    const app = appHarness(now);
    app.run('setScheduledMinimum()');
    const earliest = Date.parse(app.get('scheduledFor').min + ':00+05:00');
    assert.ok(earliest - now >= 30000, `At second ${seconds} the form permits less than 30 seconds`);
    assert.ok(earliest - now < 90000, 'The next valid minute should remain selectable');
  }
});

test('Scheduled audit timestamps use the chosen language without changing UTC+5', () => {
  const app = appHarness();
  for (const language of ['en','ru','kk']) {
    app.run(`language = '${language}'`);
    const rendered = app.run("auditDetails('Scheduled 1,250.50 KZT for 2026-11-27 14:30 UTC+5. Reminder: No reminder note.')");
    const expectedDate = app.run("date('2026-11-27T14:30:00+05:00')");
    assert.ok(rendered.includes(expectedDate), `${language}: localized date is missing from ${rendered}`);
    assert.ok(rendered.includes(catalog[language].noReminder));
  }
});

test('Kazakh month names remain readable without browser locale data and roll over at UTC+5', () => {
  const app = appHarness();
  app.run("language = 'kk'");
  assert.equal(app.run("monthName('2026-11-25T16:50:00Z')"), '2026 ж. қараша');
  assert.equal(app.run("date('2026-11-25T16:50:00Z')"), '25 қараша 2026 ж., 21:50 (UTC+5)');
  assert.equal(app.run("monthName('2026-12-31T18:59:59Z')"), '2026 ж. желтоқсан');
  assert.equal(app.run("monthName('2026-12-31T19:00:00Z')"), '2027 ж. қаңтар');
  assert.equal(app.run("date('2026-12-31T19:00:00Z')"), '1 қаңтар 2027 ж., 00:00 (UTC+5)');
  assert.equal(app.run("localDay('2026-12-31T19:00:00Z')"), '2027-01-01');
  assert.equal(app.run("monthName('invalid')"), '—');
});

test('Notices follow terminal transfer status and retranslate their values', () => {
  const app = appHarness();
  app.run("lastTransfer = {requestId:'TR-CHECK',status:'Pending',amount:1000,to:'Amina'}; notice($('notice'), () => transferNotice(lastTransfer), () => transferNoticeKind(lastTransfer));");
  assert.equal(app.get('notice').className, 'notice show info');
  for (const [status, kind] of [['Completed','success'], ['Failed','error']]) {
    app.run(`lastTransfer.status = '${status}'; language = 'ru'; renderNotice($('notice'))`);
    assert.equal(app.get('notice').className, `notice show ${kind}`);
    assert.ok(app.get('notice').textContent.includes(catalog.ru['status' + status]));
    assert.ok(app.get('notice').textContent.includes('TR-CHECK'));
  }
});

test('Unchanged polling retains notice buttons and focus while refreshing their callbacks', () => {
  const app = appHarness();
  app.run("globalThis.clicked = ''; notice($('notice'), 'Ready', 'info', [{key:'viewDetails',run:() => globalThis.clicked = 'old'}])");
  const original = app.get('notice').children.find(child => child.tagName === 'BUTTON');
  original.focus();
  app.run("notice($('notice'), 'Ready', 'info', [{key:'viewDetails',run:() => globalThis.clicked = 'current'}]); renderNotice($('notice'))");
  const current = app.get('notice').children.find(child => child.tagName === 'BUTTON');
  assert.equal(current, original, 'Polling must not replace a button under the cursor');
  assert.equal(app.document.activeElement, original);
  current.onclick();
  assert.equal(app.run('globalThis.clicked'), 'current');
});

test('Notice changes preserve focus on the same action and hidden notices can be reopened', () => {
  const app = appHarness();
  app.run("notice($('notice'), msg('requestFailed'), 'error', [{key:'viewDetails',run:() => {}}])");
  const original = app.get('notice').children.find(child => child.tagName === 'BUTTON');
  original.focus();
  app.run("language = 'ru'; renderNotice($('notice'))");
  const translated = app.get('notice').children.find(child => child.tagName === 'BUTTON');
  assert.equal(translated.textContent, catalog.ru.viewDetails);
  assert.equal(app.document.activeElement, translated, 'Translation must retain keyboard focus');
  app.run("hideNotice($('notice')); notice($('notice'), msg('requestFailed'), 'error', [{key:'viewDetails',run:() => {}}])");
  assert.equal(app.get('notice').children.filter(child => child.tagName === 'BUTTON').length, 1);
  assert.equal(app.get('notice').className, 'notice show error');
});

test('Identical history polling keeps the existing table; changes and cleared filters still render', () => {
  const app = appHarness();
  app.run("historyLoaded = true; historyEntries = [{requestId:'TR-POLL',counterparty:'Amina',direction:'outgoing',status:'Scheduled',amount:1000,createdAt:'2026-11-25T10:00:00Z'}]; renderHistory()");
  const history = app.get('historyBody');
  const firstWrites = history.htmlWrites;
  app.run('renderHistory()');
  assert.equal(history.htmlWrites, firstWrites, 'Polling must not detach history action buttons');
  app.run("setLanguage('en')");
  assert.ok(history.innerHTML.includes('TR-POLL'), 'Selecting the current language must retain the rendered table');
  app.run("historyEntries[0].status = 'Completed'; renderHistory()");
  assert.equal(history.htmlWrites, firstWrites + 1);
  assert.ok(history.innerHTML.includes('Completed'));
  app.get('historySearch').value = 'no match';
  app.run('renderHistory()');
  assert.equal(history.textContent, catalog.en.noMatches);
  app.get('historySearch').value = '';
  app.run('renderHistory()');
  assert.ok(history.innerHTML.includes('TR-POLL'), 'Clearing filters must restore the table');
});

test('Backend limit and scheduled-transfer errors translate without losing values', () => {
  const app = appHarness();
  const cases = [
    ['Minimum transfer is 100.00 KZT', "t('minimumTransfer', {amount:money(100)})"],
    ['Maximum per transfer is 500,000.00 KZT', "t('maximumTransfer', {amount:money(500000)})"],
    ['Daily transfer limit is 1,000,000.00 KZT. Remaining today: 249,999.50 KZT', "t('dailyExceeded', {limit:money(1000000), remaining:money(249999.50)})"],
    ['This transfer is already completed and cannot be cancelled', "t('cannotCancel', {status:statusText('Completed')})"],
    ['Insufficient funds at scheduled time', "t('insufficientScheduled')"],
    ['This transfer reference is already in use', "t('referenceUsed')"],
  ];
  for (const language of ['en','ru','kk']) {
    app.run(`language = '${language}'`);
    for (const [message, expected] of cases) {
      assert.equal(app.run(`systemText(${JSON.stringify(message)})`), app.run(expected), `${language}: ${message}`);
    }
  }
});

test('Existing input validation changes language without changing the entered amount', () => {
  const app = appHarness();
  const amount = app.get('amount');
  amount.value = '10'; amount.min = '100.00';
  amount.validity = {valid:false, customError:false, rangeUnderflow:true};
  app.run("language = 'en'; localizeValidity($('amount'))");
  assert.equal(amount.validationMessage, catalog.en.valueMinimum.replace('{min}','100.00'));
  app.run("setLanguage('kk')");
  assert.equal(amount.validationMessage, catalog.kk.valueMinimum.replace('{min}','100.00'));
  assert.equal(amount.value, '10');
});

test('History and receipts escape user text and retain status and reference', () => {
  const app = appHarness();
  app.run("historyLoaded = true; historyEntries = [{requestId:'TR-SAFE',counterparty:'<img src=x onerror=alert(1)>',to:'<Amina>',recipientNumber:'KZ2',direction:'outgoing',status:'Completed',amount:1250,description:'<script>alert(1)</script>',createdAt:'2026-11-25T10:00:00Z',checkedAt:'2026-11-25T10:00:01Z'}]; renderHistory();");
  const rendered = app.get('historyBody').innerHTML;
  assert.ok(rendered.includes('&lt;img'));
  assert.ok(rendered.includes('&lt;script&gt;'));
  assert.ok(rendered.includes('−'));
  const receipt = app.run('receiptHtml(historyEntries[0])');
  assert.ok(receipt.includes('&lt;Amina&gt;'));
  assert.ok(receipt.includes('TR-SAFE'));
  assert.ok(receipt.includes('Completed'));
  assert.doesNotMatch(receipt, /<script>/);
});
