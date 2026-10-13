// Verify a real browser against an isolated world. --live uses the actual Codex provider.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, readFile, rm, mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import { createInterface } from 'node:readline';
import { chromium } from 'playwright';

const live = process.argv.includes('--live');
const dataDir = await mkdtemp(resolve(tmpdir(), 'world-browser-'));
const artifacts = resolve('artifacts', live ? 'live' : 'browser');
await mkdir(artifacts, { recursive: true });
let server;
let browser;
let base;
let tokens;
const pageErrors = [];

async function startServer() {
  let diagnostic = '';
  server = spawn(process.env.WORLD_PYTHON || 'python3', ['tests/browser_server.py', '--data-dir', dataDir, ...(live ? ['--live'] : [])], { cwd: resolve('.'), stdio: ['ignore', 'pipe', 'pipe'] });
  server.stderr.on('data', (chunk) => { diagnostic = (diagnostic + chunk).slice(-6000); });
  const lines = createInterface({ input: server.stdout });
  let timer;
  const firstLine = new Promise((fulfill, reject) => {
    lines.once('line', fulfill);
    server.once('error', reject);
    server.once('exit', (code) => reject(new Error(`Fixture exited ${code}: ${diagnostic}`)));
    timer = setTimeout(() => reject(new Error(`Fixture startup timed out: ${diagnostic}`)), 15000);
  });
  const ready = JSON.parse(await firstLine.finally(() => { clearTimeout(timer); lines.close(); }));
  base = `http://127.0.0.1:${ready.port}`;
  tokens = JSON.parse(await readFile(resolve(dataDir, 'access.json'), 'utf8'));
}

async function stopServer() {
  if (!server || server.exitCode !== null) return;
  const exited = once(server, 'exit');
  server.kill('SIGTERM');
  await exited;
  server = null;
}

