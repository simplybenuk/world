'use strict';

// Only records and authentication persist. Every view and conversation below lives in memory.
const state = {
  world: null, records: [], actor: null, health: null, view: null, exploring: false,
  conversation: [], response: '', proposals: [], originalRequest: '', busy: false,
  activityTab: 'changes', events: [], eventsLoading: false, selectedRecord: null,
  recordTab: 'fields', formMode: null, notificationTimer: null, sessionEpoch: 0,
};
const $ = (id) => document.getElementById(id);
let authenticationQueue = Promise.resolve();
const examples = [
  ['Show me what needs attention.', 'Find a useful starting point', '◎'],
  ['Make this more visual.', 'See the same records another way', '▤'],
  ['Show the website project instead.', 'Focus on a different part of the world', '◇'],
  ['Mark the documentation task as complete.', 'Propose a change to a shared record', '✓'],
];
const badgeColors = [
  ['#f0eee5', '#756a48'], ['#eaf0f2', '#4e727d'], ['#eaf0e4', '#577440'],
  ['#f0eaf2', '#765a80'], ['#f6ece5', '#8a694c'], ['#eceeee', '#5d7268'],
];

function el(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}
function button(text, className, action, label) {
  const element = el('button', className, text);
  element.type = 'button';
  if (label) element.setAttribute('aria-label', label);
  if (action) element.addEventListener('click', action);
  return element;
}
function labelFor(name, descriptor) {
  return descriptor?.label || String(name).replace(/[_-]/g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2').replace(/^./, (c) => c.toUpperCase());
}
function typeLabel(type) { return state.world?.types?.[type]?.label || labelFor(type); }
function typePlural(type) { return state.world?.types?.[type]?.plural || typeLabel(type).replace(/([^aeiou])y$/i, '$1ie').replace(/([^s])$/i, '$1s'); }
function fieldsFor(type) { return state.world?.types?.[type]?.fields || {}; }
function recordKey(record) { return `${record.type}/${record.id}`; }
function getRecord(type, id) { return state.records.find((record) => record.type === type && record.id === id); }
function kindOf(descriptor) { return String(descriptor?.type || 'text').toLowerCase(); }
function enumValues(descriptor) { return (descriptor?.values || descriptor?.options || []).map((value) => typeof value === 'object' ? value.value : value); }
function refType(descriptor) { return descriptor?.target || descriptor?.record_type || descriptor?.reference || descriptor?.of || null; }
function isReference(descriptor) { return ['reference', 'ref'].includes(kindOf(descriptor)); }
function isList(descriptor) { return ['list', 'refs', 'array'].includes(kindOf(descriptor)); }
function listDescriptor(descriptor) { return typeof descriptor?.items === 'object' ? descriptor.items : { type: descriptor?.target ? 'reference' : 'text', target: descriptor?.target }; }
function primaryField(type) {
  const fields = fieldsFor(type);
  return ['name', 'title', 'label'].find((name) => name in fields) || Object.keys(fields).find((name) => kindOf(fields[name]) === 'text' && fields[name].required) || Object.keys(fields).find((name) => kindOf(fields[name]) === 'text') || Object.keys(fields)[0];
}
function recordLabel(record) {
  if (!record) return 'Unknown record';
  const value = record.data?.[primaryField(record.type)];
  return value === undefined || value === null || value === '' ? `${typeLabel(record.type)} · ${record.id}` : String(value);
}
function abbreviated(text) {
  return String(text).trim().split(/\s+/).filter(Boolean).slice(0, 2).map((word) => word[0]).join('').toUpperCase() || '?';
}
function dateText(value, time = false) {
  if (!value) return '';
  const date = new Date(/^\d{4}-\d{2}-\d{2}$/.test(String(value)) ? `${value}T12:00:00` : value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString(undefined, { month: 'short', day: 'numeric', ...(time ? { hour: 'numeric', minute: '2-digit' } : { year: 'numeric' }) });
}
function plainValue(value, descriptor) {
  if (value === undefined || value === null || value === '') return 'No value';
  if (isList(descriptor)) return Array.isArray(value) ? value.map((item) => plainValue(item, listDescriptor(descriptor))).join(', ') || 'None' : String(value);
  if (isReference(descriptor)) return recordLabel(getRecord(refType(descriptor), String(value))) === 'Unknown record' ? String(value) : recordLabel(getRecord(refType(descriptor), String(value)));
  if (kindOf(descriptor) === 'date') return dateText(value);
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}
function applyBadgeColor(element, value, descriptor) {
  const values = enumValues(descriptor);
  let index = values.indexOf(value);
  if (index < 0) index = String(value).split('').reduce((sum, char) => sum + char.charCodeAt(0), 0);
  const [bg, ink] = badgeColors[index % badgeColors.length];
  element.style.setProperty('--badge-bg', bg);
  element.style.setProperty('--badge-ink', ink);
}
function valueElement(value, descriptor, links = false) {
  if (value === undefined || value === null || value === '') return el('span', 'null-value', 'No value');
  if (isList(descriptor) && Array.isArray(value)) {
    if (!value.length) return el('span', 'null-value', 'None');
    const list = el('span');
    value.forEach((item, index) => {
      if (index && !links) list.append(document.createTextNode(', '));
      list.append(valueElement(item, listDescriptor(descriptor), links));
    });
    return list;
  }
  if (isReference(descriptor) && links) {
    const target = getRecord(refType(descriptor), String(value));
    return target ? button(recordLabel(target), 'reference-button', () => openRecord(target)) : el('span', 'text-muted', String(value));
  }
  if (kindOf(descriptor) === 'enum') {
    const badge = el('span', 'enum-badge', labelFor(value));
    applyBadgeColor(badge, value, descriptor);
    return badge;
  }
  return el('span', '', plainValue(value, descriptor));
}

class ApiError extends Error {
  constructor(message, status, details) { super(message); this.status = status; this.details = details; }
}
async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: 'same-origin', ...options,
    headers: { 'Accept': 'application/json', ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers },
    ...(options.body && typeof options.body !== 'string' ? { body: JSON.stringify(options.body) } : {}),
  });
  let result;
  try { result = await response.json(); } catch { result = {}; }
  if (!response.ok) {
    const error = result.error;
    throw new ApiError(typeof error === 'string' ? error : error?.message || result.message || `The server returned ${response.status}.`, response.status, error?.details);
  }
  return result;
}
function notify(message, error = false, retry) {
  clearTimeout(state.notificationTimer);
  const container = $('notification');
  container.replaceChildren(el('span', 'notification-copy', message));
  container.classList.toggle('is-error', error);
  container.setAttribute('role', error ? 'alert' : 'status');
  if (retry) container.append(button('Retry', 'button', retry));
  container.append(button('×', 'icon-button', () => { container.hidden = true; }, 'Dismiss notification'));
  container.hidden = false;
  if (!error) state.notificationTimer = setTimeout(() => { container.hidden = true; }, 9000);
}
function handleError(error, retry) {
  if (error.status === 401) {
    showLogin('Your session ended. Sign in to continue.');
    return;
  }
  notify(error.message || 'Something went wrong. Try again.', true, retry);
}
function resetSession() {
  state.sessionEpoch += 1;
  state.world = null; state.records = []; state.actor = null; state.view = null;
  state.conversation = []; state.proposals = []; state.response = ''; state.exploring = false;
  state.originalRequest = ''; state.events = []; state.selectedRecord = null;
  state.activityTab = 'changes'; state.busy = false; state.eventsLoading = false;
  $('intent-input').value = '';
  $('record-dialog').close(); $('schema-dialog').close();
  ['record-dialog-content', 'schema-content', 'view-content', 'view-actions', 'view-meta', 'proposals', 'type-navigation', 'activity-content', 'assistant-response'].forEach((id) => $(id).replaceChildren());
  closeActivity(); closeNavigation();
  clearTimeout(state.notificationTimer);
  $('notification').hidden = true;
}
function showLogin(message = '') {
  resetSession();
  $('boot-screen').hidden = true; $('app').hidden = true; $('login-screen').hidden = false;
  $('login-error').textContent = message; $('login-error').hidden = !message;
  $('login-button').disabled = false;
}
async function refreshRecords() {
  const epoch = state.sessionEpoch;
  const [world, data] = await Promise.all([api('/api/world'), api('/api/records')]);
  if (epoch !== state.sessionEpoch) return;
  state.world = world;
  state.records = data.records || [];
  state.actor = world.actor || state.actor;
  if (state.view && state.exploring) state.view.record_ids = state.records.filter((record) => record.type === state.view.type).map((record) => record.id);
  renderNavigation(); renderView();
}
async function loadHealth() {
  try { state.health = await api('/health'); } catch { state.health = { status: 'unavailable' }; }
  const ready = ['ok', 'healthy', 'ready'].includes(state.health?.status) && state.health?.provider !== 'unavailable' && state.health?.provider_available !== false;
  $('connection-status').classList.toggle('unavailable', !ready);
  $('connection-status').replaceChildren(el('span', 'status-dot'), el('span', '', ready ? 'World connected' : 'Check connection'));
  $('connection-status').title = state.health?.provider ? `Intent provider: ${state.health.provider}` : 'Server connection status';
}
async function startSession(actor, epoch = state.sessionEpoch) {
  if (epoch !== state.sessionEpoch) return;
  state.actor = actor;
  await refreshRecords();
  if (epoch !== state.sessionEpoch) return;
  $('boot-screen').hidden = true; $('login-screen').hidden = true; $('app').hidden = false;
  renderResponse();
  loadHealth();
}
async function login(token) {
  resetSession();
  const epoch = state.sessionEpoch;
  $('boot-screen').hidden = false; $('app').hidden = true; $('login-screen').hidden = true;
  $('login-button').disabled = true; $('login-error').hidden = true; $('access-token').value = '';
  // Serialize cookie changes so the most recent sign-in also owns the server session.
  return enqueueAuthentication(async () => {
    if (epoch !== state.sessionEpoch) return;
    try {
      await api('/api/session', { method: 'DELETE' });
      if (epoch !== state.sessionEpoch) return;
      const result = await api('/api/session', { method: 'POST', body: { token } });
      if (epoch !== state.sessionEpoch) return;
      await startSession(result.actor, epoch);
    } catch (error) {
      if (epoch === state.sessionEpoch) showLogin(error.message || 'Could not sign in. Check your access token.');
    } finally { if (epoch === state.sessionEpoch) $('login-button').disabled = false; }
  });
}
function enqueueAuthentication(operation) {
  const request = authenticationQueue.then(operation, operation);
  authenticationQueue = request.catch(() => {});
  return request;
}
function consumeSignInFragment() {
  const params = new URLSearchParams(location.hash.slice(1));
  const token = params.get('token');
  if (token === null) return false;
  history.replaceState(null, '', location.pathname + location.search);
  login(token);
  return true;
}
async function boot() {
  if (consumeSignInFragment()) return;
  const epoch = state.sessionEpoch;
  try { const session = await api('/api/session'); await startSession(session.actor, epoch); }
  catch (error) { if (epoch === state.sessionEpoch) showLogin(error.status === 401 ? '' : error.message); }
}

