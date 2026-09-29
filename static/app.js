'use strict';

const state = {
  strings: {},
  status: null,
  departments: [],
  selectedDepts: new Set(),
  scope: 'available',
  keyword: '',
  sort: 'time_desc',
  onlyOpen: false,
  page: 1,
  pageSize: 20,
  result: null,
  auto: null,
  expiredToasted: false,
};

const DETAIL_FIELDS = [
  'BGBM', 'BGTMZW', 'BGTMYW', 'BGRZW', 'BGRJJ', 'BGRYW', 'BGNR', 'YXDM',
  'YXDM_DISPLAY', 'DD', 'BGSJ', 'JZSJ', 'KXRS', 'YXRS', 'BZ', 'ZYSC', 'SFKXK',
  'SFKTK',
];

const DETAIL_SKIP = [
  'BGRZP', 'HBSC', 'HBSYYS', 'RN', 'rowNumber', 'ROWNUM', 'ZPSJ', 'ZPSC',
];

const el = (id) => document.getElementById(id);

/* ------------------------------------------------------------------ helpers */

function t(key, vars) {
  let text = state.strings[key] || key;
  if (vars) {
    Object.keys(vars).forEach((name) => {
      text = text.replace(new RegExp('\\{' + name + '\\}', 'g'), vars[name]);
    });
  }
  return text;
}

