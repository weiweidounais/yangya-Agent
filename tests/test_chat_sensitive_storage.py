"""Exercise composer + transcript persistence in Node, without HTTP or a browser."""
import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("message,prompt,sensitive", [
    ("密码：synthetic-only", "", True),
    ("password = synthetic-only", "", True),
    ("password synthetic-only", "", True),
    ("验证码123456", "", True),
    ("OTP: 123456", "", True),
    ("token: synthetic-only", "", True),
    ("API key = synthetic-only", "", True),
    ("access_token: synthetic-only", "", True),
    ("cookie: synthetic-only", "", True),
    ("secret: synthetic-only", "", True),
    ("1234", "请输入验证码", True),
    ("12345678", "Enter your OTP", True),
    ("123456", "请输入密码", True),
    ("我的进度怎么样", "", False),
    ("帮我修改密码", "", False),
    ("How do I reset my password?", "", False),
    ("123456", "", False),
    ("123456", "期望薪资是多少？", False),
    ("123", "请输入验证码", False),
    ("123456789", "请输入验证码", False),
])
def test_sensitive_composer_text_only_lives_in_memory(message, prompt, sensitive, failure):
    script = r'''
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
const {message, prompt, sensitive, failure} = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = name => fs.readFileSync(`static/js/${name}.js`, 'utf8');
const clean = s => s.replace(/import\s+[\s\S]*?from\s+"[^"]+";/g, '').replace(/\bexport /g, '');
const storage = new Map(), writes = [], calls = [], displayed = [], replies = [], errors = [];
let serial = 0, rejectWrite = false;
const sandbox = {
  console, Date, Map, Set,
  currentUserId: () => 'synthetic-user', sessionReady: () => true,
  localStorage: {
    getItem: key => storage.get(key),
    setItem: (key, value) => {
      writes.push(value);
      if (rejectWrite) { rejectWrite = false; throw new Error('synthetic quota'); }
      storage.set(key, value);
    },
  },
  nextId: () => `entry-${++serial}`,
  userMessage: data => { displayed.push(data.text); },
  assistantMessage: text => { replies.push(text); },
  typing: () => ({done() {}}),
  friendlyError: () => ({text: 'synthetic network error', retry: true}),
  errorMessage: data => errors.push(data),
  api: {chat: async text => {
    calls.push(text);
    // Inspect the write made by echoUser, before any server redaction exists.
    if (sensitive) assert.ok(writes.every(value => !value.includes(message)));
    if (failure && calls.length === 1) throw new Error('synthetic network error');
    return {status: 'chat', data: {messages: [{role: 'assistant', content: 'Backend agent reply'}]}};
  }},
};
const flows = source('flows');
for (const match of flows.matchAll(/import\s+\{([\s\S]*?)\}\s+from/g)) {
  for (const name of match[1].split(',').map(s => s.trim()).filter(Boolean)) {
    if (!(name in sandbox)) sandbox[name] = () => {};
  }
}
const ctx = vm.createContext(sandbox);
vm.runInContext(clean(source('store')) + '\nthis.store = store;', ctx);
vm.runInContext(clean(flows), ctx);
sandbox.message = message;
if (prompt) sandbox.store.add({id: 'prompt', type: 'assistant', data: {text: prompt}});
function checkStored() {
  const saved = JSON.parse(storage.get('ja.transcript.v1:synthetic-user'));
  assert.equal(saved.find(entry => entry.type === 'user').data.text,
    sensitive ? '[已隐藏敏感内容]' : message);
  if (sensitive) assert.ok(writes.every(value => !value.includes(message)), 'raw text must never reach setItem');
}
(async () => {
  await vm.runInContext('sendComposer({text: message, attachments: []})', ctx);
  assert.deepEqual(calls, [message], 'the unchanged message must reach the agent');
  assert.deepEqual(displayed, [message], 'the live transcript still shows the message');
  assert.equal(sandbox.store.entries().find(entry => entry.type === 'user').data.text, message);
  assert.deepEqual(replies, failure ? [] : ['Backend agent reply'], 'no local canned answer');
  checkStored();
  // Another write, including quota fallback, cannot accidentally flush private text.
  rejectWrite = true;
  sandbox.store.add({id: 'later', type: 'system', data: {text: 'later transcript entry'}});
  sandbox.store.update('later', {text: 'updated transcript entry'});
  checkStored();
  if (failure) {
    assert.equal(errors.length, 1);
    assert.equal(typeof errors[0].onRetry, 'function');
    sandbox.store.reload();
    assert.equal(sandbox.store.entries().find(entry => entry.type === 'user').data.text,
      sensitive ? '[已隐藏敏感内容]' : message, 'reload after failure must not restore private text');
    await errors[0].onRetry();
    assert.deepEqual(calls, [message, message], 'retry must also send the original text');
    assert.deepEqual(replies, ['Backend agent reply']);
    checkStored();
  }
  sandbox.store.reload();
  assert.equal(sandbox.store.entries().find(entry => entry.type === 'user').data.text,
    sensitive ? '[已隐藏敏感内容]' : message);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    result = subprocess.run(
        ["node", "-e", script],
        input=json.dumps(dict(message=message, prompt=prompt, sensitive=sensitive, failure=failure)),
        text=True, capture_output=True, cwd=ROOT, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
