const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require(process.env.TYPESCRIPT_PATH || 'typescript');
const source = ts.transpileModule(fs.readFileSync(path.join(__dirname, '../src/api.ts'), 'utf8'), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;

function client(handler) {
  const exports = {}, events = [];
  const sandbox = { exports, Headers, AbortSignal, CustomEvent, Error, Promise,
    fetch: handler, window: { dispatchEvent: event => events.push(event) } };
  vm.runInNewContext(source, sandbox, { filename: 'api.js' });
  return { ...exports, events };
}
function response(data, status = 200, code = null) {
  return { ok: status < 400, status, json: async () => ({ data, error_code: code, message: code || '', status: status < 400 ? 'ok' : 'error' }) };
}
const operation = { level: 'red', resource_kind: 'proposal', resource_id: 'p1', action: 'transfer', amount_minor: 200000, including_this_minor: 200000, terms: {} };

test('read-only requests do not ask for confirmation', async () => {
  const c = client(async () => response({ balance_minor: 10000 }));
  let prompts = 0;
  c.setSecurityGate(async () => { prompts++; });
  assert.equal((await c.api('/api/accounts')).balance_minor, 10000);
  assert.equal(prompts, 0);
});

test('token issuance waits for independent security confirmation', async () => {
  const calls = [];
  const c = client(async path => { calls.push(path); return response(path.includes('/security/') ? operation : { confirmation_token: 'test-token' }); });
  c.setSecurityGate(async details => { assert.equal(details.level, 'red'); calls.push('USER_VERIFIED'); });
  await c.api('/api/proposals/p1/confirmation-token', { method: 'POST' });
  assert.deepEqual(calls, ['/api/security/operations/proposal/p1', 'USER_VERIFIED', '/api/proposals/p1/confirmation-token']);
});

test('unavailable confirmation UI fails closed', async () => {
  let requests = 0;
  const c = client(async () => { requests++; return response({}); });
  await assert.rejects(c.api('/api/proposals/p1/confirmation-token', { method: 'POST' }), error => error.code === 'SECURITY_UI_UNAVAILABLE');
  assert.equal(requests, 0);
});

test('cancelling verification never issues a token', async () => {
  const calls = [];
  const c = client(async path => { calls.push(path); return response(operation); });
  c.setSecurityGate(async () => { throw Error('cancelled'); });
  await assert.rejects(c.api('/api/proposals/p1/confirmation-token', { method: 'POST' }));
  assert.equal(calls.length, 1);
});

test('changing user while confirming invalidates the pending operation', async () => {
  const calls = [];
  const c = client(async path => { calls.push(path); return response(operation); });
  c.setExpectedUser('u1');
  c.setSecurityGate(async () => c.setExpectedUser('u2'));
  await assert.rejects(c.api('/api/proposals/p1/confirmation-token', { method: 'POST' }), error => error.code === 'SESSION_CHANGED');
  assert.equal(calls.length, 1);
});

for (const [route, kind] of [
  ['/api/wealth/orders/x/confirmation-token', 'wealth'],
  ['/api/card-management/requests/x/confirmation-token', 'card'],
  ['/api/birthdays/requests/x/confirmation-token', 'birthday'],
  ['/api/subscriptions/requests/x/confirmation-token', 'subscription'],
  ['/api/schedules/x/token', 'schedule'],
  ['/api/collections/x/confirmation-token', 'collection'],
]) {
  test(`${kind} uses the common security gate`, async () => {
    const calls = [];
    const c = client(async path => { calls.push(path); return response(operation); });
    c.setSecurityGate(async () => {});
    await c.api(route, { method: 'POST' });
    assert.equal(calls[0], `/api/security/operations/${kind}/x`);
    assert.equal(calls[1], route);
  });
}

test('API denial cannot become a success receipt', async () => {
  const c = client(async () => response(null, 423, 'SECURITY_LOCKED'));
  await assert.rejects(c.api('/api/proposals/p1/confirm', { method: 'POST' }), error => error.code === 'SECURITY_LOCKED');
});

test('subscription query retains the server confirmation requirement', async () => {
  const c = client(async () => response(null, 403, 'SUBSCRIPTION_CONFIRMATION_REQUIRED'));
  await assert.rejects(c.api('/api/subscriptions'), error => error.code === 'SUBSCRIPTION_CONFIRMATION_REQUIRED');
});

test('expected-user header is sent with every request', async () => {
  let expected;
  const c = client(async (_path, options) => { expected = options.headers.get('X-FinAgent-Expected-User'); return response({}); });
  c.setExpectedUser('u1'); await c.api('/api/accounts');
  assert.equal(expected, 'u1');
});
