/* The statistics use their own API, never the history panel's limited rows. */
// analytics-catalog:start
Object.assign(I18N.en, {
  statsTitle: 'Your transfers', statsScope: 'Successful outgoing transfers · All your accounts',
  statsWeek: 'Sent this week', statsMonth: 'Sent this month',
  statsWeekHint: 'Since Monday · UTC+5', statsMonthHint: 'Since the first day · UTC+5',
  statsCount: 'Transfers: {count}', statsLargest: 'Largest transfer', statsSmallest: 'Smallest transfer',
  statsFrequent: 'Most frequent recipients', statsMonthOnly: 'This month',
  statsFrequencyHint: 'By number of transfers; all tied leaders are shown.',
  statsNoTransfers: 'No successful outgoing transfers yet.', statsLoading: 'Loading statistics…',
  statsError: 'Could not refresh statistics. Try again; any figures shown are from the last update.',
  statsRefresh: 'Refresh statistics', statsDetails: 'View transfer', statsRecipientTotal: 'Sent: {amount}',
  statsUpdated: 'Updated: {time}', statsPrivacy: 'Only your completed outgoing transfers are included.'
});
Object.assign(I18N.ru, {
  statsTitle: 'Ваши переводы', statsScope: 'Успешные исходящие переводы · Все ваши счета',
  statsWeek: 'Отправлено за неделю', statsMonth: 'Отправлено за месяц',
  statsWeekHint: 'С понедельника · UTC+5', statsMonthHint: 'С первого числа · UTC+5',
  statsCount: 'Переводов: {count}', statsLargest: 'Самый большой перевод', statsSmallest: 'Самый маленький перевод',
  statsFrequent: 'Кому переводите чаще всего', statsMonthOnly: 'В этом месяце',
  statsFrequencyHint: 'По количеству переводов. При равенстве показаны все лидеры.',
  statsNoTransfers: 'Успешных исходящих переводов пока нет.', statsLoading: 'Загружаем статистику…',
  statsError: 'Не удалось обновить статистику. Попробуйте ещё раз; показанные суммы относятся к последнему обновлению.',
  statsRefresh: 'Обновить статистику', statsDetails: 'Посмотреть перевод', statsRecipientTotal: 'Отправлено: {amount}',
  statsUpdated: 'Обновлено: {time}', statsPrivacy: 'Учитываются только ваши завершённые исходящие переводы.'
});
Object.assign(I18N.kk, {
  statsTitle: 'Сіздің аударымдарыңыз', statsScope: 'Сәтті шығыс аударымдар · Барлық шоттарыңыз',
  statsWeek: 'Осы аптада жіберілді', statsMonth: 'Осы айда жіберілді',
  statsWeekHint: 'Дүйсенбіден бастап · UTC+5', statsMonthHint: 'Айдың бірінші күнінен бастап · UTC+5',
  statsCount: 'Аударым саны: {count}', statsLargest: 'Ең үлкен аударым', statsSmallest: 'Ең кіші аударым',
  statsFrequent: 'Ең жиі ақша жіберетін алушылар', statsMonthOnly: 'Осы айда',
  statsFrequencyHint: 'Аударым саны бойынша. Саны тең болса, барлық көшбасшы көрсетіледі.',
  statsNoTransfers: 'Әзірге сәтті шығыс аударымдар жоқ.', statsLoading: 'Статистика жүктелуде…',
  statsError: 'Статистиканы жаңарту мүмкін болмады. Қайталап көріңіз; көрсетілген сомалар соңғы жаңартудан алынған.',
  statsRefresh: 'Статистиканы жаңарту', statsDetails: 'Аударымды көру', statsRecipientTotal: 'Жіберілді: {amount}',
  statsUpdated: 'Жаңартылды: {time}', statsPrivacy: 'Тек сіздің аяқталған шығыс аударымдарыңыз есепке алынады.'
});
// analytics-catalog:end