function renderNavigation() {
  $('world-name').textContent = state.world?.name || 'Your world';
  $('actor-name').textContent = recordLabel(state.actor);
  $('actor-avatar').textContent = abbreviated(recordLabel(state.actor));
  $('actor-role').textContent = state.actor?.data?.role ? labelFor(state.actor.data.role) : 'Local session';
  const types = Object.keys(state.world?.types || {});
  $('type-total').textContent = types.length;
  const navigation = $('type-navigation');
  navigation.replaceChildren();
  types.forEach((type) => {
    const item = button('', 'nav-item', () => exploreType(type), `Explore ${typePlural(type)}`);
    item.classList.toggle('is-active', state.exploring && state.view?.type === type);
    item.append(el('span', 'type-icon', type.slice(0, 1).toUpperCase()), el('span', '', typePlural(type)), el('span', 'type-count', state.records.filter((record) => record.type === type).length));
    navigation.append(item);
  });
  $('home-button').classList.toggle('is-active', !state.exploring);
  $('current-location').textContent = state.exploring ? typePlural(state.view.type) : 'Your view';
}
function exploreType(type) {
  state.view = {
    title: typePlural(type), description: `Explore the ${typePlural(type).toLowerCase()} you can read in this world.`,
    layout: 'table', type, record_ids: state.records.filter((record) => record.type === type).map((record) => record.id),
    fields: Object.keys(fieldsFor(type)), group_by: null,
  };
  state.exploring = true;
  closeNavigation(); renderNavigation(); renderView();
}
function returnHome() {
  state.view = null; state.exploring = false; closeNavigation(); renderNavigation(); renderView();
}
function viewRecords() {
  if (!state.view) return [];
  const records = state.records.filter((record) => record.type === state.view.type);
  if (!Array.isArray(state.view.record_ids)) return records;
  const lookup = new Map(records.map((record) => [record.id, record]));
  return [...new Set(state.view.record_ids)].map((id) => lookup.get(id)).filter(Boolean);
}
function viewFields() {
  const schema = fieldsFor(state.view.type);
  const chosen = (Array.isArray(state.view.fields) ? state.view.fields : Object.keys(schema)).filter((name) => name in schema);
  const primary = primaryField(state.view.type);
  return [...new Set([primary, ...chosen].filter(Boolean))];
}
function renderView() {
  const content = $('view-content');
  content.replaceChildren(); $('view-actions').replaceChildren();
  $('view-meta').hidden = !state.view;
  if (!state.view) {
    $('view-kind').textContent = 'A fresh start';
    $('view-title').textContent = 'Your world, ready to explore.';
    $('view-description').textContent = 'Ask for what you need. Your client builds a view from the records you can see.';
    renderWelcome(content); renderProposals(); return;
  }
  const view = state.view;
  const records = viewRecords();
  $('view-kind').textContent = state.exploring ? 'Explore records' : 'Built for your intent';
  $('view-title').textContent = view.title || typePlural(view.type);
  $('view-description').textContent = view.description || 'A temporary view of shared records.';
  if (state.world.types[view.type]?.capabilities?.create === true) $('view-actions').append(button(`+ New ${typeLabel(view.type).toLowerCase()}`, 'button', () => openForm(view.type)));
  const meta = $('view-meta');
  const left = el('div', 'view-meta-left');
  left.append(el('strong', '', `${records.length} ${records.length === 1 ? 'record' : 'records'}`), el('span', '', `${typeLabel(view.type)} · Live records`));
  const layouts = el('div', 'layout-switch'); layouts.setAttribute('aria-label', 'View layout');
  ['list', 'board', 'table', 'overview'].forEach((layout) => {
    const control = button(labelFor(layout), `layout-button${view.layout === layout ? ' is-active' : ''}`, () => { state.view.layout = layout; renderView(); });
    control.setAttribute('aria-label', `Show as ${layout}`); control.setAttribute('aria-pressed', String(view.layout === layout));
    layouts.append(control);
  });
  meta.replaceChildren(left, layouts);
  if (!records.length) {
    const empty = el('div', 'empty-state');
    empty.append(el('span', 'empty-symbol', '◇'), el('h2', '', 'No matching records'), el('p', '', 'Ask for a broader view, explore a record type, or create a record if your account allows it.'));
    content.append(empty);
  } else if (view.layout === 'board') renderBoard(content, records);
  else if (view.layout === 'table') renderTable(content, records);
  else if (view.layout === 'overview') renderOverview(content, records);
  else renderList(content, records);
  renderProposals();
}
function renderWelcome(container) {
  const panel = el('div', 'welcome-panel');
  const copy = el('div');
  copy.append(el('h2', '', 'A world of records. A view for right now.'), el('p', '', 'Explore what exists, ask for a different perspective, or change a record. Start with a conversation.'));
  const snapshot = el('div', 'welcome-snapshot');
  Object.keys(state.world?.types || {}).forEach((type) => {
    const row = button('', 'snapshot-row', () => exploreType(type), `Explore ${typePlural(type)}`);
    row.append(el('span', 'type-icon', type.slice(0, 1)), el('strong', '', typePlural(type)), el('span', '', state.records.filter((record) => record.type === type).length));
    snapshot.append(row);
  });
  panel.append(el('div', 'welcome-orbit'), copy, snapshot);
  const suggestions = el('section', 'suggestion-section');
  const heading = el('div', 'section-heading');
  heading.append(el('h2', '', 'Find a starting point'), el('span', '', 'Or ask in your own words'));
  const grid = el('div', 'suggestion-grid');
  examples.forEach(([message, description, symbol]) => {
    const item = button('', 'suggestion-button', () => { $('intent-input').value = message; sizeComposer(); submitIntent(); }, message);
    const text = el('span', 'suggestion-copy'); text.append(el('strong', '', message), el('span', '', description));
    item.append(el('span', 'suggestion-symbol', symbol), text, el('span', 'suggestion-arrow', '↗'));
    item.disabled = state.busy; grid.append(item);
  });
  suggestions.append(heading, grid);
  const note = el('p', 'welcome-footnote');
  note.append(el('span', '', '◇'), document.createTextNode('Fresh views in every session. Shared records that stay.'));
  container.append(panel, suggestions, note);
}
function renderTable(container, records) {
  const wrap = el('div', 'record-table-wrap'), table = el('table', 'record-table');
  const head = el('thead'), row = el('tr');
  const fields = viewFields(), schema = fieldsFor(state.view.type);
  fields.forEach((field) => { const th = el('th', '', labelFor(field, schema[field])); th.scope = 'col'; row.append(th); });
  head.append(row); table.append(head);
  const body = el('tbody');
  records.forEach((record) => {
    const tr = el('tr');
    fields.forEach((field, index) => {
      const td = el('td');
      td.append(index === 0 ? button(recordLabel(record), 'record-link', () => openRecord(record)) : valueElement(record.data[field], schema[field]));
      tr.append(td);
    });
    body.append(tr);
  });
  table.append(body); wrap.append(table); container.append(wrap);
}
function recordCard(record, fields) {
  const card = button('', 'board-card', () => openRecord(record), `Open ${recordLabel(record)}`);
  card.append(el('h3', '', recordLabel(record)));
  const metadata = el('div', 'card-fields');
  const schema = fieldsFor(record.type);
  fields.filter((field) => field !== primaryField(record.type)).slice(0, 4).forEach((field) => {
    const line = el('div', 'card-field');
    const value = el('span', 'card-field-value'); value.append(valueElement(record.data[field], schema[field]));
    line.append(el('span', 'card-field-label', labelFor(field, schema[field])), value); metadata.append(line);
  });
  card.append(metadata); return card;
}
function renderBoard(container, records) {
  const schema = fieldsFor(state.view.type);
  const group = state.view.group_by && state.view.group_by in schema ? state.view.group_by : Object.keys(schema).find((field) => kindOf(schema[field]) === 'enum');
  if (!group) { renderOverview(container, records); return; }
  const descriptor = schema[group], groups = new Map();
  enumValues(descriptor).forEach((value) => groups.set(String(value), []));
  records.forEach((record) => {
    const value = record.data[group];
    const key = value === null || value === undefined || value === '' ? '' : String(value);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(record);
  });
  const board = el('div', 'board');
  groups.forEach((items, key) => {
    const column = el('section', 'board-column');
    applyBadgeColor(column, key, descriptor);
    const header = el('div', 'board-column-header');
    const title = isReference(descriptor) ? plainValue(key, descriptor) : labelFor(key || 'Unassigned');
    header.append(el('span', 'column-dot'), el('h2', '', title), el('span', 'column-count', items.length));
    column.append(header);
    items.forEach((record) => column.append(recordCard(record, viewFields().filter((field) => field !== group))));
    if (!items.length) column.append(el('p', 'column-empty', 'No records here'));
    board.append(column);
  });
  container.append(board);
}
function renderList(container, records) {
  const list = el('div', 'list-view'), schema = fieldsFor(state.view.type), fields = viewFields();
  records.forEach((record) => {
    const row = button('', 'list-row', () => openRecord(record), `Open ${recordLabel(record)}`);
    const copy = el('div', 'list-row-copy');
    copy.append(el('h3', '', recordLabel(record)));
    const metadata = el('div', 'list-metadata');
    fields.filter((field) => field !== primaryField(record.type)).slice(0, 4).forEach((field) => {
      const value = el('span'); value.append(valueElement(record.data[field], schema[field])); metadata.append(value);
    });
    copy.append(metadata); row.append(el('span', 'list-record-marker', record.type.slice(0, 1)), copy, el('span', 'list-row-arrow', '↗')); list.append(row);
  });
  container.append(list);
}
function renderOverview(container, records) {
  const schema = fieldsFor(state.view.type);
  const group = state.view.group_by && state.view.group_by in schema ? state.view.group_by : Object.keys(schema).find((field) => kindOf(schema[field]) === 'enum');
  const summary = el('div', 'overview-summary');
  const total = el('div', 'metric-card'); total.append(el('span', '', 'Records in this view'), el('strong', '', records.length)); summary.append(total);
  if (group) {
    const values = [...new Set([...enumValues(schema[group]), ...records.map((record) => record.data[group]).filter((value) => value !== null && value !== undefined)])].slice(0, 5);
    values.forEach((value) => {
      const card = el('div', 'metric-card');
      card.append(el('span', '', isReference(schema[group]) ? plainValue(value, schema[group]) : labelFor(value)), el('strong', '', records.filter((record) => record.data[group] === value).length)); summary.append(card);
    });
  }
  const grid = el('div', 'overview-grid'); records.forEach((record) => grid.append(recordCard(record, viewFields())));
  container.append(summary, grid);
}

