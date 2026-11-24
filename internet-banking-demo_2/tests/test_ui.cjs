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

test('Inline application scripts parse before reaching the browser', () => {
  for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) {
    new vm.Script(match[1]);
  }
  new vm.Script(analytics);
});
