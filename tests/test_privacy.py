import pytest

from app.privacy import HIDDEN, redact_sensitive


@pytest.mark.parametrize(
    "text, expected, kind",
    [
        ("password: ExampleSecret123", f"password: {HIDDEN}", "password"),
        ("password: 密码测试甲乙", f"password: {HIDDEN}", "password"),
        ("密码：密码测试甲乙", f"密码：{HIDDEN}", "password"),
        ("cookie: sid=abc123", f"cookie: {HIDDEN}", "cookie"),
        ("password: example secret with spaces", f"password: {HIDDEN}", "password"),
        ("password: example secret\n技能：Python", f"password: {HIDDEN}\n技能：Python", "password"),
    ],
)
def test_credentials_are_fully_redacted(text, expected, kind):
    assert redact_sensitive(text) == (expected, [kind])


@pytest.mark.parametrize("text", ["负责密码学研究", "cookie consent banner design"])
def test_ordinary_text_is_unchanged(text):
    assert redact_sensitive(text) == (text, [])


@pytest.mark.parametrize(
    "text, expected, kinds",
    [
        ("cookie sid=abcdefghijklmnop", f"cookie {HIDDEN}", ["cookie"]),
        ("cookie: sid=abc123", f"cookie: {HIDDEN}", ["cookie"]),
        ("cookie consent banner design", "cookie consent banner design", []),
    ],
)
def test_cookie_separator_detection(text, expected, kinds):
    assert redact_sensitive(text) == (expected, kinds)