function sizeComposer() {
  const input = $('intent-input'); input.style.height = 'auto'; input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
}
function renderResponse() {
  const container = $('assistant-response');
  container.replaceChildren(); container.classList.toggle('is-working', state.busy); container.hidden = !state.busy && !state.response;
  if (state.busy) {
    const dots = el('span', 'working-dots'); dots.append(el('span'), el('span'), el('span'));
    container.append(dots, el('p', '', 'Reading your world and building a view…'));
  } else if (state.response) container.append(el('span', 'assistant-avatar', '✳'), el('p', '', state.response));
  $('send-button').disabled = state.busy;
  $('send-button').setAttribute('aria-label', state.busy ? 'Processing message' : 'Send message');
  $('composer-hint').textContent = state.busy ? 'Working with the records you can access' : 'Enter to send · Shift + Enter for a new line';
  $('intent-form').setAttribute('aria-busy', String(state.busy));
  document.querySelectorAll('.suggestion-button').forEach((item) => { item.disabled = state.busy; });
}
function cleanView(view) {
  if (!view || !state.world.types[view.type]) return null;
  return {
    title: String(view.title || typePlural(view.type)), description: String(view.description || ''), type: view.type,
    layout: ['list', 'board', 'table', 'overview'].includes(view.layout) ? view.layout : 'list',
    record_ids: Array.isArray(view.record_ids) ? view.record_ids.filter((id) => typeof id === 'string') : [],
    fields: Array.isArray(view.fields) ? view.fields.filter((field) => typeof field === 'string' && field in fieldsFor(view.type)) : Object.keys(fieldsFor(view.type)),
    group_by: view.group_by && view.group_by in fieldsFor(view.type) ? view.group_by : null,
  };
}
async function submitIntent(event) {
  event?.preventDefault();
  const input = $('intent-input'), message = input.value.trim();
  if (!message || state.busy) return;
  const epoch = state.sessionEpoch;
  const context = { view: state.view, conversation: state.conversation.slice(-8).map(({ role, content }) => ({ role, content })) };
  state.busy = true; renderResponse(); $('notification').hidden = true;
  try {
    const result = await api('/client/intent', { method: 'POST', body: { message, context } });
    if (epoch !== state.sessionEpoch || !state.actor) return;
    const view = cleanView(result.view);
    if (result.view && !view) throw new Error('The requested view uses a record type this client cannot discover. Try another request.');
    state.conversation.push({ role: 'user', content: message }, { role: 'assistant', content: String(result.message || 'Your view is ready.') });
    state.response = String(result.message || 'Your view is ready.');
    if (view) { state.view = view; state.exploring = false; }
    state.proposals = (result.proposals || []).filter((proposal) => proposal && ['create', 'update', 'delete'].includes(proposal.operation)).map((proposal) => ({ proposal, status: 'pending', error: '' }));
    state.originalRequest = message;
    if (input.value.trim() === message) input.value = '';
    sizeComposer(); renderNavigation(); renderView();
    if (!$('activity-drawer').hidden && state.activityTab === 'conversation') renderActivity();
  } catch (error) {
    if (epoch === state.sessionEpoch) handleError(error, () => submitIntent());
  } finally { if (epoch === state.sessionEpoch) { state.busy = false; renderResponse(); } }
}
function renderProposals() {
  const container = $('proposals'); container.replaceChildren(); container.hidden = !state.proposals.length;
  if (!state.proposals.length) return;
  const heading = el('div', 'proposal-heading'); heading.append(el('h2', '', 'Proposed changes'), el('span', 'enum-badge', 'Review first'));
  container.append(heading, el('p', '', 'Confirm each change to save it to the shared world.'));
  state.proposals.forEach((item) => {
    const proposal = item.proposal, record = getRecord(proposal.type, proposal.id);
    const card = el('article', 'proposal-card'), header = el('div', 'proposal-card-header'), copy = el('div');
    const name = record ? recordLabel(record) : proposal.changes?.[primaryField(proposal.type)] || typeLabel(proposal.type);
    copy.append(el('h3', '', `${labelFor(proposal.operation)} ${name}`), el('p', 'proposal-reason', proposal.reason || state.originalRequest)); header.append(copy); card.append(header);
    const changes = el('div', 'proposal-changes');
    Object.entries(proposal.changes || {}).forEach(([field, value]) => {
      const line = el('div', 'proposal-change');
      line.append(el('span', '', `${labelFor(field, fieldsFor(proposal.type)[field])}:`));
      if (record && field in record.data && proposal.operation === 'update') line.append(el('span', '', plainValue(record.data[field], fieldsFor(proposal.type)[field])), el('span', '', '→'));
      line.append(valueElement(value, fieldsFor(proposal.type)[field])); changes.append(line);
    });
    if (proposal.operation === 'delete') changes.append(el('span', '', 'This record will be removed. Its history will remain.'));
    card.append(changes);
    if (item.error) card.append(el('p', 'proposal-error', item.error));
    const actions = el('div', 'proposal-actions');
    if (['pending', 'applying'].includes(item.status)) {
      const dismiss = button('Dismiss', 'button quiet', () => { item.status = 'dismissed'; renderProposals(); }); dismiss.disabled = item.status === 'applying';
      const confirm = button(item.status === 'applying' ? 'Saving…' : 'Confirm change', 'button primary', () => applyProposal(item)); confirm.disabled = item.status === 'applying';
      actions.append(dismiss, confirm);
    } else if (item.status === 'conflict') {
      actions.append(button('Dismiss', 'button quiet', () => { item.status = 'dismissed'; renderProposals(); }));
      const review = button('Review again', 'button primary', () => { $('intent-input').value = state.originalRequest; sizeComposer(); submitIntent(); });
      review.disabled = state.busy; actions.append(review);
    } else actions.append(el('span', 'proposal-state', item.status === 'applied' ? '✓ Saved to the shared world' : 'Dismissed'));
    card.append(actions); container.append(card);
  });
}
async function applyProposal(item) {
  const epoch = state.sessionEpoch;
  const reason = state.originalRequest;
  item.status = 'applying'; item.error = ''; renderProposals();
  try {
    const result = await api('/client/apply', { method: 'POST', body: { proposal: item.proposal, reason } });
    if (epoch !== state.sessionEpoch) return;
    item.status = 'applied';
    // A successful write stays successful even if a subsequent refresh fails.
    const name = result.record ? recordLabel(result.record) : typeLabel(item.proposal.type);
    await refreshAfterWrite(`${name}: change saved. It will remain when you open a new client.`, epoch);
    if (epoch !== state.sessionEpoch) return;
    if (!$('activity-drawer').hidden && state.activityTab === 'changes') loadActivity();
  } catch (error) {
    if (epoch !== state.sessionEpoch) return;
    item.status = 'pending'; item.error = error.message;
    if (error.status === 409) {
      item.status = 'conflict';
      item.error += ' This record has changed. Review your request again before saving.';
      try { await refreshRecords(); } catch (refreshError) { if (epoch === state.sessionEpoch) handleError(refreshError, refreshRecords); }
    }
    if (error.status === 401) showLogin(error.message);
  } finally { if (epoch === state.sessionEpoch) renderProposals(); }
}
async function refreshAfterWrite(message, epoch) {
  try {
    await refreshRecords();
    if (epoch === state.sessionEpoch) notify(message);
  } catch (error) {
    if (epoch !== state.sessionEpoch) return;
    if (error.status === 401) showLogin('The change was saved. Sign in again to see the current records.');
    else notify(`Change saved, but records could not refresh. ${error.message}`, true, async () => {
      try { await refreshRecords(); notify('Current records loaded.'); } catch (refreshError) { handleError(refreshError, refreshRecords); }
    });
  }
}

