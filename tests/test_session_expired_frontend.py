"""S1 frontend: never swap uid silently; expired/503 card; no local writes while pending; masked id."""

from pathlib import Path

JS = Path("static/js")


def _src(name):
    return (JS / name).read_text(encoding="utf-8")


def test_401_never_reissues_session_silently():
    api = _src("api.js")
    assert "ensureSession(true)" not in api
    assert "_retried" not in api
    assert "throw sessionExpired()" in api


def test_expired_card_copy_role_and_button():
    ident = _src("identity.js")
    assert '"role", "alert"' in ident
    assert "登录已过期，之前的记录在原账号下" in ident
    assert "btn btn-primary" in ident


def _copy_block(ident, kind):
    i = ident.index(f"  {kind}: {{")
    return ident[i : ident.index("},", i)]


def test_503_card_is_not_login_expired_and_retries_in_place():
    ident = _src("identity.js")
    assert "r.status === 503" in ident and 'block("unavailable")' in ident
    c503 = _copy_block(ident, "unavailable")
    assert '"暂时无法确认你的身份"' in c503
    assert '"稍后再试"' in c503
    assert "登录已过期" not in c503 and "重新开始" not in c503
    assert 'kind === "unavailable" ? retry : restart' in ident
    r = ident[ident.index("export function retry()") : ident.index("/** User-confirmed switch")]
    assert "location" not in r and "setItem" not in r and "ensureSession()" in r


def test_401_card_keeps_expired_copy_and_restart():
    ident = _src("identity.js")
    c401 = _copy_block(ident, "expired")
    assert '"登录已过期，之前的记录在原账号下"' in c401 and '"重新开始"' in c401
    rs = ident[ident.index("export function restart()") :]
    assert 'window.location.assign("/")' in rs[: rs.index("\n}\n")]


def test_friendly_error_matches_card():
    api = _src("api.js")
    assert "暂时无法确认你的身份。点页面顶部的「稍后再试」" in api
    assert "登录已过期，之前的记录在原账号下。点页面顶部的「重新开始」" in api
    assert "点上方" not in api
    assert 'prepend(card)' in _src("identity.js")
    assert "position: sticky" in Path("static/chat.css").read_text(encoding="utf-8")


def test_uid_change_is_confirmed_not_adopted():
    ident = _src("identity.js")
    assert "uid !== prev" in ident
    i = ident.index("uid !== prev")
    branch = ident[i : ident.index("}", i)]
    assert 'block("expired")' in branch and "setItem" not in branch


def test_no_local_transcript_while_pending():
    store = _src("store.js")
    assert "sessionReady()" in store
    assert store.index("sessionReady()") < store.index("localStorage.setItem")


def test_displayed_id_is_masked():
    ident = _src("identity.js")
    assert "el.value = maskId(uid)" in ident


def test_focus_ring_2px():
    css = Path("static/chat.css").read_text(encoding="utf-8")
    i = css.index(".session-blocked {")
    block = css[i:]
    assert ".session-blocked-btn:focus-visible" in block
    assert "outline: 2px solid var(--focus-ring, #191919)" in block
    assert "#2563eb" not in block and "#555" not in block
    assert "var(--text-secondary, #5F5F66)" in block
