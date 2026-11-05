// Execute real identity/API/store modules with synthetic responses and storage.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = name => fs.readFileSync(`static/js/${name}.js`, 'utf8')
  .replace(/import\s+[\s\S]*?from\s+"[^"]+";/g, '')
  .replace(/export \{[^}]+\} from "[^"]+";/g, '').replace(/\bexport /g, '');
const storage = new Map();
const localStorage = {
  getItem: key => storage.get(key) || null,
  setItem: (key, value) => storage.set(key, value),
  removeItem: key => storage.delete(key),
};
let sessionStatus = 200, serverOwner = 'synthetic-new', decisionStatus = 200, profileStatus = 200;
let sessionCalls = 0;
const requests = [];
function browser() {
  const nodes = new Map();
  let navigation;
  function element() {
    return {
      children: [], setAttribute() {}, addEventListener() {}, focus() {},
      append(...children) { this.children.push(...children); },
      prepend(child) { nodes.set(child.id, child); },
      remove() { nodes.delete(this.id); },
    };
  }
  const document = {getElementById: id => nodes.get(id), createElement: element,
    querySelector: () => document.body, body: element()};
  const ctx = vm.createContext({console, Map, Set, AbortController, localStorage, document,
    window: {localStorage, setTimeout, clearTimeout, addEventListener() {},
      location: {assign: path => navigation = path}},
    h: (tag, props = {}, children = []) => ({tag, ...props, children: children.filter(Boolean),
      classList: {add() {}, toggle() {}, remove() {}},
      append(...items) { this.children.push(...items); },
      replaceChildren(...items) { this.children = items; },
      setAttribute(key, value) { this[key] = value; },
      addEventListener() {}}),
    nextId: () => 'synthetic-title', debugBlock: () => null, icon: () => null,
    setViewTz() {},
    fetch: async (path, opts) => {
      requests.push(path);
      assert.equal(opts.credentials, 'same-origin');
      let status, data;
      if (path === '/api/session') {
        sessionCalls++;
        status = sessionStatus;
        data = {data: {user_id: serverOwner}};
      } else if (path.startsWith('/api/confirm/')) {
        status = decisionStatus;
        data = {data: {decision: null}};
      } else if (path.startsWith('/api/profile/')) {
        status = profileStatus;
        data = {data: {profile: {name: '测试用户甲'}}};
      } else throw new Error(`Unexpected request: ${path}`);
      if (status >= 400) data = {detail: {code: 'unauthenticated'}};
      return {ok: status < 400, status, json: async () => data,
        text: async () => JSON.stringify(data), headers: {get: () => ''}};
    },
  });
  function load(name, exports) {
    Object.assign(ctx, vm.runInContext(`(() => {${source(name)}; return {${exports}};})()`, ctx));
  }
  load('identity', 'ensureSession, restart, sessionExpired, sessionReady, sessionState, currentUserId');
  load('api', 'api');
  load('store', 'store');
  load('text', 'draftKindLabel');
  load('confirm', 'confirmCard');
  for (const name of ['flows', 'confirm']) {
    for (const match of fs.readFileSync(`static/js/${name}.js`, 'utf8').matchAll(/import\s+\{([\s\S]*?)\}\s+from/g)) {
      for (const item of match[1].split(',').map(s => s.trim()).filter(Boolean)) {
        if (!(item in ctx)) ctx[item] = () => {};
      }
    }
  }
  load('flows', 'buildCard');
  return {ctx, nodes, navigation: () => navigation};
}

// An old account's cached transcript must not be adopted with the new identity.
storage.set('ja.session_uid', 'synthetic-old');
storage.set('ja.transcript.v1:synthetic-old', JSON.stringify([
  {id: 'old', type: 'confirm', data: {threadId: 'synthetic-old-thread',
    draft: {kind: 'email', title: '旧标题', body: '旧正文', notes: []}, decision: null}},
]));
let page = browser();
await assert.rejects(page.ctx.ensureSession(), e => e.kind === 'session');
assert(page.nodes.has('sessionBlocked'));
assert.equal(page.ctx.currentUserId(), 'synthetic-old');
page.ctx.restart();
assert.equal(page.navigation(), '/');
page = browser();
await page.ctx.ensureSession();
assert.equal(page.ctx.currentUserId(), 'synthetic-new');
assert.equal(page.ctx.store.entries().length, 0);
assert(storage.has('ja.transcript.v1:synthetic-old'), 'prior account records stay isolated');

// Resource-level 403/404 on a decision lookup never mint, retry or expire the session.
const callsBefore = sessionCalls;
for (const status of [403, 404]) {
  decisionStatus = status;
  for (const id of ['synthetic-old-thread', 'synthetic-fresh-thread']) {
    await assert.rejects(page.ctx.api.decision(id), e => e.status === status && e.kind === 'http');
    assert(page.ctx.sessionReady(), 'a decision lookup failure must leave the composer usable');
    assert(!page.nodes.has('sessionBlocked'));
    await page.ctx.api.profile();
  }
}
assert.equal(sessionCalls, callsBefore, 'resource errors must not mint or retry sessions');

// Reloading a persisted transcript renders the saved draft card from the same owner.
decisionStatus = 200;
page.ctx.store.add({id: 'fresh', type: 'confirm',
  data: {threadId: 'synthetic-fresh-thread', draft: {kind: 'message', title: '', body: '虚构的消息正文', notes: []},
    decision: null, edits: null, version: 1}});
for (let i = 0; i < 2; i++) {
  page = browser();
  await page.ctx.ensureSession();
  const entry = page.ctx.store.entries()[0];
  assert.equal(entry.data.threadId, 'synthetic-fresh-thread');
  const result = await page.ctx.api.decision(entry.data.threadId);
  assert.equal(result.data.decision, null);
  const card = page.ctx.buildCard(entry, true);
  const flatten = node => [node, ...(node.children || []).flatMap(flatten)];
  assert.ok(flatten(card).some(node => node.text === '待你确认：消息草稿'));
  assert.ok(flatten(card).some(node => node.class === 'draft-text' && node.text === '虚构的消息正文'));
  assert(page.ctx.sessionReady());
  assert(!page.nodes.has('sessionBlocked'));
}

// A protected 401 (profile or decision) still raises the wall.
for (const [set, call] of [[s => { profileStatus = s; }, () => page.ctx.api.profile()],
                           [s => { decisionStatus = s; }, () => page.ctx.api.decision('synthetic-fresh-thread')]]) {
  set(401);
  await assert.rejects(call(), e => e.kind === 'session');
  assert(page.nodes.has('sessionBlocked'));
  set(200);
  page.ctx.restart();
  page = browser();
  await page.ctx.ensureSession();
  assert(page.ctx.sessionReady());
  assert(!page.nodes.has('sessionBlocked'));
}

// Explicit authentication failures still render the wall; 503 keeps its retry copy.
for (const [status, state] of [[401, 'expired'], [503, 'unavailable']]) {
  sessionStatus = status;
  page = browser();
  await assert.rejects(page.ctx.ensureSession(), e => e.kind === 'session' && e.code === state);
  assert.equal(page.ctx.sessionState(), state);
  assert(page.nodes.has('sessionBlocked'));
}
assert(requests.includes('/api/confirm/synthetic-fresh-thread'));