function recordDialogHeading(title, eyebrow, subtitle) {
  const heading = el('div', 'dialog-heading'), copy = el('div');
  copy.append(el('p', 'eyebrow', eyebrow));
  const h2 = el('h2', '', title); h2.id = 'record-dialog-title'; copy.append(h2);
  if (subtitle) copy.append(el('p', 'dialog-description', subtitle));
  heading.append(copy, button('×', 'icon-button', () => $('record-dialog').close(), 'Close record details'));
  return heading;
}
function openRecord(record, tab = 'fields') {
  state.selectedRecord = { type: record.type, id: record.id }; state.recordTab = tab; state.formMode = null;
  renderRecord(record);
  if (!$('record-dialog').open) $('record-dialog').showModal();
}
function renderRecord(record) {
  const content = $('record-dialog-content');
  content.replaceChildren(recordDialogHeading(recordLabel(record), `${typeLabel(record.type)} record`, `${record.id} · Version ${record.version}`));
  const actions = el('div', 'dialog-actions');
  if (record.capabilities?.update) actions.append(button('Edit record', 'button', () => openForm(record.type, record)));
  if (record.capabilities?.delete) actions.append(button('Delete', 'button danger', () => showDeleteConfirm(record)));
  if (actions.children.length) content.append(actions);
  const tabs = el('div', 'tab-bar record-tabs');
  ['fields', 'history'].forEach((tab) => {
    const control = button(labelFor(tab), `tab-button${state.recordTab === tab ? ' is-active' : ''}`, () => { state.recordTab = tab; renderRecord(record); });
    control.setAttribute('aria-pressed', String(state.recordTab === tab)); tabs.append(control);
  });
  content.append(tabs);
  if (state.recordTab === 'history') {
    const history = el('div', 'detail-history', 'Loading record history…'); content.append(history);
    loadRecordHistory(record, history);
  } else {
    const fields = el('dl', 'record-fields');
    Object.entries(fieldsFor(record.type)).forEach(([name, descriptor]) => {
      const row = el('div', 'detail-field'), value = el('dd'); value.append(valueElement(record.data[name], descriptor, true));
      row.append(el('dt', '', labelFor(name, descriptor)), value); fields.append(row);
    });
    content.append(fields);
  }
  content.append(el('p', 'dialog-foot', `Created ${dateText(record.created_at, true)} · Updated ${dateText(record.updated_at, true)}. Changes are saved with actor and reason.`));
}
async function loadRecordHistory(record, container) {
  try {
    const data = await api(`/api/history/${encodeURIComponent(record.type)}/${encodeURIComponent(record.id)}`);
    if (!container.isConnected) return;
    container.replaceChildren();
    const events = (data.events || []).slice().sort((a, b) => String(eventTime(b)).localeCompare(String(eventTime(a))));
    if (!events.length) container.append(el('p', 'activity-empty', 'No changes recorded yet.'));
    events.forEach((event) => container.append(eventElement({ ...event, record_type: record.type, record_id: record.id }, false)));
  } catch (error) { if (container.isConnected) container.replaceChildren(el('p', 'form-error', error.message)); }
}
function eventTime(event) { return event.at || event.timestamp || event.created_at || ''; }
function eventElement(event, showRecord = true) {
  const entry = el('article', 'timeline-event');
  const type = event.record_type || event.type || event.record?.type;
  const id = event.record_id || event.record?.id;
  const record = getRecord(type, id);
  const title = el('div', 'timeline-event-title');
  title.append(document.createTextNode(`${labelFor(event.operation || event.action || 'Changed')}${showRecord ? ' · ' : ''}`));
  if (showRecord) title.append(record ? button(recordLabel(record), 'record-link', () => openRecord(record)) : document.createTextNode(`${typeLabel(type || 'Record')} ${id || ''}`));
  entry.append(title);
  const actor = event.requested_by || event.actor;
  let actorName = '';
  if (typeof actor === 'string') actorName = recordLabel(getRecord(state.actor?.type, actor)) === 'Unknown record' ? actor : recordLabel(getRecord(state.actor?.type, actor));
  else if (actor) actorName = actor.name || actor.data?.name || actor.id || '';
  if (actorName) entry.append(el('p', '', `By ${actorName}`));
  if (event.reason) entry.append(el('p', '', String(event.reason)));
  const after = event.after?.data || event.after;
  const before = event.before?.data || event.before;
  const changes = event.changes || ((event.operation || event.action) === 'update' && after && typeof after === 'object' ? Object.fromEntries(Object.entries(after).filter(([field, value]) => JSON.stringify(value) !== JSON.stringify(before?.[field]))) : null);
  if (changes && typeof changes === 'object') entry.append(el('p', '', Object.entries(changes).map(([field, value]) => `${labelFor(field)} → ${plainValue(value, fieldsFor(type)[field])}`).join('\n')));
  if (Array.isArray(event.evidence) && event.evidence.length) entry.append(el('p', '', `Evidence: ${event.evidence.map((item) => typeof item === 'string' ? item : JSON.stringify(item)).join(', ')}`));
  const time = el('time', '', dateText(eventTime(event), true));
  if (eventTime(event)) time.dateTime = eventTime(event);
  entry.append(time); return entry;
}

