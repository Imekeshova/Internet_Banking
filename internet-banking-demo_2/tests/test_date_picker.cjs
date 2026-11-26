// Pure calendar/locale checks: no DOM, network, accounts, or database access.
// Run: node --test tests/test_date_picker.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { spawnSync } = require('node:child_process');

const modulePath = path.join(__dirname, '..', 'date-picker.js');
const source = fs.readFileSync(modulePath, 'utf8');
const sandbox = vm.createContext({});
vm.runInContext(source, sandbox);
const { parse, format, localize } = sandbox.BankDates;

test('Dates use day-first locale formats, including ambiguous day/month pairs', () => {
  assert.equal(parse('03/04/2026', 'date', 'en'), '2026-04-03');
  assert.equal(parse('03.04.2026', 'date', 'ru'), '2026-04-03');
  assert.equal(parse('03.04.2026', 'date', 'kk'), '2026-04-03');
  assert.equal(parse('27/11/2026 14:05', 'datetime-local', 'en'), '2026-11-27T14:05');
  for (const language of ['ru', 'kk']) {
    assert.equal(parse('27.11.2026 14:05', 'datetime-local', language), '2026-11-27T14:05');
  }
});

test('Canonical ISO values are accepted only for the matching field kind', () => {
  for (const language of ['en', 'ru', 'kk']) {
    assert.equal(parse('2026-11-27', 'date', language), '2026-11-27');
    assert.equal(parse('2026-11-27T14:05', 'datetime-local', language), '2026-11-27T14:05');
    assert.equal(parse('2026-11-27T14:05', 'date', language), null);
    assert.equal(parse('2026-11-27', 'datetime-local', language), null);
    assert.equal(parse('2026-11-27T14:05:00', 'datetime-local', language), null);
    assert.equal(parse('2026-11-27T14:05Z', 'datetime-local', language), null);
    assert.equal(parse('2026-11-27T14:05+05:00', 'datetime-local', language), null);
  }
});

test('Empty input stays empty and surrounding whitespace does not change valid values', () => {
  for (const language of ['en', 'ru', 'kk']) {
    for (const kind of ['date', 'datetime-local']) {
      assert.equal(parse('', kind, language), '');
      assert.equal(parse('  \t\n ', kind, language), '');
      assert.equal(format('', kind, language), '');
      assert.equal(format('not-a-date', kind, language), '');
    }
  }
  assert.equal(parse(' 27/11/2026 ', 'date', 'en'), '2026-11-27');
  assert.equal(parse(' 27.11.2026 14:05 ', 'datetime-local', 'kk'), '2026-11-27T14:05');
});

test('Real calendar validation rejects rolled-over days and handles Gregorian leap years', () => {
  const valid = ['0001-01-01', '0004-02-29', '0099-12-31', '1600-02-29',
    '2000-02-29', '2024-02-29', '2026-04-30', '9999-12-31'];
  const invalid = ['0000-01-01', '10000-01-01', '0099-02-29', '1700-02-29',
    '1900-02-29', '2100-02-29', '2026-02-29', '2024-02-30', '2026-04-31',
    '2026-06-31', '2026-11-31', '2026-00-10', '2026-13-10', '2026-01-00', '2026-01-32'];
  for (const iso of valid) {
    assert.equal(parse(iso, 'date', 'en'), iso, iso);
    assert.equal(parse(`${iso}T12:30`, 'datetime-local', 'en'), `${iso}T12:30`, iso);
  }
  for (const iso of invalid) {
    assert.equal(parse(iso, 'date', 'en'), null, iso);
    assert.equal(parse(`${iso}T12:30`, 'datetime-local', 'en'), null, iso);
  }
  assert.equal(parse('31/04/2026', 'date', 'en'), null);
  assert.equal(parse('29.02.1900', 'date', 'ru'), null);
  assert.equal(parse('29.02.2000', 'date', 'kk'), '2000-02-29');
});

test('Time parsing accepts minute boundaries and rejects overflow or extra precision', () => {
  for (const time of ['00:00', '00:01', '12:00', '23:59']) {
    assert.equal(parse(`27/11/2026 ${time}`, 'datetime-local', 'en'), `2026-11-27T${time}`);
  }
  for (const time of ['24:00', '23:60', '99:99', '-1:00', '1:05', '01:5', '12:30:00', '12:30 PM']) {
    assert.equal(parse(`27/11/2026 ${time}`, 'datetime-local', 'en'), null, time);
  }
  // A local clock elsewhere may skip/repeat these times. The banking value is
  // still a valid UTC+5 wall time and must never use the computer's DST rules.
  assert.equal(parse('2026-03-08T02:30', 'datetime-local', 'en'), '2026-03-08T02:30');
  assert.equal(parse('2026-11-01T01:30', 'datetime-local', 'en'), '2026-11-01T01:30');
});