async function recordApi(path, method = 'GET', body, actor = 'ben') {
  const response = await fetch(base + path, {
    method,
    headers: { Authorization: `Bearer ${tokens[actor]}`, ...(body ? { 'Content-Type': 'application/json' } : {}) },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  return { status: response.status, data: await response.json() };
}

async function signIn(page, actor = 'ben') {
  await page.goto(`${base}/#token=${tokens[actor]}`);
  await page.waitForFunction(() => location.hash === '' && !document.getElementById('app').hidden);
  assert.equal(new URL(page.url()).hash === '', true, 'The sign-in credential must leave the address bar');
  assert.equal((await page.locator('#actor-name').innerText()).toLowerCase(), actor, 'A same-tab sign-in must switch actors');
}

async function ask(page, message) {
  await page.locator('#intent-input').fill(message);
  const completed = page.waitForResponse((response) => response.url() === base + '/client/intent' && response.request().method() === 'POST', { timeout: 140000 });
  await page.getByRole('button', { name: 'Send message', exact: true }).click();
  const response = await completed;
  const plan = await response.json();
  assert.equal(response.status(), 200, JSON.stringify(plan));
  await page.waitForFunction(() => document.getElementById('intent-form').getAttribute('aria-busy') === 'false');
  console.log(`${live ? 'Live Codex' : 'Fixture'}: ${message} -> ${plan.view.layout}, ${plan.view.record_ids.length} records`);
  return plan;
}

try {
  await startServer();
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  page.on('pageerror', (error) => pageErrors.push(error.message));
  await signIn(page);
  await page.screenshot({ path: resolve(artifacts, 'welcome.png'), fullPage: true, animations: 'disabled' });
  assert.equal(await page.locator('#view-title').innerText(), 'Your world, ready to explore.');

  const attention = await ask(page, 'Show me what needs attention.');
  assert.equal(attention.view.type, 'Task');
  assert.ok(attention.view.record_ids.includes('documentation'));
  if (attention.view.layout === 'list') assert.equal(await page.locator('.list-row h3').first().innerText(), 'Write documentation');
  const visual = await ask(page, 'Make this more visual.');
  assert.ok(['board', 'overview'].includes(visual.view.layout));
  assert.deepEqual(new Set(visual.view.record_ids), new Set(attention.view.record_ids));
  await page.screenshot({ path: resolve(artifacts, 'visual.png'), fullPage: true, animations: 'disabled' });

  const website = await ask(page, 'Show the website project instead.');
  assert.ok(website.view.record_ids.length > 0);
  for (const id of website.view.record_ids) {
    const result = await recordApi(`/api/records/${website.view.type}/${id}`);
    assert.equal(result.status, 200);
    assert.ok(result.data.record.data.project === 'website' || website.view.type === 'Project' && id === 'website');
  }
  const proposal = await ask(page, 'Mark the documentation task as complete.');
  assert.ok(proposal.proposals.some((change) => change.type === 'Task' && change.id === 'documentation' && change.changes.status === 'done'));
  let documentation = (await recordApi('/api/records/Task/documentation')).data.record;
  assert.equal(documentation.data.status, 'todo', 'Generation alone must not mutate a record');
  const saved = page.waitForResponse((response) => response.url() === base + '/client/apply');
  await page.getByRole('button', { name: 'Confirm change', exact: true }).click();
  assert.equal((await saved).status(), 200);
  await page.getByText('✓ Saved to the shared world', { exact: true }).waitFor();
  documentation = (await recordApi('/api/records/Task/documentation')).data.record;
  assert.equal(documentation.data.status, 'done');

  await page.locator('#type-navigation').getByRole('button', { name: 'Explore Tasks', exact: true }).click();
  await page.locator('#view-content').getByRole('button', { name: 'Write documentation', exact: true }).click();
  await page.locator('#record-dialog').getByRole('button', { name: 'History', exact: true }).click();
  await page.locator('.detail-history .timeline-event').first().waitFor();
  const history = await page.locator('.detail-history').innerText();
  assert.ok(history.includes('Mark the documentation task as complete.'));
  assert.match(history, /Status.*done/i);
  assert.ok(await page.locator('.detail-history time[datetime]').count() >= 2);
  await page.getByRole('button', { name: 'Close record details', exact: true }).click();

  // Drive the schema-derived editor through creation, editing and deletion.
  await page.getByRole('button', { name: '+ New task', exact: true }).click();
  await page.locator('#field-Task-title').fill('Browser-created task');
  await page.locator('#field-Task-project').selectOption('website');
  await page.locator('#change-reason').fill('Verify schema-derived creation');
  const created = page.waitForResponse((response) => response.url() === base + '/api/records/Task' && response.request().method() === 'POST');
  await page.getByRole('button', { name: 'Create record', exact: true }).click();
  const creation = await created;
  assert.equal(creation.status(), 201, await creation.text());
  const createdRecord = (await creation.json()).record;
  await page.locator('#view-content').getByRole('button', { name: 'Browser-created task', exact: true }).click();
  await page.getByRole('button', { name: 'Edit record', exact: true }).click();
  await page.locator('#field-Task-status').selectOption('doing');
  await page.locator('#change-reason').fill('Verify schema-derived editing');
  const edited = page.waitForResponse((response) => response.url().endsWith(`/api/records/Task/${createdRecord.id}`) && response.request().method() === 'PATCH');
  await page.getByRole('button', { name: 'Save changes', exact: true }).click();
  assert.equal((await edited).status(), 200);
  await page.locator('#view-content').getByRole('button', { name: 'Browser-created task', exact: true }).click();
  await page.getByRole('button', { name: 'Delete', exact: true }).click();
  await page.locator('#delete-reason').fill('Finish isolated browser verification');
  const removed = page.waitForResponse((response) => response.url().endsWith(`/api/records/Task/${createdRecord.id}`) && response.request().method() === 'DELETE');
  await page.getByRole('button', { name: 'Delete record', exact: true }).click();
  assert.equal((await removed).status(), 200);
  assert.equal((await recordApi(`/api/records/Task/${createdRecord.id}`)).status, 404);
  const deletedHistory = (await recordApi(`/api/history/Task/${createdRecord.id}`)).data.events;
  assert.equal(deletedHistory.at(-1).action, 'delete');

  // A second writer must cause a conflict, preserve entered edits, and refresh reopening.
  await page.locator('#view-content').getByRole('button', { name: 'Write documentation', exact: true }).click();
  await page.getByRole('button', { name: 'Edit record', exact: true }).click();
  const current = (await recordApi('/api/records/Task/documentation')).data.record;
  const concurrent = await recordApi('/api/records/Task/documentation', 'PATCH', { changes: { description: 'Changed by a second client' }, expected_version: current.version, reason: 'Verify conflict recovery' });
  assert.equal(concurrent.status, 200);
  await page.locator('#field-Task-status').selectOption('doing');
  await page.locator('#change-reason').fill('This edit is intentionally stale');
  const conflicted = page.waitForResponse((response) => response.url().endsWith('/api/records/Task/documentation') && response.request().method() === 'PATCH');
  await page.getByRole('button', { name: 'Save changes', exact: true }).click();
  assert.equal((await conflicted).status(), 409);
  await page.locator('.record-form .form-error').filter({ hasText: 'Close the editor' }).waitFor();
  assert.equal(await page.locator('#field-Task-status').inputValue(), 'doing');
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  assert.ok((await page.locator('#record-dialog').innerText()).includes(`Version ${concurrent.data.record.version}`));
  await page.getByRole('button', { name: 'Close record details', exact: true }).click();

  await page.reload();
  await page.locator('#app').waitFor({ state: 'visible' });
  assert.equal(await page.locator('#view-title').innerText(), 'Your world, ready to explore.');
  assert.equal(await page.locator('#assistant-response').isVisible(), false);
  assert.deepEqual(await page.evaluate(() => ({ local: localStorage.length, session: sessionStorage.length })), { local: 0, session: 0 });

  // Restart the process and open an entirely separate browser client.
  await stopServer();
  await startServer();
  const fresh = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  const mobile = await fresh.newPage();
  mobile.on('pageerror', (error) => pageErrors.push(error.message));
  await signIn(mobile);
  assert.equal(await mobile.locator('#view-title').innerText(), 'Your world, ready to explore.');
  assert.equal((await recordApi('/api/records/Task/documentation')).data.record.data.status, 'done');
  await mobile.screenshot({ path: resolve(artifacts, 'mobile.png'), fullPage: true, animations: 'disabled' });
  assert.ok(await mobile.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'The mobile page must fit the viewport');
  await mobile.getByRole('button', { name: 'Open navigation', exact: true }).click();
  await mobile.locator('#type-navigation').getByRole('button', { name: 'Explore Tasks', exact: true }).click();
  await mobile.locator('#view-content').getByRole('button', { name: 'Write documentation', exact: true }).click();
  assert.ok((await mobile.locator('#record-dialog').innerText()).includes('Done'));
  await mobile.getByRole('button', { name: 'Close record details', exact: true }).click();
  await mobile.getByRole('button', { name: 'Open navigation', exact: true }).click();
  await mobile.getByRole('button', { name: 'Discover schema', exact: true }).click();
  assert.ok((await mobile.locator('#schema-dialog').innerText()).includes("actor.role == 'admin'"));
  await mobile.getByRole('button', { name: 'Close schema', exact: true }).click();

  await signIn(mobile, 'sam');
  await mobile.getByRole('button', { name: 'Open navigation', exact: true }).click();
  await mobile.locator('#type-navigation').getByRole('button', { name: 'Explore Projects', exact: true }).click();
  assert.equal(await mobile.locator('#view-content').getByRole('button', { name: 'World', exact: true }).count(), 0);
  await mobile.getByRole('button', { name: 'Open navigation', exact: true }).click();
  await mobile.locator('#type-navigation').getByRole('button', { name: 'Explore Tasks', exact: true }).click();
  assert.equal(await mobile.getByRole('button', { name: '+ New task', exact: true }).count(), 0);
  await mobile.locator('#view-content').getByRole('button', { name: 'Write documentation', exact: true }).click();
  assert.equal(await mobile.getByRole('button', { name: 'Edit record', exact: true }).count(), 0);
  assert.deepEqual(pageErrors, [], `Browser errors: ${pageErrors.join(', ')}`);
  await fresh.close();
  await context.close();
  console.log(`Passed: conversation, layout changes, scoped records, durable mutation/history, generic CRUD, stale edit recovery, fresh client, server restart, mobile, viewer permissions. Screenshots: ${artifacts}`);
} finally {
  await browser?.close();
  await stopServer();
  await rm(dataDir, { recursive: true, force: true });
}