function openForm(type, record = null) {
  state.formMode = record ? 'edit' : 'create';
  const content = $('record-dialog-content');
  content.replaceChildren(recordDialogHeading(record ? `Edit ${recordLabel(record)}` : `New ${typeLabel(type).toLowerCase()}`, 'From the discovered schema', record ? `Changes use record version ${record.version}.` : 'The server validates fields, relationships, and your permissions.'));
  const form = el('form', 'record-form');
  const controls = new Map();
  Object.entries(fieldsFor(type)).forEach(([name, descriptor]) => {
    if (descriptor.readonly || descriptor.read_only) return;
    const value = record ? record.data[name] : descriptor.default === '$actor' ? state.actor.id : descriptor.default;
    const { field, read } = fieldControl(type, name, descriptor, value);
    const protectedField = descriptor.writable === false;
    if (protectedField) field.querySelectorAll('input,textarea,select,fieldset').forEach((control) => { control.disabled = true; });
    form.append(field); controls.set(name, { read, descriptor });
  });
  const reasonField = el('div', 'form-field'), reasonLabel = el('label', '', 'Reason for this change');
  reasonLabel.htmlFor = 'change-reason';
  const reason = el('input'); reason.id = 'change-reason'; reason.name = 'reason'; reason.required = true; reason.placeholder = 'What changed, and why?'; reason.maxLength = 2000;
  reasonField.append(reasonLabel, reason, el('p', 'field-help', 'Saved in the record history.')); form.append(reasonField);
  const errorText = el('p', 'form-error'); errorText.setAttribute('role', 'alert'); form.append(errorText);
  const actions = el('div', 'form-row-actions');
  actions.append(button('Cancel', 'button quiet', () => record ? openRecord(getRecord(type, record.id) || record) : $('record-dialog').close()));
  const save = el('button', 'button primary', record ? 'Save changes' : 'Create record'); save.type = 'submit'; actions.append(save); form.append(actions);
  form.addEventListener('submit', async (event) => {
    event.preventDefault(); if (!form.reportValidity()) return;
    const epoch = state.sessionEpoch;
    save.disabled = true; errorText.textContent = '';
    try {
      const data = {};
      controls.forEach(({ read, descriptor }, name) => {
        let value = read();
        if (value === '' && descriptor.nullable) value = null;
        if (!record && (value === '' || value === undefined) && !descriptor.required && descriptor.default === undefined) return;
        if (!record || JSON.stringify(value) !== JSON.stringify(record.data[name])) data[name] = value;
      });
      if (record && !Object.keys(data).length) { openRecord(record); notify('No fields changed.'); return; }
      const path = `/api/records/${encodeURIComponent(type)}${record ? `/${encodeURIComponent(record.id)}` : ''}`;
      const result = await api(path, { method: record ? 'PATCH' : 'POST', body: record ? { changes: data, expected_version: record.version, reason: reason.value.trim() } : { data, reason: reason.value.trim() } });
      if (epoch !== state.sessionEpoch) return;
      $('record-dialog').close();
      await refreshAfterWrite(`${typeLabel(type)} saved. The change is recorded in its history.`, epoch);
      if (epoch !== state.sessionEpoch) return;
      if (result.record && state.view?.type === type && !state.exploring && state.view.record_ids) {
        if (!state.view.record_ids.includes(result.record.id)) state.view.record_ids.push(result.record.id);
        renderView();
      }
      if (!$('activity-drawer').hidden) loadActivity();
    } catch (error) {
      if (epoch !== state.sessionEpoch) return;
      if (error.status === 401) showLogin(error.message);
      else if (error.status === 409) {
        errorText.textContent = `${error.message} Loading the current record…`;
        try {
          await refreshRecords();
          if (epoch === state.sessionEpoch) errorText.textContent = `${error.message} Your edits are still here. Close the editor and reopen the record to review its current version.`;
        } catch (refreshError) {
          if (epoch === state.sessionEpoch) errorText.textContent = `${error.message} The latest record could not be loaded. ${refreshError.message}`;
        }
      } else errorText.textContent = error.message;
    } finally { save.disabled = false; }
  });
  content.append(form);
  if (!$('record-dialog').open) $('record-dialog').showModal();
}
function fieldControl(type, name, descriptor, value) {
  const field = el('div', 'form-field');
  const id = `field-${type}-${name}`;
  const label = el('label', '', labelFor(name, descriptor)); label.htmlFor = id;
  if (descriptor.required) label.append(el('span', 'required-mark', '*'));
  field.append(label);
  const kind = kindOf(descriptor);
  let input, read;
  if (isList(descriptor) && isReference(listDescriptor(descriptor))) {
    const target = refType(listDescriptor(descriptor));
    input = el('fieldset'); input.id = id; label.removeAttribute('for');
    const legend = el('legend', 'sr-only', label.textContent); input.append(legend);
    state.records.filter((record) => record.type === target).forEach((record) => {
      const option = el('label', 'checkbox-option'); const checkbox = el('input'); checkbox.type = 'checkbox'; checkbox.value = record.id; checkbox.checked = Array.isArray(value) && value.includes(record.id);
      option.append(checkbox, document.createTextNode(recordLabel(record))); input.append(option);
    });
    if (input.children.length === 1) input.append(el('p', 'text-muted', `No readable ${typePlural(target).toLowerCase()}.`));
    read = () => [...input.querySelectorAll('input:checked')].map((control) => control.value);
  } else if (kind === 'enum' || isReference(descriptor)) {
    input = el('select');
    if (!descriptor.required || descriptor.nullable || value === undefined) { const option = el('option', '', 'Select a value'); option.value = ''; input.append(option); }
    const options = isReference(descriptor) ? state.records.filter((record) => record.type === refType(descriptor)).map((record) => [record.id, recordLabel(record)]) : enumValues(descriptor).map((item) => [item, labelFor(item)]);
    options.forEach(([key, title]) => { const option = el('option', '', title); option.value = String(key); input.append(option); });
    input.value = value ?? ''; read = () => input.value;
  } else if (kind === 'boolean' || kind === 'bool') {
    input = el('select'); [['true', 'Yes'], ['false', 'No']].forEach(([key, title]) => { const option = el('option', '', title); option.value = key; input.append(option); });
    input.value = String(value ?? false); read = () => input.value === 'true';
  } else if (isList(descriptor) || kind === 'json' || kind === 'object') {
    input = el('textarea'); input.value = JSON.stringify(value ?? (isList(descriptor) ? [] : {}), null, 2);
    read = () => { try { return JSON.parse(input.value); } catch { throw new Error(`${labelFor(name)} must be valid JSON.`); } };
    field.append(el('p', 'field-help', 'Enter a JSON value.'));
  } else {
    input = el(kind === 'text' && (!descriptor.required || /description|body|notes/.test(name)) ? 'textarea' : 'input');
    if (input.tagName === 'INPUT') input.type = kind === 'date' ? 'date' : ['number', 'integer', 'float'].includes(kind) ? 'number' : 'text';
    input.value = value ?? '';
    if (descriptor.max_length || descriptor.maxLength) input.maxLength = descriptor.max_length || descriptor.maxLength;
    if (descriptor.min_length || descriptor.minLength) input.minLength = descriptor.min_length || descriptor.minLength;
    if (kind === 'integer') input.step = '1';
    if (descriptor.min !== undefined) input.min = descriptor.min;
    if (descriptor.max !== undefined) input.max = descriptor.max;
    read = () => ['number', 'integer', 'float'].includes(kind) && input.value !== '' ? Number(input.value) : input.value;
  }
  input.id = id; input.name = name;
  if (input.tagName !== 'FIELDSET') input.required = Boolean(descriptor.required && !descriptor.nullable);
  field.append(input);
  if (descriptor.description) field.append(el('p', 'field-help', descriptor.description));
  if (descriptor.write_rule) field.append(el('p', 'field-help', `Write rule: ${descriptor.write_rule}`));
  return { field, read };
}
function showDeleteConfirm(record) {
  if ($('record-dialog-content').querySelector('.delete-confirm')) return;
  const confirm = el('form', 'delete-confirm');
  confirm.append(el('h3', '', 'Delete this record?'), el('p', '', 'The server checks your permission and any references before deleting. Its history remains.'));
  const field = el('div', 'form-field'), label = el('label', '', 'Reason for deletion'), reason = el('input');
  reason.id = 'delete-reason'; reason.required = true; reason.maxLength = 2000; label.htmlFor = reason.id;
  field.append(label, reason); confirm.append(field);
  const errorText = el('p', 'form-error'); errorText.setAttribute('role', 'alert'); confirm.append(errorText);
  const actions = el('div', 'form-row-actions'); actions.append(button('Keep record', 'button quiet', () => confirm.remove()));
  const remove = el('button', 'button danger', 'Delete record'); remove.type = 'submit'; actions.append(remove); confirm.append(actions);
  confirm.addEventListener('submit', async (event) => {
    event.preventDefault(); if (!confirm.reportValidity()) return;
    const epoch = state.sessionEpoch;
    remove.disabled = true; errorText.textContent = '';
    try {
      await api(`/api/records/${encodeURIComponent(record.type)}/${encodeURIComponent(record.id)}`, { method: 'DELETE', body: { expected_version: record.version, reason: reason.value.trim() } });
      if (epoch !== state.sessionEpoch) return;
      $('record-dialog').close();
      await refreshAfterWrite('Record deleted. Its change history remains on the server.', epoch);
      if (epoch !== state.sessionEpoch) return;
      if (!$('activity-drawer').hidden) loadActivity();
    } catch (error) { if (epoch === state.sessionEpoch) errorText.textContent = error.message; }
    finally { remove.disabled = false; }
  });
  $('record-dialog-content').querySelector('.dialog-actions').after(confirm); reason.focus();
}

