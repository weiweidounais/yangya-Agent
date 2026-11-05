"""Offline markdown regressions using the existing Node/minimal-DOM test style."""

import json
from pathlib import Path
import subprocess

import pytest

JS = Path(__file__).resolve().parent.parent / "static/js"


def run_thread(text, assertions):
    markdown = JS / "markdown.js"
    source = markdown.read_text() if markdown.exists() else ""
    source += "\n" + "\n".join(
        line for line in (JS / "thread.js").read_text().splitlines()
        if not line.startswith("import ")
    )
    source = source.replace("export function ", "function ")
    harness = r'''
const assert = require('node:assert/strict');
const value = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
function h(tag, props = {}, children = []) {
  const classes = new Set((props.class || '').split(' '));
  return {tag, ...props, children: children.filter(Boolean), innerHTML: '',
    classList: {add(x) {classes.add(x)}, toggle(x) {
      if (classes.has(x)) {classes.delete(x); return false;}
      classes.add(x); return true;
    }},
    addEventListener(name, fn) {this[name] = fn},
    setAttribute(name, value) {this[name] = value},
    append(...xs) {this.children.push(...xs)}};
}
const log = {appendChild() {}};
const $ = id => id === 'log' ? log : null;
const requestAnimationFrame = () => {};
const announce = () => {};
function find(node, cls) {
  if ((node.class || '').split(' ').includes(cls)) return node;
  for (const child of node.children || []) {const found = find(child, cls); if (found) return found;}
}
'''
    result = subprocess.run(
        ["node", "-e", harness + source + "\n" + assertions],
        input=json.dumps(text), text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_mock_llm_markdown_in_assistant_bubble(client, fake_llm):
    fake_llm.reply = "**重点**\n*建议*：先准备 `作品集`。\n- 梳理项目\n- 练习表达"
    response = client.post("/api/chat", json={
        "user_id": "markdown-synthetic", "message": "帮我规划一下周末",
    })
    assert response.status_code == 200
    content = response.json()["data"]["messages"][-1]["content"]
    assert "**重点**" in content
    run_thread(content, r'''
const row = assistantMessage(value, {speak: false});
assert.equal(find(row, 'bubble-text').innerHTML,
  '<strong>重点</strong><br><em>建议</em>：先准备 <code>作品集</code>。<br><ul><li>梳理项目</li><li>练习表达</li></ul>');
assert.equal(find(userRow({text: value}), 'bubble-text').text, value);
''')


@pytest.mark.parametrize("reply,expected", [
    ("> 建议先列个清单。", "<blockquote>建议先列个清单。</blockquote>"),
    ("> 优先**准备作品集**。\n> 然后留出休息时间。",
     "<blockquote>优先<strong>准备作品集</strong>。<br>然后留出休息时间。</blockquote>"),
    ("根据你的经历，建议如下：\n> 优先**准备作品集**。\n> 然后留出休息时间。\n祝你顺利！",
     "根据你的经历，建议如下：<br><blockquote>优先<strong>准备作品集</strong>。"
     "<br>然后留出休息时间。</blockquote><br>祝你顺利！"),
])
def test_mock_llm_blockquotes_in_assistant_bubble(client, fake_llm, reply, expected):
    fake_llm.reply = reply
    response = client.post("/api/chat", json={
        "user_id": "blockquote-synthetic", "message": "帮我规划一下周末",
    })
    assert response.status_code == 200
    content = response.json()["data"]["messages"][-1]["content"]
    assert content == reply
    run_thread(content, rf'''
const html = find(assistantMessage(value, {{speak: false}}), 'bubble-text').innerHTML;
assert.equal(html, {json.dumps(expected)});
assert.ok(!html.includes('&gt; '));
''')


@pytest.mark.parametrize("reply,expected", [
    ("> 建议先列个清单。", "<blockquote>建议先列个清单。</blockquote>"),
    ("> 优先**准备作品集**。\n> 然后留出休息时间。",
     "<blockquote>优先<strong>准备作品集</strong>。<br>然后留出休息时间。</blockquote>"),
    ("根据你的经历，建议如下：\n> 优先**准备作品集**。\n> 然后留出休息时间。\n祝你顺利！",
     "根据你的经历，建议如下：<br><blockquote>优先<strong>准备作品集</strong>。"
     "<br>然后留出休息时间。</blockquote><br>祝你顺利！"),
])
def test_golden_llm_blockquotes_in_assistant_bubble(reply, expected):
    run_thread(reply, rf'''
const html = find(assistantMessage(value, {{speak: false}}), 'bubble-text').innerHTML;
assert.equal(html, {json.dumps(expected)});
assert.ok(!html.includes('&gt; '));
''')


def test_bold_adjacent_to_chinese_text_and_punctuation():
    run_thread("优先**整理清单**再出门，**重点**：展示成果。", r'''
assert.equal(find(assistantMessage(value, {speak: false}), 'bubble-text').innerHTML,
  '优先<strong>整理清单</strong>再出门，<strong>重点</strong>：展示成果。');
''')


@pytest.mark.parametrize("wrapper,tag", [("{}", ""), ("**{}**", "strong"),
                                         ("*{}*", "em"), ("`{}`", "code"),
                                         ("- {}", "ul"), ("> {}", "blockquote")])
def test_assistant_markdown_escapes_xss(wrapper, tag):
    payload = '<img src=x onerror="alert(1)"><script>alert(2)</script><svg onload=\'alert(3)\'>&lt;img&gt;'
    escaped = (payload.replace("&", "&amp;").replace("<", "&lt;")
               .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))
    expected = f"<{tag}>{escaped}</{tag}>" if tag else escaped
    if tag == "ul":
        expected = f"<ul><li>{escaped}</li></ul>"
    run_thread(wrapper.format(payload), rf'''
const row = assistantMessage(value, {{speak: false}});
const html = find(row, 'bubble-text').innerHTML;
assert.equal(html, {json.dumps(expected)});
assert.doesNotMatch(html, /<(?:img|script|svg)\b/i);
''')


