// Synthetic DOM contract test: no browser, network, persistent storage or real token.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {randomBytes} from 'node:crypto';

class Node {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.events = {};
    this.dataset = {};
    this.classList = {toggle() {}};
    this.textContent = '';
    this.value = '';
  }
  append(...nodes) { this.children.push(...nodes); }
  prepend(...nodes) { this.children.unshift(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(key, value) { this[key] = value; }
  addEventListener(type, fn, options) { (this.events[type] ||= []).push({fn, options}); }
  async fire(type, event = {}) {
    for (const entry of [...this.events[type] || []]) {
      if (entry.options?.once) this.events[type] = this.events[type].filter(e => e !== entry);
      await entry.fn(event);
    }
  }
  showModal() { this.open = true; }
  close() { if (this.open) { this.open = false; this.fire('close'); } }
}
const nodes = new Map();
const get = id => { if (!nodes.has(id)) nodes.set(id, new Node(id)); return nodes.get(id); };
const storage = new Map();
const requests = [];
let disabled = false;
const token = randomBytes(24).toString('hex');
const user = () => ({user_id: 'synthetic-user', display_name: '合成用户', disabled, updated_at: 'synthetic'});
const context = vm.createContext({
  Node, URLSearchParams,
  document: {getElementById: get, createElement: tag => new Node(tag), querySelectorAll: () => []},
  sessionStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
  location: {hash: '', pathname: '/admin'}, window: {addEventListener() {}},
  fetch: async (url, options) => {
    assert.equal(options.headers.Authorization, 'Bearer ' + token);
    assert.equal(options.credentials, 'omit');
    requests.push({url, method: options.method});
    let data;
    if (url === '/api/admin/me') data = {principal: 'synthetic', scopes: ['users:read', 'users:write', 'audit:read']};
    else if (url.startsWith('/api/admin/users?')) data = {total: 1, items: [user()]};
    else if (url.endsWith('/disable')) { disabled = true; data = user(); }
    else if (url.endsWith('/enable')) { disabled = false; data = user(); }
    else if (url.startsWith('/api/admin/users/')) data = {...user(), profile: {}, memory_facts: url.endsWith('?reveal=1') ? ['synthetic memory'] : [{sha: 'hash-only', chars: 16}], usage: {available: false}};
    else throw new Error('Unexpected API request');
    return {ok: true, status: 200, json: async () => data};
  },
});
vm.runInContext(fs.readFileSync('static/admin/admin.js', 'utf8'), context);
assert.equal(get('login').hidden, false);
get('token').value = token;
await get('login-form').fire('submit', {preventDefault() {}});
assert.equal(storage.get('adminToken'), token);
assert.equal(get('token').value, '');
assert.equal(get('console').hidden, false);
assert.equal(get('nav').children.filter(n => n['aria-disabled'] === 'true').length, 2);
await vm.runInContext("location.hash = '#users'; render()", context);
await vm.runInContext("userDetail('synthetic-user')", context);
const all = node => [node, ...node.children.flatMap(all)];
const byText = text => all(get('detail')).find(n => n.tagName === 'button' && n.textContent === text);
assert(all(get('detail')).some(n => n.textContent.includes('hash-only')));
assert(!all(get('detail')).some(n => n.textContent.includes('synthetic memory')));

const disable = byText('禁用用户').fire('click');
assert.equal(get('confirm').open, true);
assert.equal(get('confirm-ok').disabled, true);
assert(!requests.some(r => r.method === 'POST'));
get('confirm').returnValue = 'cancel';
get('confirm').close();
await disable;
assert(!requests.some(r => r.method === 'POST'));

const confirmed = byText('禁用用户').fire('click');
get('confirm-input').value = '确认';
await get('confirm-input').fire('input');
assert.equal(get('confirm-ok').disabled, false);
get('confirm').returnValue = 'yes';
get('confirm').close();
await confirmed;
assert(disabled);
assert.equal(requests.filter(r => r.method === 'POST').length, 1);
assert(byText('解禁用户'));

const reveal = byText('显示记忆原文').fire('click');
assert(!requests.some(r => r.url.endsWith('?reveal=1')));
get('confirm-input').value = '确认';
await get('confirm-input').fire('input');
get('confirm').returnValue = 'yes';
get('confirm').close();
await reveal;
assert(requests.some(r => r.url.endsWith('?reveal=1')));
assert(all(get('detail')).some(n => n.textContent.includes('synthetic memory')));
get('logout').onclick();
assert.equal(storage.size, 0);
assert.equal(get('console').hidden, true);
assert.equal(get('detail').children.length, 0);
