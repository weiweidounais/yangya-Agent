// Real API, flow, store, typing/error UI and card renderers; synthetic transport and clock.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {webcrypto} from 'node:crypto';

const [scenario, entrypoint] = process.argv.slice(2);
const source = name => fs.readFileSync(`static/js/${name}.js`, 'utf8');
const clean = text => text.replace(/import\s+[\s\S]*?from\s+"[^"]+";/g, '')
  .replace(/export \{[^}]+\} from "[^"]+";/g, '').replace(/\bexport /g, '');
let now = 0, serial = 0, posts = 0, polls = 0, requestId, finished = false, failure;
const timers = new Map(), rendered = [], storage = new Map();
function setTimer(callback, delay) {
  const id = ++serial;
  timers.set(id, {at: now + delay, callback});
  return id;
}
function h(tag, props = {}, children = []) {
  return {tag, ...props, textContent: props.text || '', children: children.filter(x => x != null),
    classList: {add() {}, toggle() {}, remove() {}},
    append(...nodes) { this.children.push(...nodes); },
    replaceChildren(...nodes) { this.children = nodes; },
    setAttribute(key, value) { this[key] = value; },
    addEventListener() {},
    remove() { const i = rendered.indexOf(this); if (i >= 0) rendered.splice(i, 1); },
  };
}
const flatten = node => typeof node === 'string' ? [] : [node, ...(node.children || []).flatMap(flatten)];
const allNodes = () => rendered.flatMap(flatten);
const typingNodes = () => allNodes().filter(node => (node.class || '').includes('msg-typing'));
const textOf = node => typeof node === 'string' ? node :
  [node.textContent || '', ...(node.children || []).map(textOf)].join(' ');
const payload = {
  status: 'awaiting_confirm', thread_id: 'synthetic-thread', awaiting_confirm: true,
  data: {intent: 'draft', needs_confirm: true,
    draft: {kind: 'email', title: '虚构的请假邮件标题', body: '虚构的请假邮件正文', notes: ['已按你的语气调整']},
    task_card: {type: 'draft_confirm', title: '请确认草稿', kind: 'email', draft_title: '虚构的请假邮件标题', body: '虚构的请假邮件正文'},
    messages: [{role: 'assistant', content: '虚构草稿写好了'}]},
};
const reply = (body, status = 200) => ({ok: status < 400, status,
  text: async () => JSON.stringify(body), headers: {get: () => 'synthetic-trace'}});
const brokenWait = signal => new Promise((_, reject) => {
  signal.addEventListener('abort', () => reject(Object.assign(new Error('synthetic timeout'), {name: 'AbortError'})));
});
async function fetch(path, options) {
  if (path === '/api/chat' || path === '/api/draft') {
    assert.equal(path, entrypoint === 'chat' ? '/api/chat' : '/api/draft');
    posts++;
    assert.equal(posts, 1, 'recovery must not rerun the model/turn');
    const body = JSON.parse(options.body);
    requestId = body.request_id;
    assert.match(requestId, /^[a-f0-9]{32}$/);
    if (scenario === 'fast') return reply(payload);
    if (scenario === 'server_error') return reply({...payload, status: 'error', data: {error: 'synthetic failure'}});
    if (scenario === 'network') throw new TypeError('synthetic closed connection');
    if (scenario === 'proxy') return reply({}, 504);
    if (scenario === 'truncated') return {...reply(payload), text: async () => '{'};
    if (scenario === 'body_timeout') return {...reply(payload), text: () => brokenWait(options.signal)};
    return brokenWait(options.signal);
  }
  if (path.startsWith('/api/turns/')) {
    assert.equal(path, `/api/turns/${requestId}`);
    polls++;
    if (scenario === 'failed') return reply({status: 'failed', code: 'turn_failed'});
    if (scenario === 'worker_timeout') return reply({status: 'failed', code: 'turn_timeout'});
    if (scenario === 'expired') return reply({}, 401);
    if (scenario === 'poll_hangs') return brokenWait(options.signal);
    if (scenario === 'missing') return reply({}, 404);
    if (scenario === 'offline') throw new TypeError('synthetic offline');
    if (scenario === 'transient_poll' && polls === 1) return reply({}, 503);
    if (scenario === 'transient_poll' && polls === 2) throw new TypeError('synthetic poll disconnect');
    // Intermediate redrafts do not constitute a terminal response.
    if (now < 210000 || scenario === 'hard_timeout') return reply({status: 'running'});
    return reply({status: 'completed', response: {...payload, traceId: 'original-turn-trace'}});
  }
  throw new Error(`unexpected synthetic request ${path}`);
}
const sandbox = {
  console, Date: class extends Date { static now() { return now; } }, Map, Set,
  crypto: webcrypto, Uint8Array, AbortController, fetch,
  window: {setTimeout: setTimer, clearTimeout: id => timers.delete(id), addEventListener() {}},
  localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)},
  currentUserId: () => 'synthetic-user', sessionReady: () => true, ensureSession: async () => {},
  sessionExpired: () => Object.assign(new Error('expired'), {kind: 'session', status: 401}),
  setViewTz() {}, h, icon: () => null, avatar: () => null, debugBlock: () => null,
  nextId: () => `entry-${++serial}`, announce() {},
  append: node => { rendered.push(node); return node; },
  userMessage: data => h('p', {text: data.text}),
  collapsible: text => h('div', {text}), feedbackBar: () => null,
  getProfile: () => ({}), setProfile() {}, DEBUG: false,
};
for (const name of ['flows', 'confirm', 'cards']) {
  for (const match of source(name).matchAll(/import\s+\{([\s\S]*?)\}\s+from/g)) {
    for (const item of match[1].split(',').map(s => s.trim()).filter(Boolean)) {
      if (!(item in sandbox)) sandbox[item] = () => {};
    }
  }
}
const ctx = vm.createContext(sandbox);
function load(text, exports) {
  Object.assign(sandbox, vm.runInContext(`(() => {${clean(text)}\nreturn {${exports.join(',')}};})()`, ctx));
}
load(source('api'), ['api', 'friendlyError', 'debugText']);
load(source('store'), ['store']);
load(source('text'), ['draftKindLabel', 'splitList', 'cleanAssistantText']);
load(source('cards'), ['continueCard', 'askCard']);
load(source('confirm'), ['confirmCard', 'draftFromResponse']);
const thread = source('thread');
load(thread.slice(thread.indexOf('export function assistantMessage('), thread.indexOf('export function renderHero(')),
  ['assistantMessage', 'errorMessage', 'typing']);