def test_markdown_subset_and_code_is_literal():
    run_thread("", r'''
assert.equal(renderMarkdown('1. **one**\r\n2. _two_\r\n\r\n+ three\n* four\nend'),
  '<ol><li><strong>one</strong></li><li><em>two</em></li></ol><br><br><ul><li>three</li><li>four</li></ul><br>end');
assert.equal(renderMarkdown('`**literal** _code_` and **bold** *italic*'),
  '<code>**literal** _code_</code> and <strong>bold</strong> <em>italic</em>');
assert.equal(renderMarkdown('plain & text\n\nnext\n'), 'plain &amp; text<br><br>next<br>');
assert.equal(renderMarkdown('unmatched ** and `'), 'unmatched ** and `');
// Intraword underscores are identifiers, not emphasis; standalone _..._ still is.
assert.equal(renderMarkdown('请配置 LLM_FAST_API_KEY / user_id 后再试'), '请配置 LLM_FAST_API_KEY / user_id 后再试');
assert.equal(renderMarkdown('snake_case_name 和 _强调_ 以及 __加粗__'), 'snake_case_name 和 <em>强调</em> 以及 <strong>加粗</strong>');
assert.equal(renderMarkdown(''), '');
''')


def test_long_assistant_reply_keeps_collapse_toggle():
    run_thread("**synthetic** " * 60, r'''
const row = assistantMessage(value, {speak: false});
const body = find(row, 'bubble-text');
const toggle = find(row, 'more-toggle');
assert.ok(body.innerHTML.includes('<strong>synthetic</strong>'));
toggle.click();
assert.equal(toggle['aria-expanded'], 'true');
assert.equal(toggle.textContent, '收起');
toggle.click();
assert.equal(toggle['aria-expanded'], 'false');
assert.equal(toggle.textContent, '展开全文');
''')
