"""Synthetic frontend table regressions using the Node/minimal-DOM harness."""

import pytest

from tests.test_chat_markdown import run_thread


@pytest.mark.parametrize("reply", [
    "| 状态 | 数量 |\n|---|---|\n| 待投 | 0 |",
    "状态 | 数量\n:--- | ---:\n待投 | 0",
    "| 状态 | 数量 |\r\n| :---: | ---: |\r\n| 待投 | 0 |",
])
def test_pipe_table_in_assistant_bubble(reply):
    run_thread(reply, r'''
const html = find(assistantMessage(value, {speak: false}), 'bubble-text').innerHTML;
assert.equal(html, '<table><thead><tr><th>状态</th><th>数量</th></tr></thead>' +
  '<tbody><tr><td>待投</td><td>0</td></tr></tbody></table>');
assert.ok(!html.includes('|--'));
assert.equal(find(userRow({text: value}), 'bubble-text').text, value);
''')


def test_pipe_table_inline_formatting_escaping_and_surrounding_blocks():
    run_thread("", r'''
assert.equal(renderMarkdown('进度：\n| **状态** | 数量 |\n|---|---|\n' +
  '| 待\\|投 | `0` |\n| <img src=x onerror="alert(1)"> | *1* |\n\n- 建议跟进\n> 保持联系'),
  '进度：<br><table><thead><tr><th><strong>状态</strong></th><th>数量</th></tr></thead>' +
  '<tbody><tr><td>待|投</td><td><code>0</code></td></tr>' +
  '<tr><td>&lt;img src=x onerror=&quot;alert(1)&quot;&gt;</td><td><em>1</em></td></tr>' +
  '</tbody></table><br><br><ul><li>建议跟进</li></ul><br><blockquote>保持联系</blockquote>');
assert.equal(renderMarkdown('| 状态 | 数量 |\n|---|---|\n| 待投 |\n| 已投 | 2 | 多余 |\n结束'),
  '<table><thead><tr><th>状态</th><th>数量</th></tr></thead><tbody>' +
  '<tr><td>待投</td><td></td></tr><tr><td>已投</td><td>2</td></tr></tbody></table><br>结束');
assert.equal(renderMarkdown('| 状态 |\n|---|\n- 待投 | 已投\n> 1 | 2'),
  '<table><thead><tr><th>状态</th></tr></thead><tbody></tbody></table><br>' +
  '<ul><li>待投 | 已投</li></ul><br><blockquote>1 | 2</blockquote>');
''')


@pytest.mark.parametrize("reply", [
    "状态 | 数量\n待投 | 0",
    "| 状态 | 数量 |\n|---|\n| 待投 | 0 |",
    "| 状态 | 数量 |\n|---|not a delimiter|\n| 待投 | 0 |",
    "普通文本\n---\n结束",
])
def test_non_table_pipes_stay_literal(reply):
    run_thread(reply, r'''
assert.equal(renderMarkdown(value), value.replaceAll('\n', '<br>'));
''')