(() => {
  const bank = document.getElementById('bankView');
  const panel = document.createElement('section');
  panel.id = 'analyticsPanel';
  panel.className = 'analytics-panel';
  panel.setAttribute('aria-labelledby', 'analyticsTitle');
  bank.querySelector('.greeting').after(panel);
  let data = null;
  let failed = false;
  let generation = 0;
  let pending = false;
  let controller = null;
  let owner = '';
  const visible = () => !bank.classList.contains('hidden');
  const identity = () => document.getElementById('userEmail').textContent;

  function highlight(transfer, title) {
    const content = transfer
      ? `<div class="stat-amount">${esc(money(transfer.amount))}</div>
         <div class="stat-person">${esc(transfer.recipient)}</div>
         <div class="stat-hint">${esc(date(transfer.completedAt))}</div>
         <button type="button" class="rowaction stat-link" data-stat-detail="${esc(transfer.requestId)}">${esc(t('statsDetails'))} ↗</button>`
      : `<p class="stat-empty">${esc(t(data ? 'statsNoTransfers' : 'statsLoading'))}</p>`;
    return `<article class="card stat-highlight"><div class="stat-label">${esc(title)}</div>${content}</article>`;
  }

  function render() {
    if (!visible()) return;
    const active = panel.contains(document.activeElement) ? document.activeElement : null;
    const focusedId = active?.id;
    const focusedTransfer = active?.dataset.statDetail;
    const scroll = panel.querySelector('.stat-leaders')?.scrollTop || 0;
    const month = data?.month;
    const monthName = data ? new Intl.DateTimeFormat(
      {en: 'en-GB', ru: 'ru-RU', kk: 'kk-KZ'}[document.documentElement.lang] || 'en-GB',
      {month: 'long', year: 'numeric', timeZone: 'Etc/GMT-5'}).format(new Date(data.asOf)) : t('statsMonthOnly');
    const summary = (period, label, hint, accent) => `<article class="card stat-total ${accent ? 'stat-total-accent' : ''}">
      <div><div class="stat-label">${esc(label)}</div><div class="stat-total-amount">${period ? esc(money(period.totalAmount)) : '—'}</div></div>
      <div class="stat-total-meta"><span>${period ? esc(t('statsCount', {count: period.count})) : esc(t('statsLoading'))}</span><span>${esc(hint)}</span></div>
    </article>`;
    panel.innerHTML = `<div class="analytics-heading"><div><h2 id="analyticsTitle">${esc(t('statsTitle'))}</h2><p>${esc(t('statsScope'))}</p></div>
      <button type="button" class="stat-refresh" id="refreshAnalytics" aria-label="${esc(t('statsRefresh'))}" title="${esc(t('statsRefresh'))}" ${pending ? 'disabled' : ''}>↻</button></div>
      ${failed ? `<div class="notice error show" role="status">${esc(t('statsError'))}</div>` : ''}
      <div class="stat-totals">${summary(data?.week, t('statsWeek'), t('statsWeekHint'), false)}${summary(month, t('statsMonth'), t('statsMonthHint'), true)}</div>
      <div class="stat-period"><span>${esc(monthName)}</span><span>${esc(t('statsMonthOnly'))}</span></div>
      <div class="stat-highlights">${highlight(month?.largest, t('statsLargest'))}${highlight(month?.smallest, t('statsSmallest'))}
        <article class="card stat-highlight stat-frequent"><div class="stat-label">${esc(t('statsFrequent'))}</div>
          <div class="stat-leaders">${month?.frequentRecipients.length ? month.frequentRecipients.map(recipient => `<div class="stat-leader">
            <span class="stat-initial" aria-hidden="true">${esc(recipient.name.slice(0, 1))}</span>
            <div class="stat-leader-name"><strong>${esc(recipient.name)}</strong><span>${esc(t('statsRecipientTotal', {amount: money(recipient.totalAmount)}))}</span></div>
            <span class="stat-count">${esc(t('statsCount', {count: recipient.count}))}</span></div>`).join('')
            : `<p class="stat-empty">${esc(data ? t('statsNoTransfers') : t('statsLoading'))}</p>`}</div>
          <p class="stat-hint">${esc(t('statsFrequencyHint'))}</p>
        </article>
      </div><div class="stats-foot"><span>${esc(t('statsPrivacy'))}</span>${data ? `<span>${esc(t('statsUpdated', {time: date(data.asOf)}))}</span>` : ''}</div>`;
    panel.querySelector('#refreshAnalytics').onclick = load;
    if (focusedId === 'refreshAnalytics') panel.querySelector('#refreshAnalytics').focus({preventScroll: true});
    if (focusedTransfer) {
      [...panel.querySelectorAll('[data-stat-detail]')].find(button => button.dataset.statDetail === focusedTransfer)
        ?.focus({preventScroll: true});
    }
    panel.querySelector('.stat-leaders').scrollTop = scroll;
  }

  function reset() {
    generation += 1;
    controller?.abort();
    controller = null;
    data = null;
    failed = false;
    pending = false;
    owner = '';
    panel.replaceChildren();
  }

  async function load() {
    if (!visible()) return;
    if (owner !== identity()) { reset(); owner = identity(); }
    if (pending) return;
    const requestGeneration = generation;
    const requestOwner = owner;
    controller = new AbortController();
    pending = true;
    if (!data) render();
    try {
      const result = await api('/api/analytics', {signal: controller.signal});
      if (requestGeneration !== generation || !visible() || identity() !== requestOwner) return;
      data = result;
      failed = false;
    } catch (error) {
      if (requestGeneration !== generation || !visible() || identity() !== requestOwner) return;
      if (error.name !== 'AbortError') failed = true;
    } finally {
      if (requestGeneration === generation) { pending = false; render(); }
    }
  }

  panel.addEventListener('click', event => {
    const button = event.target.closest('[data-stat-detail]');
    if (button) showDetails(button.dataset.statDetail);
  });
  document.addEventListener('languagechange', render);
  document.addEventListener('bankrefresh', load);
  document.addEventListener('bankready', load);
  document.addEventListener('banksessionchange', reset);
  new MutationObserver(() => { if (visible()) load(); else reset(); })
    .observe(bank, {attributes: true, attributeFilter: ['class']});
  // Also refresh after a calendar boundary or when a background tab becomes active.
  document.addEventListener('visibilitychange', () => { if (!document.hidden) load(); });
  setInterval(() => { if (!document.hidden && visible()) load(); }, 5000);
  if (visible()) load();
})();