load(source('flows'), ['sendComposer', 'requestDraft']);

const turn = entrypoint === 'chat'
  ? sandbox.sendComposer({text: '帮我写一封请假邮件，明天请一天假', attachments: []})
  : sandbox.requestDraft('帮我写一封请假邮件，明天请一天假');
turn.then(() => { finished = true; }, error => { failure = error; finished = true; });
assert.equal(typingNodes().length, 1);
for (let step = 0; !finished && step < 1000; step++) {
  // Flush promise continuations before advancing simulated browser time.
  await new Promise(resolve => setImmediate(resolve));
  if (finished) break;
  const [id, timer] = [...timers].sort((a, b) => a[1].at - b[1].at)[0] || [];
  assert.ok(timer, 'a pending turn must have a bounded wait');
  now = timer.at;
  if (now < 210000) assert.equal(typingNodes().length, 1, 'typing stays during multi-step work');
  timers.delete(id);
  timer.callback();
}
if (failure) throw failure;
assert.ok(finished, 'turn must terminate');
assert.equal(typingNodes().length, 0, 'terminal success/error removes the actual typing row');
assert.equal(posts, 1);
const success = ['fast', 'timeout', 'network', 'proxy', 'truncated', 'body_timeout', 'transient_poll'].includes(scenario);
if (success) {
  if (scenario !== 'fast') {
    assert.ok(now >= 210000, 'final response must arrive beyond the normal 150-second wait');
    assert.ok(polls >= 2);
  }
  const entries = sandbox.store.entries();
  assert.deepEqual(Array.from(entries, e => e.type),
    [...(entrypoint === 'chat' ? ['user'] : []), 'assistant', 'confirm']);
  const confirm = entries.find(e => e.type === 'confirm');
  assert.equal(confirm.data.threadId, 'synthetic-thread');
  // Objects cross the vm realm: compare by value, not prototype.
  assert.equal(JSON.stringify(confirm.data.draft),
    JSON.stringify({kind: 'email', title: '虚构的请假邮件标题', body: '虚构的请假邮件正文', notes: ['已按你的语气调整']}));
  assert.equal(confirm.data.decision, null);
  const text = rendered.map(textOf).join(' ');
  for (const expected of ['虚构草稿写好了', '待你确认：邮件草稿', '虚构的请假邮件标题', '虚构的请假邮件正文', '已按你的语气调整', '确认定稿', '拒绝']) {
    assert.ok(text.includes(expected), `missing rendered output: ${expected}`);
  }
  assert.equal(allNodes().filter(n => (n.class || '').includes('card-confirm')).length, 1);
} else {
  const alert = allNodes().find(node => node.role === 'alert');
  assert.ok(alert, 'terminal failures must render a clear error');
  assert.ok(textOf(alert).includes(scenario === 'expired' ? '稍后再试' : '重试'));
  if (scenario !== 'expired') assert.ok(allNodes().some(node => node.tag === 'button' && node.text === '重试'));
  assert.equal(sandbox.store.entries().filter(e => e.type !== 'user').length, 0);
  if (['hard_timeout', 'poll_hangs', 'missing', 'offline'].includes(scenario)) assert.equal(now, 600000);
}
// A late original response/recovery continuation must never append a second result.
await new Promise(resolve => setImmediate(resolve));
assert.equal(typingNodes().length, 0);
console.log(`${entrypoint}/${scenario}: complete at simulated ${now} ms`);