function esc(value) {
  return String(value === null || value === undefined ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function formatTime(value) {
  if (!value) return '--';
  return String(value).replace(/\s00:00:00$/, '');
}

function toast(message, kind) {
  const node = document.createElement('div');
  node.className = 'toast' + (kind ? ' ' + kind : '');
  node.textContent = message;
  el('toasts').appendChild(node);
  setTimeout(() => node.remove(), 3600);
}

async function api(path, options) {
  const response = await fetch(path, Object.assign({ headers: {} }, options));
  let payload = null;
  try {
    payload = await response.json();
  } catch (error) {
    throw new Error('HTTP ' + response.status);
  }
  if (payload && payload.expired) {
    state.status = Object.assign({}, state.status, { connected: false });
    renderSession();
    toast(t('session.expired'), 'error');
  }
  if (!response.ok || !payload || payload.ok === false) {
    throw new Error((payload && payload.error) || 'HTTP ' + response.status);
  }
  return payload;
}

function postJson(path, body) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
}

/* -------------------------------------------------------------------- i18n */

function applyI18n() {
  document.querySelectorAll('[data-i18n]').forEach((node) => {
    node.textContent = t(node.getAttribute('data-i18n'));
  });
  document.querySelectorAll('[data-i18n-placeholder]').forEach((node) => {
    node.setAttribute('placeholder', t(node.getAttribute('data-i18n-placeholder')));
  });
  document.title = t('app.title');
}

/* ----------------------------------------------------------------- session */

function renderSession() {
  const status = state.status || {};
  const connected = !!status.connected;
  const expired = !!status.sessionExpired;

  el('sessionChip').classList.toggle('online', connected && !expired);
  el('sessionChip').classList.toggle('warn', expired);
  el('sessionChipText').textContent = expired
    ? t('session.expiredShort')
    : t(connected ? 'session.connected' : 'session.disconnected');

  // Keep the cookie box open so a fresh cookie can be pasted straight away.
  el('sessionBody').classList.toggle('collapsed', connected && !expired);
  el('connectBtn').disabled = false;
  el('refreshBtn').disabled = !connected || expired;
  renderCookieCloud(status);

  if (expired && !state.expiredToasted) {
    state.expiredToasted = true;
    toast(t('session.expiredHint'), 'error');
  } else if (!expired) {
    state.expiredToasted = false;
  }
}

function renderCookieCloud(status) {
  const cloud = (status && status.cookieCloud) || {};
  const configured = !!cloud.configured;
  el('cookiecloudBtn').disabled = !configured;
  el('cookiecloudNote').textContent = configured
    ? t('session.cloudReady', { url: cloud.endpoint || '' })
    : t('session.cloudOff');
}

function renderStats() {
  const status = state.status || {};
  el('statCredits').textContent = status.credits === null || status.credits === undefined ? '--' : status.credits;
  el('statAvailable').textContent = status.connected ? status.availableCount : '--';
  el('statSelected').textContent = status.connected ? status.selectedCount : '--';
  el('statDepts').textContent = status.deptCount === undefined ? '--' : status.deptCount;

  const note = el('cacheNote');
  if (!status.connected || !status.fetchedAt) {
    note.textContent = '';
  } else {
    const when = new Date(status.fetchedAt * 1000);
    note.textContent = when.toLocaleTimeString() + (status.stale ? ' (stale)' : '');
  }
}

async function connect() {
  const cookies = el('cookieInput').value.trim();
  if (!cookies) {
    toast(t('session.cookieRequired'), 'error');
    return;
  }
  const button = el('connectBtn');
  button.disabled = true;
  button.textContent = t('empty.loading');
  try {
    const payload = await postJson('/api/connect', { cookies: cookies });
    state.status = payload.status;
    el('cookieInput').value = '';
    toast(t('session.ok') + ' · ' + t('toast.loaded', { n: payload.status.availableCount }), 'success');
    renderSession();
    renderStats();
    await loadDepartments();
    await loadReports();
  } catch (error) {
    toast(t('toast.failed') + ': ' + error.message, 'error');
  } finally {
    button.disabled = false;
    button.textContent = t('session.connect');
  }
}

async function syncCookieCloud() {
  const button = el('cookiecloudBtn');
  button.disabled = true;
  button.textContent = t('empty.loading');
  try {
    const payload = await postJson('/api/cookiecloud/sync');
    state.status = payload.status;
    el('cookieInput').value = '';
    toast(
      t('session.cloudSynced') + ' · ' + t('toast.loaded', { n: payload.status.availableCount }),
      'success'
    );
    renderSession();
    renderStats();
    await loadDepartments();
    await loadReports();
  } catch (error) {
    toast(t('toast.failed') + ': ' + error.message, 'error');
  } finally {
    button.textContent = t('session.syncCloud');
    renderSession();
  }
}

async function disconnect() {
  try {
    const payload = await postJson('/api/disconnect');
    state.status = payload.status;
    state.result = null;
    renderSession();
    renderStats();
    renderGrid();
  } catch (error) {
    toast(t('toast.failed') + ': ' + error.message, 'error');
  }
}

async function refresh() {
  const button = el('refreshBtn');
  button.disabled = true;
  try {
    const payload = await postJson('/api/refresh');
    state.status = payload.status;
    toast(t('toast.refreshed') + ' · ' + t('toast.loaded', { n: payload.status.availableCount }), 'success');
    renderSession();
    renderStats();
    await loadDepartments();
    await loadReports();
  } catch (error) {
    toast(t('toast.failed') + ': ' + error.message, 'error');
  } finally {
    button.disabled = false;
  }
}

/* ------------------------------------------------------------- departments */

async function loadDepartments() {
  try {
    const payload = await api('/api/departments');
    state.departments = payload.departments || [];
  } catch (error) {
    state.departments = [];
  }
  renderDeptList();
}

function renderDeptList() {
  const list = el('deptList');
  list.innerHTML = '';
  if (!state.departments.length) {
    const empty = document.createElement('p');
    empty.className = 'hint';
    empty.textContent = t('empty.noSessionHint');
    list.appendChild(empty);
    return;
  }
  state.departments.forEach((dept) => {
    const label = document.createElement('label');
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.value = dept.code;
    input.checked = state.selectedDepts.has(dept.code);
    input.addEventListener('change', () => {
      if (input.checked) {
        state.selectedDepts.add(dept.code);
      } else {
        state.selectedDepts.delete(dept.code);
      }
      state.page = 1;
      renderDeptSummary();
      loadReports();
    });
    const name = document.createElement('span');
    name.textContent = dept.name;
    const count = document.createElement('span');
    count.className = 'dept-count';
    count.textContent = state.status && state.status.connected ? String(dept.available) : '';
    label.appendChild(input);
    label.appendChild(name);
    label.appendChild(count);
    list.appendChild(label);
  });
  renderDeptSummary();
}

function renderDeptSummary() {
  const count = state.selectedDepts.size;
  if (!count) {
    el('deptSummary').textContent = t('filter.deptAll');
  } else if (count === state.departments.length) {
    el('deptSummary').textContent = t('filter.deptAll');
  } else if (count === 1) {
    const only = state.departments.find((d) => state.selectedDepts.has(d.code));
    el('deptSummary').textContent = only ? only.name : String(count);
  } else {
    el('deptSummary').textContent = t('filter.dept') + ' · ' + count;
  }
}

/* ----------------------------------------------------------------- reports */

async function loadReports() {
  if (!state.status || !state.status.connected) {
    renderGrid();
    return;
  }
  const params = new URLSearchParams({
    scope: state.scope,
    keyword: state.keyword,
    depts: Array.from(state.selectedDepts).join(','),
    onlyOpen: state.onlyOpen ? '1' : '0',
    sort: state.sort,
    page: String(state.page),
    pageSize: String(state.pageSize),
  });
  el('emptyState').innerHTML = '<strong>' + esc(t('empty.loading')) + '</strong>';
  el('emptyState').classList.remove('hidden');
  try {
    const payload = await api('/api/reports?' + params.toString());
    state.result = payload.result;
    state.page = payload.result.page;
  } catch (error) {
    state.result = null;
    toast(t('toast.failed') + ': ' + error.message, 'error');
  }
  renderGrid();
}

function statusBadge(status) {
  return '<span class="badge ' + esc(status) + '">' + esc(t('status.' + status)) + '</span>';
}

function renderGrid() {
  const body = el('gridBody');
  const empty = el('emptyState');
  body.innerHTML = '';

  if (!state.status || !state.status.connected) {
    empty.innerHTML = '<strong>' + esc(t('empty.noSession')) + '</strong>' + esc(t('empty.noSessionHint'));
    empty.classList.remove('hidden');
    el('pageInfo').textContent = '';
    return;
  }

  const result = state.result;
  if (!result || !result.rows.length) {
    empty.innerHTML = '<strong>' + esc(t('empty.noData')) + '</strong>';
    empty.classList.remove('hidden');
    el('pageInfo').textContent = result ? t('table.count', { total: result.total, page: 1, pages: 1 }) : '';
    return;
  }

  empty.classList.add('hidden');

  result.rows.forEach((row) => {
    const tr = document.createElement('tr');

    const titleCell = document.createElement('td');
    titleCell.className = 'cell-title';
    const titleLink = document.createElement('a');
    titleLink.className = 'title-link';
    titleLink.textContent = row.title || row.bgbm;
    titleLink.addEventListener('click', () => openDetail(row.bgbm));
    titleCell.appendChild(titleLink);
    if (row.titleEn) {
      const en = document.createElement('span');
      en.className = 'title-en';
      en.textContent = row.titleEn;
      titleCell.appendChild(en);
    }

    const speakerCell = document.createElement('td');
    speakerCell.className = 'cell-nowrap';
    speakerCell.textContent = row.speaker || '--';

    const deptCell = document.createElement('td');
    deptCell.className = 'cell-dim';
    deptCell.textContent = row.deptName || row.deptCode;

    const timeCell = document.createElement('td');
    timeCell.className = 'cell-nowrap';
    timeCell.textContent = formatTime(row.time);

    const deadlineCell = document.createElement('td');
    deadlineCell.className = 'cell-nowrap cell-dim';
    deadlineCell.textContent = formatTime(row.deadline);

    const capacityCell = document.createElement('td');
    capacityCell.className = 'cell-nowrap';
    capacityCell.textContent = (row.capacity || '--') + ' / ' + (row.enrolled || '--');

    const statusCell = document.createElement('td');
    statusCell.innerHTML = statusBadge(row.status);

    const actionCell = document.createElement('td');
    const actions = document.createElement('div');
    actions.className = 'row-actions';

    const viewLink = document.createElement('a');
    viewLink.className = 'link';
    viewLink.textContent = t('action.view');
    viewLink.addEventListener('click', () => openDetail(row.bgbm));
    actions.appendChild(viewLink);

    if (state.scope === 'available') {
      const enrollLink = document.createElement('a');
      enrollLink.className = 'link' + (row.canEnroll ? '' : ' disabled');
      enrollLink.textContent = t('action.enroll');
      enrollLink.addEventListener('click', () => enroll(row));
      actions.appendChild(enrollLink);
    } else {
      const dropLink = document.createElement('a');
      dropLink.className = 'link danger' + (row.canDrop ? '' : ' disabled');
      dropLink.textContent = t('action.drop');
      dropLink.addEventListener('click', () => drop(row));
      actions.appendChild(dropLink);
    }

    actionCell.appendChild(actions);

    [titleCell, speakerCell, deptCell, timeCell, deadlineCell, capacityCell, statusCell, actionCell]
      .forEach((cell) => tr.appendChild(cell));
    body.appendChild(tr);
  });

  el('pageInfo').textContent = t('table.count', {
    total: result.total, page: result.page, pages: result.pages,
  });
  el('prevPage').disabled = result.page <= 1;
  el('nextPage').disabled = result.page >= result.pages;
}

/* ---------------------------------------------------------------- mutation */

async function enroll(row) {
  if (!row.canEnroll) return;
  if (!window.confirm(t('confirm.enroll') + '\n' + row.title)) return;
  try {
    const payload = await postJson('/api/enroll', { bgbm: row.bgbm });
    state.status = payload.status;
    toast(t('toast.enrolled') + ' · ' + row.title, 'success');
    renderStats();
    await loadDepartments();
    await loadReports();
  } catch (error) {
    toast(t('toast.failed') + ': ' + error.message, 'error');
  }
}

async function drop(row) {
  if (!row.canDrop) return;
  if (!window.confirm(t('confirm.drop') + '\n' + row.title)) return;
  try {
    const payload = await postJson('/api/drop', { bgbm: row.bgbm });
    state.status = payload.status;
    toast(t('toast.dropped') + ' · ' + row.title, 'success');
    renderStats();
    await loadDepartments();
    await loadReports();
  } catch (error) {
    toast(t('toast.failed') + ': ' + error.message, 'error');
  }
}

/* ------------------------------------------------------------------ detail */

function detailSection(title, inner) {
  const section = document.createElement('div');
  section.className = 'detail-section';
  const heading = document.createElement('h4');
  heading.textContent = title;
  section.appendChild(heading);
  if (typeof inner === 'string') {
    const box = document.createElement('div');
    box.className = 'detail-text';
    box.textContent = inner;
    section.appendChild(box);
  } else {
    section.appendChild(inner);
  }
  return section;
}

function detailGrid(pairs) {
  const grid = document.createElement('dl');
  grid.className = 'detail-grid';
  pairs.forEach((pair) => {
    if (!pair[1]) return;
    const dt = document.createElement('dt');
    dt.textContent = pair[0];
    const dd = document.createElement('dd');
    dd.textContent = pair[1];
    grid.appendChild(dt);
    grid.appendChild(dd);
  });
  return grid;
}

async function openDetail(bgbm) {
  const body = el('modalBody');
  body.innerHTML = '';
  const loading = document.createElement('p');
  loading.className = 'hint';
  loading.textContent = t('detail.loading');
  body.appendChild(loading);
  el('modal').classList.add('open');

  let report = null;
  try {
    const payload = await api('/api/report?bgbm=' + encodeURIComponent(bgbm));
    report = payload.report;
  } catch (error) {
    body.innerHTML = '';
    const failed = document.createElement('p');
    failed.className = 'hint';
    failed.textContent = t('toast.failed') + ': ' + error.message;
    body.appendChild(failed);
    return;
  }

  body.innerHTML = '';
  el('modalTitle').textContent = report.BGTMZW || t('detail.title');

  body.appendChild(detailSection(t('detail.basic'), detailGrid([
    [t('table.title'), report.BGTMZW],
    [t('table.title') + ' (EN)', report.BGTMYW],
    [t('table.speaker'), report.BGRZW],
    [t('table.dept'), report.YXDM_DISPLAY || report.YXDM],
    [t('table.location'), report.DD],
    [t('table.time'), formatTime(report.BGSJ)],
    [t('table.deadline'), formatTime(report.JZSJ)],
    [t('table.capacity'), (report.KXRS || '--') + ' / ' + (report.YXRS || '--')],
  ])));

  body.appendChild(detailSection(t('detail.content'), report.BGNR || t('detail.none')));

  if (report.BGRYW) {
    body.appendChild(detailSection(t('detail.bio') + ' (EN)', report.BGRYW));
  }

  if (report.BGRJJ) {
    body.appendChild(detailSection(t('detail.bio'), report.BGRJJ));
  }

  const extras = Object.keys(report).filter((key) => {
    return DETAIL_FIELDS.indexOf(key) === -1 && DETAIL_SKIP.indexOf(key) === -1
      && report[key] !== null && report[key] !== '' && typeof report[key] !== 'object';
  });
  if (extras.length) {
    body.appendChild(detailSection(t('detail.other'), detailGrid(
      extras.map((key) => [key, String(report[key])])
    )));
  }
}

/* -------------------------------------------------------------- auto enroll */

function formatClock(seconds) {
  if (!seconds) return '--';
  const when = new Date(seconds * 1000);
  return when.toLocaleString();
}

function renderAuto(auto) {
  if (!auto) return;
  state.auto = auto;
  el('autoEnabled').checked = !!auto.enabled;
  if (document.activeElement !== el('autoTarget')) el('autoTarget').value = auto.target;
  if (document.activeElement !== el('autoInterval')) el('autoInterval').value = auto.intervalMinutes;

  const parts = [];
  parts.push(t(auto.enabled ? 'auto.stateOn' : 'auto.stateOff'));
  parts.push(t('auto.holding') + ': ' + (state.status && state.status.connected
    ? state.status.selectedCount + '/' + auto.target
    : '--'));
  if (auto.enabled) parts.push(t('auto.nextRun') + ': ' + formatClock(auto.nextRun));
  if (auto.lastRun) parts.push(t('auto.lastRun') + ': ' + formatClock(auto.lastRun));
  if (auto.running) parts.push(t('auto.running'));
  el('autoStatus').textContent = parts.join(' · ');

  const log = el('autoLog');
  log.innerHTML = '';
  const entries = auto.entries || [];
  if (!entries.length) {
    const empty = document.createElement('p');
    empty.className = 'hint';
    empty.textContent = t('auto.noLog');
    log.appendChild(empty);
    return;
  }
  entries.slice().reverse().forEach((entry) => {
    const row = document.createElement('div');
    row.className = 'auto-entry ' + esc(entry.kind);
    const time = document.createElement('span');
    time.className = 'auto-time';
    time.textContent = new Date(entry.at * 1000).toLocaleTimeString();
    const kind = document.createElement('span');
    kind.className = 'auto-kind';
    kind.textContent = t('auto.kind.' + entry.kind);
    const text = document.createElement('span');
    text.className = 'auto-text';
    text.textContent = entry.message;
    row.appendChild(time);
    row.appendChild(kind);
    row.appendChild(text);
    log.appendChild(row);
  });
}

async function loadAuto() {
  try {
    const payload = await api('/api/auto');
    renderAuto(payload.auto);
  } catch (error) {
    /* the panel simply stays empty when the worker cannot be reached */
  }
}

async function saveAuto(patch) {
  try {
    const payload = await postJson('/api/auto', patch);
    renderAuto(payload.auto);
    return true;
  } catch (error) {
    toast(t('toast.failed') + ': ' + error.message, 'error');
    return false;
  }
}

async function toggleAuto() {
  const wanted = el('autoEnabled').checked;
  if (wanted && !window.confirm(t('auto.confirmEnable'))) {
    el('autoEnabled').checked = false;
    return;
  }
  const ok = await saveAuto({ enabled: wanted });
  if (!ok) el('autoEnabled').checked = !wanted;
  toast(t(wanted ? 'auto.armed' : 'auto.paused'), wanted ? 'success' : undefined);
}

async function runAutoNow() {
  const button = el('autoRun');
  button.disabled = true;
  button.textContent = t('empty.loading');
  try {
    const payload = await postJson('/api/auto/run');
    const result = payload.result || {};
    renderAuto(payload.auto);
    if (payload.status) state.status = payload.status;
    renderStats();
    if (result.reason) {
      toast(t('auto.passIdle') + ': ' + result.reason);
    } else {
      toast(t('auto.passDone', { n: result.enrolled || 0 }), 'success');
    }
    await loadReports();
  } catch (error) {
    toast(t('toast.failed') + ': ' + error.message, 'error');
  } finally {
    button.disabled = false;
    button.textContent = t('auto.runNow');
  }
}

/* -------------------------------------------------------------------- init */

function bindEvents() {
  el('connectBtn').addEventListener('click', connect);
  el('disconnectBtn').addEventListener('click', disconnect);
  el('cookiecloudBtn').addEventListener('click', syncCookieCloud);
  el('refreshBtn').addEventListener('click', refresh);
  el('toggleSession').addEventListener('click', () => {
    el('sessionBody').classList.toggle('collapsed');
  });

  el('tabs').addEventListener('click', (event) => {
    const tab = event.target.closest('.tab');
    if (!tab) return;
    state.scope = tab.getAttribute('data-scope');
    state.page = 1;
    document.querySelectorAll('.tab').forEach((node) => node.classList.toggle('active', node === tab));
    loadReports();
  });

  let keywordTimer = null;
  el('keyword').addEventListener('input', (event) => {
    state.keyword = event.target.value;
    state.page = 1;
    clearTimeout(keywordTimer);
    keywordTimer = setTimeout(loadReports, 260);
  });

  el('sort').addEventListener('change', (event) => {
    state.sort = event.target.value;
    state.page = 1;
    loadReports();
  });

  el('onlyOpen').addEventListener('change', (event) => {
    state.onlyOpen = event.target.checked;
    state.page = 1;
    loadReports();
  });

  el('deptAll').addEventListener('click', () => {
    state.selectedDepts = new Set(state.departments.map((d) => d.code));
    state.page = 1;
    renderDeptList();
    loadReports();
  });

  el('deptNone').addEventListener('click', () => {
    state.selectedDepts.clear();
    state.page = 1;
    renderDeptList();
    loadReports();
  });

  el('prevPage').addEventListener('click', () => {
    if (state.page > 1) { state.page -= 1; loadReports(); }
  });

  el('nextPage').addEventListener('click', () => {
    if (state.result && state.page < state.result.pages) { state.page += 1; loadReports(); }
  });

  el('autoEnabled').addEventListener('change', toggleAuto);

  el('autoTarget').addEventListener('change', (event) => {
    const value = Math.max(1, parseInt(event.target.value, 10) || 1);
    event.target.value = value;
    saveAuto({ target: value });
  });

  el('autoInterval').addEventListener('change', (event) => {
    const value = Math.min(1440, Math.max(1, parseInt(event.target.value, 10) || 60));
    event.target.value = value;
    saveAuto({ intervalMinutes: value });
  });

  el('autoRun').addEventListener('click', runAutoNow);

  el('autoClear').addEventListener('click', async () => {
    try {
      const payload = await postJson('/api/auto/log/clear');
      renderAuto(payload.auto);
    } catch (error) {
      toast(t('toast.failed') + ': ' + error.message, 'error');
    }
  });

  el('modalClose').addEventListener('click', () => el('modal').classList.remove('open'));
  el('modal').addEventListener('click', (event) => {
    if (event.target === el('modal')) el('modal').classList.remove('open');
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') el('modal').classList.remove('open');
  });
  document.addEventListener('click', (event) => {
    const details = el('deptSelect');
    if (details.open && !details.contains(event.target)) details.open = false;
  });
}

let lastSelectedCount = null;

async function pollAuto() {
  try {
    const payload = await api('/api/status');
    state.status = payload.status;
  } catch (error) {
    return;
  }
  renderStats();
  await loadAuto();
  // The worker may have enrolled something while this page sat open.
  if (lastSelectedCount !== null && lastSelectedCount !== state.status.selectedCount) {
    await loadDepartments();
    await loadReports();
  }
  lastSelectedCount = state.status.selectedCount;
}

async function init() {
  try {
    const payload = await api('/api/i18n');
    state.strings = payload.strings || {};
  } catch (error) {
    state.strings = {};
  }
  applyI18n();
  bindEvents();

  try {
    const payload = await api('/api/status');
    state.status = payload.status;
  } catch (error) {
    state.status = { connected: false };
  }
  renderSession();
  renderStats();
  await loadAuto();
  lastSelectedCount = state.status ? state.status.selectedCount : null;

  if (state.status && state.status.connected) {
    await loadDepartments();
    await loadReports();
  } else {
    renderGrid();
  }

  setInterval(pollAuto, 30000);
}

init();