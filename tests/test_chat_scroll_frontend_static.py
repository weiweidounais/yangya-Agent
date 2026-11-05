"""Scroll policy regression checks for the served frontend (no JS harness)."""

from pathlib import Path
import subprocess

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"
THREAD = (STATIC / "js/thread.js").read_text(encoding="utf-8")
FLOWS = (STATIC / "js/flows.js").read_text(encoding="utf-8")


def block(source, start, end):
    return source.split(start, 1)[1].split(end, 1)[0]


@pytest.mark.parametrize("height", [60, 700])
@pytest.mark.parametrize("check", ["send", "reply"])
def test_send_from_history_resumes_follow_until_upward_input(height, check):
    policy = "let stickToBottom" + block(THREAD, "let stickToBottom", "export function clearLog(")
    user_message = "function userMessage(" + block(THREAD, "export function userMessage(", "export function userRow(")
    render = "function renderEntry(" + block(FLOWS, "function renderEntry(", "function rerender(")
    emit = "function emit(" + block(FLOWS, "function emit(", "const say =")
    echo = "let lastEcho = null;\nfunction echoUser(" + block(FLOWS, "function echoUser(", "/** The server hid")
    result = subprocess.run(
        ["node", "-e", (policy + user_message + render + emit + echo).replace("export function", "function") + r'''
const assert = require('node:assert/strict');
const frames = [];
const requestAnimationFrame = callback => frames.push(callback);
const flush = () => { while (frames.length) frames.shift()(); };
const setTimeout = () => 1;
const clearTimeout = () => {};
const prefersReducedMotion = () => true;
const removeHero = () => {};
const listeners = {};
let scrolls = 0;
const scroller = {
  scrollHeight: 1500, scrollTop: 200, clientHeight: 500,
  addEventListener: (name, callback) => { listeners[name] = callback; },
  scrollTo({top}) {
    scrolls++;
    this.scrollTop = Math.min(top, this.scrollHeight - this.clientHeight);
    listeners.scroll();
  },
};
const button = {hidden: false, addEventListener() {}};
const log = {appendChild(node) { scroller.scrollHeight += node.offsetHeight; }};
const $ = id => ({logScroll: scroller, log, jumpLatest: button})[id];
const document = {addEventListener() {}};
const window = {addEventListener() {}};
const ResizeObserver = class { observe() {} };
const userRow = () => ({isConnected: true, offsetHeight: HEIGHT, offsetTop: scroller.scrollHeight});
const nodes = new Map();
const store = {add: entry => entry};
const nextId = () => 'sent';
const MAX_STORED_TEXT = 1500;
initThread();
listeners.scroll();
assert.equal(button.hidden, false);
echoUser('sent while reading history');
flush();
if (CHECK === 'send') {
  assert.equal(scroller.scrollTop, scroller.scrollHeight - scroller.clientHeight,
    'sending must reach the bottom even for a tall sent message');
  assert.equal(scrolls, 1, 'sending must scroll once');
  assert.equal(button.hidden, true);
}

// The reply starts later, after the programmatic guard has expired.
listeners.scrollend();
for (let chunk = 0; chunk < 3; chunk++) {
  append({isConnected: true, offsetHeight: 100});
  flush();
  assert.equal(scroller.scrollTop, scroller.scrollHeight - scroller.clientHeight,
    'reply updates after sending must remain followed');
  assert.equal(button.hidden, true);
  listeners.scrollend();
}

// Upward input cancels even a reply update already queued for the next frame.
append({isConnected: true, offsetHeight: 100});
listeners.wheel({deltaY: -100});
scroller.scrollTop -= 200;
listeners.scroll();
const readingTop = scroller.scrollTop;
const previousScrolls = scrolls;
flush();
append({isConnected: true, offsetHeight: 100});
flush();
assert.equal(scroller.scrollTop, readingTop);
assert.equal(scrolls, previousScrolls);
assert.equal(button.hidden, false);

// Restoring a user row must neither jump nor re-enable following.
renderEntry({id: 'restored', type: 'user', data: {text: 'history'}}, {restored: true});
flush();
append({isConnected: true, offsetHeight: 100});
flush();
assert.equal(scroller.scrollTop, readingTop);
assert.equal(scrolls, previousScrolls);
'''.replace("HEIGHT", str(height)).replace("CHECK", repr(check))],
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_append_respects_reader_and_opens_tall_nodes_at_top():
    append = block(THREAD, "export function append(", "export function clearLog(")
    assert append.index("updateJumpLatest();") < append.index("requestAnimationFrame")
    pending = append.split("requestAnimationFrame", 1)[1]
    assert pending.index("if (!stickToBottom)") < pending.index("return;") < pending.index("scrollToBottom()")
    assert "node.offsetHeight > scroller.clientHeight" in pending
    assert "programmaticScroll(() => { scroller.scrollTop = node.offsetTop - 16; });" in pending


def test_automatic_reveal_neither_scrolls_nor_focuses_while_reading():
    reveal = block(THREAD, "export function reveal(", "export function append(")
    assert "user = false" in reveal
    assert reveal.index("if (!stickToBottom && !user)") < reveal.index("return;", reveal.index("if (!stickToBottom")) < reveal.index("scrollIntoView") < reveal.index("focus.focus")
    assert reveal.index("updateJumpLatest();") < reveal.index("return;", reveal.index("if (!stickToBottom"))
    assert "programmaticScroll(() => node.scrollIntoView" in reveal
    assert "revealEntry(conf.id, { user: true })" in FLOWS
    assert "revealEntry: (id) => revealEntry(id, { user: true })" in FLOWS


def test_restore_scrolls_once_and_rerenders_preserve_position():
    render = block(FLOWS, "function renderEntry(", "function rerender(")
    assert render.count("scroll: !restored") == 4
    user_message = block(THREAD, "export function userMessage(", "export function userRow(")
    assert "append(userRow(data, { animate }), { scroll: false });" in user_message
    assert "if (scroll) scrollToBottom({ force: true });" in user_message
    assert user_message.index("append(") < user_message.index("scrollToBottom(")
    assert "return append(row, { scroll });" in THREAD
    assert 'role: "note" }, [h("span", { text })]), { scroll });' in THREAD
    restore = block(FLOWS, "export function restoreTranscript(", "export function resetThread(")
    assert "renderEntry(entry, { restored: true })" in restore
    assert "divider: true, scroll: false" in restore
    assert restore.count("scrollToBottom(") == 1
    assert "force: true" not in restore
    rerender = block(FLOWS, "function rerender(", "function emit(")
    assert "const scrollTop = scroller.scrollTop" in rerender
    assert "old.getBoundingClientRect().bottom <= scroller.getBoundingClientRect().top" in rerender
    assert "scrollTop + (aboveViewport ? node.offsetHeight - oldHeight : 0)" in rerender
    assert "scrollToBottom" not in rerender and "reveal(" not in rerender
    css = (STATIC / "chat.css").read_text(encoding="utf-8")
    scroller = block(css, ".log-scroll {", "}")
    assert "overflow-anchor: none" in scroller
    assert "position: relative" in scroller


def test_programmatic_guard_upward_input_and_jump_button():
    assert "let isProgrammatic = false" in THREAD
    assert "isProgrammatic = true" in THREAD
    assert "setTimeout(endProgrammaticScroll, 400)" in THREAD
    assert 'addEventListener("scrollend", endProgrammaticScroll)' in THREAD
    assert THREAD.index("if (isProgrammatic) return") < THREAD.index("stickToBottom = isNearBottom()")
    assert "if (event.deltaY < 0) stopSticking()" in THREAD
    assert '["PageUp", "ArrowUp", "Home"].includes(event.key)' in THREAD
    stop = block(THREAD, "const stopSticking =", 'scroller.addEventListener("wheel"')
    assert "stickToBottom = false;" in stop
    assert "endProgrammaticScroll();" in stop
    assert "programmaticScroll(" not in stop
    assert "scrollTo(" not in stop
    assert "isProgrammatic" not in stop
    end_guard = block(THREAD, "function endProgrammaticScroll()", "export function programmaticScroll(")
    assert "isProgrammatic = false;" in end_guard
    assert "clearTimeout(programmaticTimer);" in end_guard
    assert "programmaticScroll(() => s.scrollTo" in THREAD
    assert "scrollToBottom({ force: true, smooth: true })" in THREAD
    assert "if (force) stickToBottom = true" in THREAD
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    button = block(html, 'id="jumpLatest"', "</button>")
    assert html.count('id="jumpLatest"') == 1
    assert "新消息" in button
    assert "↓" not in button
    assert '<svg ' in button
    assert '<path d="M19 12l-7 7-7-7"/>' in button


def test_touch_only_stops_sticking_when_finger_moves_down():
    start = block(THREAD, 'addEventListener("touchstart",', 'addEventListener("touchmove",')
    assert "touchY = event.touches[0].clientY;" in start
    move = block(THREAD, 'addEventListener("touchmove",', 'document.addEventListener("keydown",')
    assert "const clientY = event.touches[0].clientY;" in move
    assert "if (clientY > touchY) stopSticking();" in move
    assert move.count("stopSticking()") == 1
    assert move.index("if (clientY > touchY)") < move.index("touchY = clientY;")
    assert "passive: true" in start and "passive: true" in move


def test_keyboard_ignores_composer_and_contenteditable_targets():
    keydown = block(THREAD, 'document.addEventListener("keydown",', '$("jumpLatest").addEventListener("click",')
    guard = 'if (event.target.closest("input, textarea, select, [contenteditable]")) return;'
    assert guard in keydown
    assert keydown.index(guard) < keydown.index("stopSticking()")


def test_jump_visibility_depends_only_on_actual_bottom_gap():
    visibility = block(THREAD, "function updateJumpLatest()", "function isNearBottom()")
    assert '$("jumpLatest").hidden = isNearBottom();' in visibility
    assert "stickToBottom" not in visibility
    near_bottom = block(THREAD, "function isNearBottom()", "export function scrollToBottom(")
    assert "s.scrollHeight - s.scrollTop - s.clientHeight <= 80" in near_bottom
    # No event or auto-scroll path may override the geometry-derived state.
    assert THREAD.count('.hidden =') == 1


def test_jump_visibility_handles_missing_elements_and_bottom_threshold():
    helpers = "function updateJumpLatest()" + block(
        THREAD, "function updateJumpLatest()", "export function scrollToBottom("
    )
    result = subprocess.run(
        ["node", "-e", helpers + r'''
const assert = require('node:assert/strict');
const elements = {};
const $ = id => elements[id] || null;
assert.equal(isNearBottom(), true);
assert.doesNotThrow(updateJumpLatest);

const button = elements.jumpLatest = {hidden: false};
updateJumpLatest();
assert.equal(button.hidden, true);

const scroller = elements.logScroll = {
  scrollHeight: 1000, scrollTop: 419, clientHeight: 500
};
assert.equal(isNearBottom(), false);
updateJumpLatest();
assert.equal(button.hidden, false);
scroller.scrollTop = 420;
assert.equal(isNearBottom(), true);
updateJumpLatest();
assert.equal(button.hidden, true);
scroller.scrollTop = 500;
updateJumpLatest();
assert.equal(button.hidden, true);
for (const height of [400, 500]) {
  scroller.scrollHeight = height;
  scroller.scrollTop = 0;
  assert.equal(isNearBottom(), true);
  updateJumpLatest();
  assert.equal(button.hidden, true);
}
scroller.scrollHeight = 1000;
scroller.scrollTop = 0;
updateJumpLatest();
assert.equal(button.hidden, false);

delete elements.jumpLatest;
assert.doesNotThrow(updateJumpLatest);
assert.equal(isNearBottom(), false);
'''],
        text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_scroll_updates_visibility_even_during_programmatic_guard():
    listener = block(THREAD, '\"scroll\",', 'scroller.addEventListener("scrollend"')
    assert listener.index("updateJumpLatest();") < listener.index("if (isProgrammatic) return")
    action = block(THREAD, "export function programmaticScroll(", "export function initThread()")
    assert action.index("action();") < action.index("updateJumpLatest();")
    end_guard = block(THREAD, "function endProgrammaticScroll()", "export function programmaticScroll(")
    assert "updateJumpLatest();" in end_guard


def test_resize_long_replies_and_reload_refresh_visibility_without_scrolling():
    init = block(THREAD, "export function initThread()", "function updateJumpLatest()")
    assert "const resizeObserver = new ResizeObserver(updateJumpLatest);" in init
    assert "resizeObserver.observe(scroller);" in init
    assert 'resizeObserver.observe($("log"));' in init
    assert 'window.addEventListener("pageshow", updateJumpLatest);' in init
    assert init.rstrip().endswith("updateJumpLatest();\n}")
    visibility = block(THREAD, "function updateJumpLatest()", "function isNearBottom()")
    assert "scrollTo" not in visibility and "focus" not in visibility


def test_jump_waits_for_bottom_and_input_does_not_force_visibility():
    jump = block(THREAD, "export function scrollToBottom(", "export function reveal(")
    assert jump.index("s.scrollTo(") < jump.rindex("updateJumpLatest();")
    stop = block(THREAD, "const stopSticking =", 'scroller.addEventListener("wheel"')
    assert "updateJumpLatest();" in stop
    clear = block(THREAD, "export function clearLog()", "export function collapsible(")
    assert clear.index("replaceChildren();") < clear.index("updateJumpLatest();")