test('Wrong locale separators and partial dates are rejected instead of guessed', () => {
  for (const value of ['27.11.2026', '11/27/2026', '1/2/2026', '01/02/26',
    '2026-1-02', '2026-11', '27/11/2026junk', 'Tomorrow']) {
    assert.equal(parse(value, 'date', 'en'), null, value);
  }
  for (const language of ['ru', 'kk']) {
    for (const value of ['27/11/2026', '1.2.2026', '01.02.26', '2026-1-02']) {
      assert.equal(parse(value, 'date', language), null, `${language}: ${value}`);
    }
  }
});

test('Formatting and parsing round-trip exact dates and times in all languages', () => {
  const dates = ['0001-01-01', '0099-12-31', '2000-02-29', '2026-11-27', '9999-12-31'];
  for (const language of ['en', 'ru', 'kk']) {
    const separator = language === 'en' ? '/' : '.';
    for (const iso of dates) {
      const [year, month, day] = iso.split('-');
      const displayed = [day, month, year].join(separator);
      assert.equal(format(iso, 'date', language), displayed);
      assert.equal(parse(format(iso, 'date', language), 'date', language), iso);
      for (const time of ['00:00', '14:05', '23:59']) {
        assert.equal(format(`${iso}T${time}`, 'datetime-local', language), `${displayed} ${time}`);
        assert.equal(parse(format(`${iso}T${time}`, 'datetime-local', language), 'datetime-local', language), `${iso}T${time}`);
      }
    }
  }
});

test('Changing language reformats valid fields without changing their dates or times', () => {
  const date = {value:'03/04/2026', dataset:{dateKind:'date', dateLocale:'en'}};
  const scheduled = {value:'27/11/2026 14:05', dataset:{dateKind:'datetime-local', dateLocale:'en'}};
  for (const language of ['ru', 'kk', 'en']) {
    const placeholder = key => `${language}:${key}`;
    localize([date, scheduled], language, placeholder);
    const separator = language === 'en' ? '/' : '.';
    assert.equal(date.value, ['03', '04', '2026'].join(separator));
    assert.equal(scheduled.value, ['27', '11', '2026'].join(separator) + ' 14:05');
    assert.equal(parse(date.value, 'date', language), '2026-04-03');
    assert.equal(parse(scheduled.value, 'datetime-local', language), '2026-11-27T14:05');
    assert.equal(date.placeholder, `${language}:datePlaceholder`);
    assert.equal(scheduled.placeholder, `${language}:dateTimePlaceholder`);
    for (const input of [date, scheduled]) {
      assert.equal(input.lang, language);
      assert.equal(input.dataset.dateLocale, language);
    }
  }
});

test('Changing language preserves unfinished or impossible drafts and empty fields', () => {
  const inputs = ['27/11/', '31/04/2026', '2026-02-29', ''].map(value => ({
    value, dataset:{dateKind:'date', dateLocale:'en'},
  }));
  const before = inputs.map(input => input.value);
  for (const language of ['ru', 'kk', 'en']) {
    localize(inputs, language, key => `${language}:${key}`);
    assert.deepEqual(inputs.map(input => input.value), before);
    assert.ok(inputs.every(input => input.lang === language));
  }
});

test('Date/time results are independent of the operating-system timezone', () => {
  const cases = [
    ['2026-01-01', 'date', 'en'],
    ['2026-03-08T02:30', 'datetime-local', 'en'],
    ['2026-11-01T01:30', 'datetime-local', 'ru'],
    ['2026-12-31T23:59', 'datetime-local', 'kk'],
    ['0099-01-01T00:00', 'datetime-local', 'kk'],
  ];
  const expected = cases.map(([iso, kind, language]) => [iso, format(iso, kind, language)]);
  const probe = `
    const fs = require('node:fs'), vm = require('node:vm');
    const context = vm.createContext({});
    vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
    const {parse, format} = context.BankDates;
    const cases = JSON.parse(process.argv[2]);
    process.stdout.write(JSON.stringify(cases.map(([iso, kind, language]) => {
      const displayed = format(iso, kind, language);
      return [parse(displayed, kind, language), displayed];
    })));
  `;
  for (const zone of ['UTC', 'America/Los_Angeles', 'Pacific/Kiritimati', 'Asia/Qyzylorda']) {
    const child = spawnSync(process.execPath, ['-e', probe, modulePath, JSON.stringify(cases)], {
      encoding: 'utf8', env: { ...process.env, TZ: zone }, timeout: 10000,
    });
    assert.equal(child.status, 0, `${zone}: ${child.error || child.stderr}`);
    assert.deepEqual(JSON.parse(child.stdout), expected, zone);
  }
});