function openSchema() {
  const content = $('schema-content'); content.replaceChildren();
  Object.entries(state.world.types).forEach(([type, definition]) => {
    const card = el('section', 'schema-type'), heading = el('div', 'schema-type-header');
    heading.append(el('h3', '', typeLabel(type)));
    const capabilities = el('div', 'capabilities');
    Object.entries(definition.capabilities || {}).filter(([, allowed]) => allowed === true).forEach(([operation]) => capabilities.append(el('span', 'capability', labelFor(operation)))); heading.append(capabilities); card.append(heading);
    Object.entries(definition.fields).forEach(([name, descriptor]) => {
      const field = el('div', 'schema-field'), label = el('div', '', labelFor(name, descriptor));
      let kind = kindOf(descriptor);
      if (isReference(descriptor)) kind = `Reference to ${typeLabel(refType(descriptor))}`;
      if (isList(descriptor)) kind = isReference(listDescriptor(descriptor)) ? `List of ${typePlural(refType(listDescriptor(descriptor)))}` : 'List';
      label.append(el('span', 'schema-field-type', `${labelFor(kind)}${descriptor.required ? ' · Required' : ' · Optional'}`));
      const description = el('div', 'schema-field-description');
      if (descriptor.description) description.append(el('p', '', descriptor.description));
      if (enumValues(descriptor).length) description.append(el('p', '', `Values: ${enumValues(descriptor).map((value) => labelFor(value)).join(', ')}`));
      if (descriptor.default !== undefined) description.append(el('p', '', `Default: ${plainValue(descriptor.default, descriptor)}`));
      if (descriptor.nullable) description.append(el('p', '', 'May be empty.'));
      if (descriptor.max_length) description.append(el('p', '', `Up to ${descriptor.max_length} characters.`));
      if (descriptor.write_rule) description.append(el('p', '', `Write rule: ${descriptor.write_rule}`));
      if (!description.children.length) description.append(el('span', '', 'Validated by the record service.'));
      field.append(label, description); card.append(field);
    });
    const rules = definition.rules || definition.capabilities?.rules;
    if (rules || Object.values(definition.capabilities || {}).some((value) => typeof value === 'string')) {
      const section = el('div', 'schema-rules');
      const values = typeof rules === 'string' ? [rules] : Array.isArray(rules) ? rules : rules ? Object.entries(rules).map(([operation, rule]) => `${labelFor(operation)}: ${rule}`) : [];
      values.forEach((rule) => section.append(el('p', '', typeof rule === 'string' ? rule : JSON.stringify(rule))));
      (definition.constraints || []).forEach((constraint) => section.append(el('p', '', `Constraint: ${constraint}`)));
      Object.entries(definition.capabilities || {}).filter(([, value]) => typeof value === 'string').forEach(([operation, rule]) => section.append(el('p', '', `${labelFor(operation)}: ${rule}`)));
      card.append(section);
    }
    content.append(card);
  });
  closeNavigation(); $('schema-dialog').showModal();
}
function openActivity() {
  $('activity-drawer').hidden = false; $('activity-backdrop').hidden = false;
  $('activity-button').setAttribute('aria-expanded', 'true');
  $('activity-close').focus();
  if (state.activityTab === 'changes') loadActivity(); else renderActivity();
}
function closeActivity() {
  $('activity-drawer').hidden = true; $('activity-backdrop').hidden = true;
  $('activity-button').setAttribute('aria-expanded', 'false');
}
async function loadActivity() {
  if (state.eventsLoading) return;
  const epoch = state.sessionEpoch;
  state.eventsLoading = true; renderActivity();
  const records = state.records.slice().sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at))).slice(0, 40);
  const results = await Promise.allSettled(records.map(async (record) => {
    const result = await api(`/api/history/${encodeURIComponent(record.type)}/${encodeURIComponent(record.id)}`);
    return (result.events || []).map((event) => ({ ...event, record_type: record.type, record_id: record.id }));
  }));
  if (epoch !== state.sessionEpoch) return;
  state.events = results.filter((result) => result.status === 'fulfilled').flatMap((result) => result.value).sort((a, b) => String(eventTime(b)).localeCompare(String(eventTime(a)))).slice(0, 50);
  state.eventsLoading = false;
  state.activityError = results.find((result) => result.status === 'rejected')?.reason?.message || '';
  renderActivity();
}
function renderActivity() {
  const content = $('activity-content'); content.replaceChildren();
  $('changes-tab').classList.toggle('is-active', state.activityTab === 'changes'); $('changes-tab').setAttribute('aria-pressed', String(state.activityTab === 'changes'));
  $('conversation-tab').classList.toggle('is-active', state.activityTab === 'conversation'); $('conversation-tab').setAttribute('aria-pressed', String(state.activityTab === 'conversation'));
  $('activity-foot').textContent = state.activityTab === 'changes' ? 'Recent history of readable records. Saved on this server.' : 'This conversation disappears when the client reloads.';
  if (state.activityTab === 'conversation') {
    if (!state.conversation.length) content.append(el('p', 'activity-empty', 'Your conversation will appear here. Ask World for a view to get started.'));
    state.conversation.forEach(({ role, content: text }) => {
      const entry = el('article', `conversation-entry ${role}`); entry.append(el('span', '', role === 'user' ? 'You' : 'World'), el('p', '', text)); content.append(entry);
    });
  } else if (state.eventsLoading) content.append(el('p', 'activity-empty', 'Reading change history…'));
  else {
    if (state.activityError) content.append(el('p', 'form-error', `Some history could not be read. ${state.activityError}`));
    if (!state.events.length) content.append(el('p', 'activity-empty', 'No record history to show yet. Confirm a change to record who changed it and why.'));
    state.events.forEach((event) => content.append(eventElement(event)));
  }
}
function closeNavigation() {
  $('sidebar').classList.remove('is-open'); $('sidebar-backdrop').hidden = true; $('menu-button').setAttribute('aria-expanded', 'false');
}
function openNavigation() {
  $('sidebar').classList.add('is-open'); $('sidebar-backdrop').hidden = false; $('menu-button').setAttribute('aria-expanded', 'true');
}

