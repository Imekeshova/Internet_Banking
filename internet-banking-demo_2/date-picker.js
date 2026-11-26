/* Localized date entry and calendar, independent of the browser's UI language. */
(() => {
  'use strict';
  const pad = value => String(value).padStart(2, '0');
  const isoDay = date => `${String(date.getUTCFullYear()).padStart(4, '0')}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}`;
  const utc = (year, month, day) => {
    const date = new Date(0);
    date.setUTCFullYear(year, month - 1, day);
    date.setUTCHours(0, 0, 0, 0);
    return date;
  };
  const fromDay = value => utc(...value.split('-').map(Number));
  const escape = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;').replaceAll('"', '&quot;');

  function parse(value, kind, language) {
    if (typeof value !== 'string' || !['date', 'datetime-local'].includes(kind)) return null;
    const text = value.trim();
    if (!text) return '';
    const time = kind === 'datetime-local';
    let match = text.match(time ? /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/ : /^(\d{4})-(\d{2})-(\d{2})$/);
    let year, month, day, hour = 0, minute = 0;
    if (match) [, year, month, day, hour = 0, minute = 0] = match;
    else {
      const separator = language === 'en' ? '/' : '\\.';
      match = text.match(new RegExp(`^(\\d{2})${separator}(\\d{2})${separator}(\\d{4})${time ? ' (\\d{2}):(\\d{2})' : ''}$`));
      if (!match) return null;
      [, day, month, year, hour = 0, minute = 0] = match;
    }
    [year, month, day, hour, minute] = [year, month, day, hour, minute].map(Number);
    if (year < 1 || year > 9999 || month < 1 || month > 12 || day < 1 || day > 31 || hour > 23 || minute > 59) return null;
    const date = utc(year, month, day);
    if (date.getUTCFullYear() !== year || date.getUTCMonth() + 1 !== month || date.getUTCDate() !== day) return null;
    return isoDay(date) + (time ? `T${pad(hour)}:${pad(minute)}` : '');
  }

  function format(value, kind, language) {
    const canonical = parse(value, kind, 'en');
    if (!canonical) return '';
    const [year, month, day] = canonical.slice(0, 10).split('-');
    const separator = language === 'en' ? '/' : '.';
    return [day, month, year].join(separator) + (kind === 'datetime-local' ? ` ${canonical.slice(11)}` : '');
  }

  function localize(inputs, language, t) {
    for (const input of inputs) {
      const kind = input.dataset.dateKind;
      const canonical = parse(input.value, kind, input.dataset.dateLocale || language);
      if (canonical !== null) input.value = format(canonical, kind, language);
      input.dataset.dateLocale = language;
      input.placeholder = t(kind === 'datetime-local' ? 'dateTimePlaceholder' : 'datePlaceholder');
      input.lang = language;
    }
  }

  let instance = 0;
  function mount({inputs, getLanguage, t, today}) {
    const id = `bankDateDialog${++instance}`;
    const dialog = document.createElement('dialog');
    dialog.id = id;
    dialog.className = 'bd-dialog';
    dialog.setAttribute('aria-labelledby', `${id}Title`);
    document.body.append(dialog);
    const controls = [];
    let active = null, selected = '', view = '', hour = '12', minute = '00', returnFocus = null;
    const bound = name => active ? parse(active.input[name] || '', active.input.dataset.dateKind, 'en') : '';
    const allowedDay = day => (!bound('min') || day >= bound('min').slice(0, 10)) && (!bound('max') || day <= bound('max').slice(0, 10));
    const clampDay = day => {
      if (bound('min') && day < bound('min').slice(0, 10)) return bound('min').slice(0, 10);
      if (bound('max') && day > bound('max').slice(0, 10)) return bound('max').slice(0, 10);
      return day;
    };
    const button = (action, label, content, extra = '') => `<button type="button" data-action="${action}" aria-label="${escape(label)}" title="${escape(label)}" ${extra}>${content}</button>`;
    const options = (count, chosen) => Array.from({length:count}, (_, value) => `<option value="${pad(value)}"${Number(chosen) === value ? ' selected' : ''}>${pad(value)}</option>`).join('');

    function close() {
      if (dialog.open) dialog.close();
    }
    dialog.addEventListener('close', () => {
      if (active) active.button.setAttribute('aria-expanded', 'false');
      active = null;
      if (returnFocus?.isConnected && !returnFocus.disabled) returnFocus.focus();
    });
    dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
    dialog.addEventListener('keydown', event => {
      if (event.key === 'Escape') { event.preventDefault(); close(); }
    });
    dialog.addEventListener('click', event => { if (event.target === dialog) close(); });

    function commit(value) {
      if (!active || active.input.disabled) { close(); return; }
      const input = active.input;
      input.value = format(value, input.dataset.dateKind, getLanguage());
      input.dataset.dateLocale = getLanguage();
      close();
      input.dispatchEvent(new Event('input', {bubbles:true}));
      input.dispatchEvent(new Event('change', {bubbles:true}));
    }

    function moveMonth(delta, focus = '[data-month]') {
      const date = fromDay(view + '-01');
      date.setUTCMonth(date.getUTCMonth() + delta);
      if (date.getUTCFullYear() < 1 || date.getUTCFullYear() > 9999) return;
      view = isoDay(date).slice(0, 7);
      render(focus);
    }

    function render(focus = '') {
      if (!active) return;
      const months = t('calendarMonths').split('|'), weekdays = t('calendarWeekdays').split('|');
      const [year, month] = view.split('-').map(Number);
      const first = utc(year, month, 1);
      const start = new Date(first);
      start.setUTCDate(1 - (first.getUTCDay() + 6) % 7);
      let days = '';
      for (let week = 0; week < 6; week++) {
        days += '<div class="bd-week" role="row">';
        for (let day = 0; day < 7; day++) {
          const current = new Date(start);
          current.setUTCDate(start.getUTCDate() + week * 7 + day);
          if (current.getUTCFullYear() < 1 || current.getUTCFullYear() > 9999) { days += '<span role="gridcell"></span>'; continue; }
          const iso = isoDay(current), picked = iso === selected;
          const label = `${current.getUTCDate()} ${months[current.getUTCMonth()]} ${current.getUTCFullYear()}`;
          days += `<button type="button" role="gridcell" data-day="${iso}" class="bd-day${current.getUTCMonth() + 1 !== month ? ' bd-outside' : ''}" aria-label="${escape(label)}" aria-selected="${picked}"${iso === today() ? ' aria-current="date"' : ''} tabindex="${picked ? '0' : '-1'}"${allowedDay(iso) ? '' : ' disabled'}>${current.getUTCDate()}</button>`;
        }
        days += '</div>';
      }
      const withTime = active.input.dataset.dateKind === 'datetime-local';
      dialog.lang = getLanguage();
      dialog.innerHTML = `<div class="bd-content"><div class="bd-heading"><h2 id="${id}Title">${escape(t('calendarTitle'))}</h2>${button('close', t('close'), '×', 'class="bd-icon"')}</div>
        <div class="bd-navigation">${button('previous', t('calendarPrevious'), '‹', `class="bd-icon"${view === '0001-01' ? ' disabled' : ''}`)}
          <label class="bd-month-label"><span>${escape(t('calendarMonth'))}</span><select data-month aria-label="${escape(t('calendarMonth'))}">${months.map((name, index) => `<option value="${index + 1}"${index + 1 === month ? ' selected' : ''}>${escape(name)}</option>`).join('')}</select></label>
          <label class="bd-year-label"><span>${escape(t('calendarYear'))}</span><input data-year type="text" inputmode="numeric" maxlength="4" value="${year}" aria-label="${escape(t('calendarYear'))}"></label>
          ${button('next', t('calendarNext'), '›', `class="bd-icon"${view === '9999-12' ? ' disabled' : ''}`)}</div>
        <div class="bd-calendar" role="grid" aria-label="${escape(months[month - 1])} ${year}"><div class="bd-weekdays" role="row">${weekdays.map(name => `<span role="columnheader">${escape(name)}</span>`).join('')}</div>${days}</div>
        ${withTime ? `<fieldset class="bd-time"><legend>${escape(t('calendarTime'))}</legend><label><span>${escape(t('calendarHour'))}</span><select data-hour aria-label="${escape(t('calendarHour'))}">${options(24, hour)}</select></label><span aria-hidden="true">:</span><label><span>${escape(t('calendarMinute'))}</span><select data-minute aria-label="${escape(t('calendarMinute'))}">${options(60, minute)}</select></label></fieldset>` : ''}
        <div class="bd-actions">${button('today', t('calendarToday'), escape(t('calendarToday')), allowedDay(today()) ? '' : 'disabled')}${button('clear', t('calendarClear'), escape(t('calendarClear')))}${button('apply', t('calendarApply'), escape(t('calendarApply')), 'class="bd-apply"')}</div></div>`;
      dialog.querySelector('[data-action="close"]').onclick = close;
      dialog.querySelector('[data-action="previous"]').onclick = () => moveMonth(-1, '[data-action="previous"]');
      dialog.querySelector('[data-action="next"]').onclick = () => moveMonth(1, '[data-action="next"]');
      dialog.querySelector('[data-action="clear"]').onclick = () => commit('');
      dialog.querySelector('[data-action="apply"]').onclick = () => commit(selected + (withTime ? `T${hour}:${minute}` : ''));
      dialog.querySelector('[data-action="today"]').onclick = () => { selected = today(); view = selected.slice(0, 7); render(`[data-day="${selected}"]`); };
      dialog.querySelector('[data-month]').onchange = event => { view = `${String(year).padStart(4, '0')}-${pad(event.target.value)}`; render('[data-month]'); };
      dialog.querySelector('[data-year]').onchange = event => {
        const next = event.target.value;
        if (/^\d{1,4}$/.test(next) && Number(next) >= 1) view = `${next.padStart(4, '0')}-${pad(month)}`;
        render('[data-year]');
      };
      if (withTime) {
        dialog.querySelector('[data-hour]').onchange = event => { hour = event.target.value; };
        dialog.querySelector('[data-minute]').onchange = event => { minute = event.target.value; };
      }
      for (const day of dialog.querySelectorAll('[data-day]')) {
        day.onclick = () => { selected = day.dataset.day; view = selected.slice(0, 7); render(`[data-day="${selected}"]`); };
        day.onkeydown = event => {
          const delta = {ArrowLeft:-1, ArrowRight:1, ArrowUp:-7, ArrowDown:7}[event.key];
          if (delta === undefined) return;
          event.preventDefault();
          const next = fromDay(day.dataset.day);
          next.setUTCDate(next.getUTCDate() + delta);
          if (next.getUTCFullYear() < 1 || next.getUTCFullYear() > 9999) return;
          selected = clampDay(isoDay(next)); view = selected.slice(0, 7);
          render(`[data-day="${selected}"]`);
        };
      }
      const firstAllowed = dialog.querySelector('[data-day]:not([disabled])');
      if (!dialog.querySelector('[data-day][tabindex="0"]') && firstAllowed) firstAllowed.tabIndex = 0;
      if (focus) dialog.querySelector(focus)?.focus();
    }

    function open(control) {
      if (control.input.disabled) return;
      active = control;
      returnFocus = document.activeElement;
      const input = control.input, kind = input.dataset.dateKind;
      const current = parse(input.value, kind, input.dataset.dateLocale || getLanguage());
      selected = clampDay(current ? current.slice(0, 10) : today());
      view = selected.slice(0, 7);
      [hour, minute] = (current?.slice(11) || bound('min')?.slice(11) || '12:00').split(':');
      render();
      control.button.setAttribute('aria-expanded', 'true');
      dialog.showModal();
      (dialog.querySelector(`[data-day="${selected}"]:not([disabled])`) || dialog.querySelector('[data-month]')).focus();
    }

    for (const input of inputs) {
      const wrapper = document.createElement('div'), opener = document.createElement('button');
      wrapper.className = 'bank-date-control';
      input.before(wrapper); wrapper.append(input, opener);
      input.type = 'text'; input.autocomplete = 'off';
      opener.type = 'button'; opener.className = 'bank-date-open';
      opener.innerHTML = '<svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="5" width="18" height="16" rx="3"/><path d="M7 3v4M17 3v4M3 10h18M7 14h3M14 14h3M7 17h3"/></svg>';
      opener.setAttribute('aria-haspopup', 'dialog'); opener.setAttribute('aria-controls', id); opener.setAttribute('aria-expanded', 'false');
      const control = {input, button:opener}; controls.push(control);
      opener.onclick = () => open(control);
      input.addEventListener('input', () => { input.dataset.dateLocale = getLanguage(); });
      input.addEventListener('change', () => localize([input], getLanguage(), t));
      input.addEventListener('keydown', event => { if (event.key === 'ArrowDown' && event.altKey) { event.preventDefault(); open(control); } });
    }
    function refresh() {
      localize(inputs, getLanguage(), t);
      for (const {input, button:opener} of controls) {
        opener.disabled = input.disabled;
        opener.title = t('calendarOpen'); opener.setAttribute('aria-label', t('calendarOpen'));
      }
      if (dialog.open) { if (active?.input.disabled) close(); else render(); }
    }
    refresh();
    return {refresh};
  }
  globalThis.BankDates = {parse, format, localize, mount};
})();
