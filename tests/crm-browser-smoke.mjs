// The same shared engine and client, with an isolated CRM database.
// --live replaces only the fixture planner with the real Codex provider.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, readFile, rm, mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { chromium } from 'playwright';

const live = process.argv.includes('--live');
const dataDir = await mkdtemp(resolve(tmpdir(), 'world-crm-browser-'));
const artifacts = resolve('artifacts', live ? 'crm-live' : 'crm-browser');
await mkdir(artifacts, { recursive: true });
let server, browser, base, tokens;
const pageErrors = [];
const requests = {
  open: 'Show my open sales opportunities.',
  board: 'Make this a pipeline board by stage.',
  move: 'Move the Northstar renewal opportunity to proposal.',
};

async function startServer() {
  let diagnostic = '';
  server = spawn(process.env.WORLD_PYTHON || 'python3', ['tests/crm_browser_server.py', '--data-dir', dataDir, ...(live ? ['--live'] : [])], {
    cwd: resolve('.'), stdio: ['ignore', 'pipe', 'pipe'],
  });
  server.stderr.on('data', (chunk) => { diagnostic = (diagnostic + chunk).slice(-6000); });
  const lines = createInterface({ input: server.stdout });
  let timer;
  const readyLine = new Promise((fulfill, reject) => {
    lines.once('line', fulfill);
    server.once('error', reject);
    server.once('exit', (code) => reject(new Error(`CRM fixture exited ${code}: ${diagnostic}`)));
    timer = setTimeout(() => reject(new Error(`CRM fixture startup timed out: ${diagnostic}`)), 15000);
  });
  const ready = JSON.parse(await readyLine.finally(() => { clearTimeout(timer); lines.close(); }));
  base = `http://127.0.0.1:${ready.port}`;
  tokens = JSON.parse(await readFile(resolve(dataDir, 'access.json'), 'utf8'));
}

async function stopServer() {
  if (!server || server.exitCode !== null) return;
  const process = server;
  server = null;
  const exited = once(process, 'exit');
  const force = setTimeout(() => process.kill('SIGKILL'), 5000);
  process.kill('SIGTERM');
  try { await exited; } finally { clearTimeout(force); }
}