$('login-form').addEventListener('submit', (event) => { event.preventDefault(); login($('access-token').value.trim()); });
$('intent-form').addEventListener('submit', submitIntent);
$('intent-input').addEventListener('input', sizeComposer);
$('intent-input').addEventListener('keydown', (event) => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); submitIntent(); } });
$('home-button').addEventListener('click', returnHome);
$('schema-button').addEventListener('click', openSchema);
$('schema-close').addEventListener('click', () => $('schema-dialog').close());
$('activity-button').addEventListener('click', () => $('activity-drawer').hidden ? openActivity() : closeActivity());
$('activity-close').addEventListener('click', () => { closeActivity(); $('activity-button').focus(); });
$('activity-backdrop').addEventListener('click', closeActivity);
$('changes-tab').addEventListener('click', () => { state.activityTab = 'changes'; loadActivity(); });
$('conversation-tab').addEventListener('click', () => { state.activityTab = 'conversation'; renderActivity(); });
$('menu-button').addEventListener('click', openNavigation);
$('sidebar-backdrop').addEventListener('click', closeNavigation);
$('logout-button').addEventListener('click', async () => {
  const epoch = state.sessionEpoch;
  $('logout-button').disabled = true;
  try {
    await enqueueAuthentication(() => epoch === state.sessionEpoch ? api('/api/session', { method: 'DELETE' }) : undefined);
    if (epoch === state.sessionEpoch) showLogin();
  }
  catch (error) { if (epoch === state.sessionEpoch) handleError(error); }
  finally { $('logout-button').disabled = false; }
});
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape' || $('record-dialog').open || $('schema-dialog').open) return;
  closeActivity(); closeNavigation();
});
for (const dialog of [$('record-dialog'), $('schema-dialog')]) {
  dialog.addEventListener('click', (event) => { if (event.target === dialog) { const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close(); } });
}
window.addEventListener('hashchange', consumeSignInFragment);
boot();