async function recordApi(path, method = 'GET', body, actor = 'ben') {
  const response = await fetch(base + path, {
    method,
    headers: { Authorization: `Bearer ${tokens[actor]}`, ...(body ? { 'Content-Type': 'application/json' } : {}) },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  return { status: response.status, data: await response.json() };
}

async function readRecord(type, id, actor = 'ben') {
  const result = await recordApi(`/api/records/${type}/${id}`, 'GET', undefined, actor);
  assert.equal(result.status, 200, JSON.stringify(result.data));
  return result.data.record;
}

async function readHistory(type, id) {
  const result = await recordApi(`/api/history/${type}/${id}`);
  assert.equal(result.status, 200, JSON.stringify(result.data));
  return result.data.events;
}

async function signIn(page, actor = 'ben') {
  const session = page.waitForResponse((response) => response.url() === base + '/api/session' && response.request().method() === 'POST');
  await page.goto(`${base}/#token=${tokens[actor]}`);
  assert.equal((await session).status(), 200);
  await page.waitForFunction((id) => location.hash === '' && !document.getElementById('app').hidden && document.getElementById('actor-name').textContent.toLowerCase() === id, actor);
  assert.equal(new URL(page.url()).hash, '', 'The sign-in credential must leave the address bar');
}

async function ask(page, message) {
  await page.locator('#intent-input').fill(message);
  const completed = page.waitForResponse((response) => response.url() === base + '/client/intent' && response.request().method() === 'POST', { timeout: 140000 });
  await page.getByRole('button', { name: 'Send message', exact: true }).click();
  const response = await completed;
  const plan = await response.json();
  assert.equal(response.status(), 200, JSON.stringify(plan));
  assert.equal(plan.provider, live ? 'codex' : 'fixture');
  await page.waitForFunction(() => document.getElementById('intent-form').getAttribute('aria-busy') === 'false');
  console.log(`${live ? 'Live Codex' : 'Fixture'}: ${message} -> ${plan.view.layout}, ${plan.view.record_ids.length} records`);
  return plan;
}

async function explore(page, plural) {
  const menu = page.getByRole('button', { name: 'Open navigation', exact: true });
  if (await menu.isVisible()) await menu.click();
  await page.locator('#type-navigation').getByRole('button', { name: `Explore ${plural}`, exact: true }).click();
}

async function openRecord(page, name) {
  await page.locator('#view-content').getByRole('button', { name, exact: true }).click();
}

async function closeRecord(page) {
  await page.getByRole('button', { name: 'Close record details', exact: true }).click();
}

try {
  await startServer();
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  page.on('pageerror', (error) => pageErrors.push(error.message));
  await signIn(page);
  assert.equal(await page.locator('#view-title').innerText(), 'Your world, ready to explore.');
  const discovery = await recordApi('/api/world');
  assert.equal(discovery.status, 200);
  assert.deepEqual(new Set(Object.keys(discovery.data.types)), new Set(['Person', 'Organization', 'Contact', 'Opportunity', 'Interaction']));
  const suggestions = await page.locator('.suggestion-grid').innerText();
  assert.doesNotMatch(suggestions, /website project|documentation task/i, 'The shared client must not offer a different world\'s domain prompts');
  await page.getByRole('button', { name: 'Discover schema', exact: true }).click();
  const schema = await page.locator('#schema-dialog').innerText();
  assert.ok(schema.includes('Amount minor'));
  assert.ok(schema.includes('Minimum: 0'));
  assert.ok(schema.includes('same_reference'));
  assert.ok(schema.includes('Transition:'));
  assert.doesNotMatch(schema, /\[object Object\]/);
  await page.getByRole('button', { name: 'Close schema', exact: true }).click();
  await page.screenshot({ path: resolve(artifacts, 'welcome.png'), fullPage: true, animations: 'disabled' });

  const beforePlan = await readRecord('Opportunity', 'northstar-renewal');
  const beforeHistory = await readHistory('Opportunity', 'northstar-renewal');
  const open = await ask(page, requests.open);
  assert.equal(open.view.type, 'Opportunity');
  assert.ok(open.view.record_ids.includes('northstar-renewal'));
  for (const id of open.view.record_ids) {
    const record = await readRecord('Opportunity', id);
    assert.equal(record.data.owner, 'ben', 'The request for my opportunities must scope the actor');
    assert.ok(!['won', 'lost'].includes(record.data.stage));
  }
  const board = await ask(page, requests.board);
  assert.equal(board.view.layout, 'board');
  assert.equal(board.view.type, 'Opportunity');
  assert.equal(board.view.group_by, 'stage');
  assert.deepEqual(new Set(board.view.record_ids), new Set(open.view.record_ids));
  assert.equal(await page.locator('.board-column').count(), discovery.data.types.Opportunity.fields.stage.values.length);
  await page.screenshot({ path: resolve(artifacts, 'pipeline.png'), fullPage: true, animations: 'disabled' });

  const proposal = await ask(page, requests.move);
  const index = proposal.proposals.findIndex((change) => change.type === 'Opportunity' && change.id === 'northstar-renewal' && change.changes.stage === 'proposal');
  assert.ok(index >= 0, 'The request must propose the requested stage change');
  assert.deepEqual(await readRecord('Opportunity', 'northstar-renewal'), beforePlan, 'Generating a proposal must not save records');
  assert.deepEqual(await readHistory('Opportunity', 'northstar-renewal'), beforeHistory, 'Generating a proposal must not create history');
  await page.screenshot({ path: resolve(artifacts, 'reviewed-proposal.png'), fullPage: true, animations: 'disabled' });
  await explore(page, 'Opportunities');
  await openRecord(page, 'Northstar renewal');
  assert.ok((await page.locator('.record-fields').innerText()).includes('Qualified'), 'The saved stage must remain qualified while the proposal awaits confirmation');
  await closeRecord(page);
  const saved = page.waitForResponse((response) => response.url() === base + '/client/apply' && response.request().method() === 'POST');
  await page.locator('.proposal-card').nth(index).getByRole('button', { name: 'Confirm change', exact: true }).click();
  assert.equal((await saved).status(), 200);
  await page.getByText('✓ Saved to the shared world', { exact: true }).waitFor();
  const moved = await readRecord('Opportunity', 'northstar-renewal');
  assert.equal(moved.data.stage, 'proposal');
  assert.equal(moved.version, beforePlan.version + 1);
  const savedHistory = await readHistory('Opportunity', 'northstar-renewal');
  assert.equal(savedHistory.length, beforeHistory.length + 1);
  assert.equal(savedHistory.at(-1).reason, requests.move);
  assert.equal(savedHistory.at(-1).requested_by, 'ben');
  assert.equal(savedHistory.at(-1).before.data.stage, 'qualified');
  assert.equal(savedHistory.at(-1).after.data.stage, 'proposal');

  await explore(page, 'Opportunities');
  await openRecord(page, 'Northstar renewal');
  await page.locator('#record-dialog').getByRole('button', { name: 'History', exact: true }).click();
  await page.locator('.detail-history .timeline-event').first().waitFor();
  const visibleHistory = await page.locator('.detail-history').innerText();
  assert.ok(visibleHistory.includes(requests.move));
  assert.match(visibleHistory, /Stage.*proposal/i);
  assert.match(visibleHistory, /By ben/i);
  await page.screenshot({ path: resolve(artifacts, 'record-history.png'), fullPage: true, animations: 'disabled' });
  await closeRecord(page);

  // The same discovered schema drives a Contact form and an integer editor.
  await explore(page, 'Contacts');
  await page.getByRole('button', { name: '+ New contact', exact: true }).click();
  await page.locator('#field-Contact-name').fill('Browser contact');
  await page.locator('#field-Contact-email').fill('browser@northstar.example');
  await page.locator('#field-Contact-organization').selectOption('northstar');
  await page.locator('#change-reason').fill('Verify a schema-derived CRM contact form');
  const created = page.waitForResponse((response) => response.url() === base + '/api/records/Contact' && response.request().method() === 'POST');
  await page.getByRole('button', { name: 'Create record', exact: true }).click();
  const creation = await created;
  assert.equal(creation.status(), 201, await creation.text());
  const contact = (await creation.json()).record;
  assert.equal(contact.data.organization, 'northstar');
  await page.locator('#record-dialog').waitFor({ state: 'hidden' });
  await page.locator('#view-content').getByRole('button', { name: 'Browser contact', exact: true }).waitFor();
  await openRecord(page, 'Browser contact');
  await page.getByRole('button', { name: 'Edit record', exact: true }).click();
  await page.locator('#field-Contact-name').fill('Updated browser contact');
  await page.locator('#change-reason').fill('Verify schema-derived CRM contact editing');
  const contactEdited = page.waitForResponse((response) => response.url().endsWith(`/api/records/Contact/${contact.id}`) && response.request().method() === 'PATCH');
  await page.getByRole('button', { name: 'Save changes', exact: true }).click();
  assert.equal((await contactEdited).status(), 200);
  await page.locator('#record-dialog').waitFor({ state: 'hidden' });
  const updatedContact = await readRecord('Contact', contact.id);
  assert.equal(updatedContact.data.name, 'Updated browser contact');
  assert.equal(updatedContact.version, contact.version + 1);
  const contactHistory = await readHistory('Contact', contact.id);
  assert.equal(contactHistory.at(-1).reason, 'Verify schema-derived CRM contact editing');
  assert.equal(contactHistory.at(-1).before.data.name, 'Browser contact');
  assert.equal(contactHistory.at(-1).after.data.name, 'Updated browser contact');

  await explore(page, 'Opportunities');
  await openRecord(page, 'Northstar renewal');
  await page.getByRole('button', { name: 'Edit record', exact: true }).click();
  const amount = page.locator('#field-Opportunity-amount_minor');
  assert.equal(await amount.getAttribute('type'), 'number');
  assert.equal(await amount.getAttribute('step'), '1');
  assert.equal(await amount.getAttribute('min'), '0');
  assert.equal(await amount.getAttribute('max'), '1000000000000');
  await amount.fill('2500123');
  await page.locator('#change-reason').fill('Verify exact integer editing without a currency conversion');
  const numericSaved = page.waitForResponse((response) => response.url().endsWith('/api/records/Opportunity/northstar-renewal') && response.request().method() === 'PATCH');
  await page.getByRole('button', { name: 'Save changes', exact: true }).click();
  assert.equal((await numericSaved).status(), 200);
  await page.locator('#record-dialog').waitFor({ state: 'hidden' });
  await page.locator('#view-content').getByText('2500123', { exact: true }).waitFor();
  assert.equal((await readRecord('Opportunity', 'northstar-renewal')).data.amount_minor, 2500123);

  // Reject a skipped pipeline stage atomically, while retaining the user's input.
  const pilot = await readRecord('Opportunity', 'northstar-pilot');
  const pilotHistory = await readHistory('Opportunity', 'northstar-pilot');
  await openRecord(page, 'Northstar pilot');
  await page.getByRole('button', { name: 'Edit record', exact: true }).click();
  await page.locator('#field-Opportunity-stage').selectOption('won');
  await page.locator('#change-reason').fill('Verify that the server rejects an invalid stage transition');
  const rejected = page.waitForResponse((response) => response.url().endsWith('/api/records/Opportunity/northstar-pilot') && response.request().method() === 'PATCH');
  await page.getByRole('button', { name: 'Save changes', exact: true }).click();
  assert.equal((await rejected).status(), 422);
  await page.waitForFunction(() => Boolean(document.querySelector('.record-form .form-error')?.textContent.trim()));
  assert.equal(await page.locator('#field-Opportunity-stage').inputValue(), 'won');
  assert.deepEqual(await readRecord('Opportunity', 'northstar-pilot'), pilot);
  assert.deepEqual(await readHistory('Opportunity', 'northstar-pilot'), pilotHistory);
  await page.screenshot({ path: resolve(artifacts, 'rejected-transition.png'), fullPage: true, animations: 'disabled' });
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await closeRecord(page);

  await page.reload();
  await page.locator('#app').waitFor({ state: 'visible' });
  assert.equal(await page.locator('#view-title').innerText(), 'Your world, ready to explore.');
  assert.equal(await page.locator('#assistant-response').isVisible(), false);
  assert.deepEqual(await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length })), { local: 0, session: 0 });

  // A process restart and a separate mobile browser keep records, but recreate the view.
  await stopServer();
  await startServer();
  const fresh = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  const mobile = await fresh.newPage();
  mobile.on('pageerror', (error) => pageErrors.push(error.message));
  await signIn(mobile);
  assert.equal(await mobile.locator('#view-title').innerText(), 'Your world, ready to explore.');
  const durable = await readRecord('Opportunity', 'northstar-renewal');
  assert.equal(durable.data.stage, 'proposal');
  assert.equal(durable.data.amount_minor, 2500123);
  assert.deepEqual(await readRecord('Contact', contact.id), updatedContact);
  assert.ok(await mobile.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'The mobile client must fit the viewport');
  await mobile.screenshot({ path: resolve(artifacts, 'mobile.png'), fullPage: true, animations: 'disabled' });
  await explore(mobile, 'Opportunities');
  await mobile.screenshot({ path: resolve(artifacts, 'mobile-opportunities.png'), fullPage: true, animations: 'disabled' });
  await openRecord(mobile, 'Northstar renewal');
  assert.ok((await mobile.locator('#record-dialog').innerText()).includes('Proposal'));
  assert.ok(await mobile.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'The mobile record details must fit the viewport');
  await mobile.screenshot({ path: resolve(artifacts, 'mobile-record.png'), fullPage: true, animations: 'disabled' });
  await closeRecord(mobile);

  // The persisted contact can be deleted through the same discovered mobile client.
  await explore(mobile, 'Contacts');
  await openRecord(mobile, 'Updated browser contact');
  await mobile.getByRole('button', { name: 'Delete', exact: true }).click();
  await mobile.locator('#delete-reason').fill('Finish isolated CRM contact verification');
  const contactRemoved = mobile.waitForResponse((response) => response.url().endsWith(`/api/records/Contact/${contact.id}`) && response.request().method() === 'DELETE');
  await mobile.getByRole('button', { name: 'Delete record', exact: true }).click();
  assert.equal((await contactRemoved).status(), 200);
  await mobile.locator('#record-dialog').waitFor({ state: 'hidden' });
  assert.equal((await recordApi(`/api/records/Contact/${contact.id}`)).status, 404);
  const deletedContactHistory = await readHistory('Contact', contact.id);
  assert.equal(deletedContactHistory.length, contactHistory.length + 1);
  assert.equal(deletedContactHistory.at(-1).action, 'delete');
  assert.equal(deletedContactHistory.at(-1).reason, 'Finish isolated CRM contact verification');

  // Changing the same tab's token switches actor and hides every private organization record.
  await signIn(mobile, 'sam');
  await explore(mobile, 'Organizations');
  assert.equal(await mobile.locator('#view-content').getByRole('button', { name: 'Atlas Studio', exact: true }).count(), 0);
  assert.equal((await recordApi('/api/records/Organization/atlas', 'GET', undefined, 'sam')).status, 403);
  const viewerRecords = (await recordApi('/api/records', 'GET', undefined, 'sam')).data.records;
  for (const id of ['atlas', 'atlas-lee', 'atlas-expansion', 'atlas-proposal-review']) assert.ok(!viewerRecords.some((record) => record.id === id));
  await explore(mobile, 'Opportunities');
  assert.equal(await mobile.getByRole('button', { name: '+ New opportunity', exact: true }).count(), 0);
  await openRecord(mobile, 'Northstar renewal');
  assert.equal(await mobile.getByRole('button', { name: 'Edit record', exact: true }).count(), 0);
  const beforeDenial = await readHistory('Opportunity', 'northstar-renewal');
  const denied = await recordApi('/api/records/Opportunity/northstar-renewal', 'PATCH', {
    changes: { amount_minor: 2500124 }, expected_version: durable.version, reason: 'Verify that viewer writes are denied by the server',
  }, 'sam');
  assert.equal(denied.status, 403);
  assert.deepEqual(await readRecord('Opportunity', 'northstar-renewal'), durable);
  assert.deepEqual(await readHistory('Opportunity', 'northstar-renewal'), beforeDenial);
  assert.deepEqual(pageErrors, [], `Browser errors: ${pageErrors.join(', ')}`);
  await fresh.close();
  await context.close();
  console.log(`Passed CRM: shared schema discovery, ${live ? 'real Codex' : 'fixture'} conversation, pipeline board, reviewed mutation/history, Contact CRUD/history, exact numeric editing, rejected transition with no audit, fresh view after reload/restart, mobile, private records and viewer write denial. Screenshots: ${artifacts}`);
} finally {
  await browser?.close();
  await stopServer();
  await rm(dataDir, { recursive: true, force: true });
}
