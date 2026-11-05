from __future__ import annotations

import pytest

from app.agent.ask_cards import answer_context, as_plain_text, split_reply, validate_ask

SCREENSHOT_REPLY = (
    "好嘞，C端AI产品，这个方向你经验挺足的。\n想先帮你筛一筛：\n"
    "- AI内容/创作工具\n- AI效率/助手类\n- AI社交/情感陪伴\n"
    '<ask>{"type":"single","question":"你倾向哪类C端场景？",'
    '"options":["AI内容/创作工具","AI效率/助手类","AI社交/情感陪伴","还是别的？"]}</ask>'
)


def test_single_card_from_screenshot_case():
    text, card = split_reply(SCREENSHOT_REPLY)
    assert card is not None
    assert card["type"] == "single"
    assert card["question"] == "你倾向哪类C端场景？"
    assert card["options"] == ["AI内容/创作工具", "AI效率/助手类", "AI社交/情感陪伴"]
    # "还是别的？" becomes the free-text supplement instead of an option
    assert card["allow_text"] is True and card["text_label"]
    assert card["id"].startswith("ask_")
    # option bullets duplicated in prose are dropped; the tag never leaks
    assert "<ask>" not in text and "- AI效率" not in text
    assert text.startswith("好嘞")


def test_multi_with_max_select_and_dedupe():
    raw = (
        "挑几个你在意的："
        '<ask>{"type":"multi","question":"你最看重哪些？","options":["薪资","成长","薪资","团队",""],'
        '"max_select":2,"allow_text":false}</ask>'
    )
    text, card = split_reply(raw)
    assert card["type"] == "multi"
    assert card["options"] == ["薪资", "成长", "团队"]
    assert card["max_select"] == 2
    assert card["allow_text"] is False
    assert text == "挑几个你在意的："


def test_text_card_standalone():
    _, card = split_reply('说说看<ask>{"type":"text","question":"最近一次被拒是什么情况？","placeholder":"公司、轮次都行"}</ask>')
    assert card["type"] == "text"
    assert "options" not in card
    assert card["placeholder"] == "公司、轮次都行"


def test_secret_card_is_server_defined():
    _, card = split_reply(
        '需要保存一下<ask>{"type":"secret","platform":"账号密码","purpose":"保存网盘登录信息",'
        '"fields":[{"name":"evil","label":"银行卡"}]}</ask>'
    )
    assert card["type"] == "secret"
    assert card["key"] == "account_login"
    assert card["label"] == "账号登录信息"
    assert card["purpose"] == "保存网盘登录信息"
    # field list comes from the server catalog, never from the model
    assert [f["name"] for f in card["fields"]] == ["platform_name", "account", "password", "cookie"]


@pytest.mark.parametrize(
    "platform,key",
    [("账号密码", "account_login"), ("登录信息", "account_login"), ("手机号", "phone"), ("身份证", "id_card")],
)
def test_secret_platform_mapping(platform, key):
    assert validate_ask({"type": "secret", "platform": platform})["key"] == key


def test_unknown_secret_platform_is_rejected():
    assert validate_ask({"type": "secret", "platform": "网银"}) is None


def test_whole_reply_json_format():
    raw = '{"reply":"选一个吧","ask":{"type":"radio","question":"先聊哪个？","options":["写邮件","设提醒"]}}'
    text, card = split_reply(raw)
    assert text == "选一个吧"
    assert card["type"] == "single"


def test_fenced_json_inside_tag():
    raw = '看看：<ask>```json\n{"type":"single","question":"Q?","options":["A","B"]}\n```</ask>'
    _, card = split_reply(raw)
    assert card and card["options"] == ["A", "B"]


@pytest.mark.parametrize(
    "raw",
    [
        "你好<ask>{not json</ask>",
        "你好<ask>",
        '你好<ask>{"type":"single","question":"Q?"',
        "你好<ask>[1,2,3]</ask>",
    ],
)
def test_malformed_blocks_fall_back_to_text(raw):
    text, card = split_reply(raw)
    assert card is None
    assert text.startswith("你好")
    assert "<ask>" not in text and "{" not in text


def test_invalid_card_salvages_question_as_text():
    raw = '你倾向哪个方向？<ask>{"type":"single","question":"选方向","options":["只有一个"]}</ask>'
    text, card = split_reply(raw)
    assert card is None
    assert "选方向" in text and "- 只有一个" in text


def test_unknown_type_falls_back():
    text, card = split_reply('嗨<ask>{"type":"slider","question":"几分？"}</ask>')
    assert card is None
    assert "几分？" in text


def test_plain_reply_untouched():
    assert split_reply("今天心情怎么样？") == ("今天心情怎么样？", None)
    assert split_reply("") == ("", None)
    assert split_reply(None) == ("", None)  # type: ignore[arg-type]


def test_question_and_option_limits():
    long_q = "问" * 300
    card = validate_ask({"type": "single", "question": long_q, "options": [f"选项{i}" * 10 for i in range(12)]})
    assert len(card["question"]) == 120
    assert len(card["options"]) == 8
    assert all(len(o) <= 40 for o in card["options"])


def test_plain_text_for_channels():
    _, card = split_reply(SCREENSHOT_REPLY)
    out = as_plain_text(card)
    assert out.splitlines()[0] == "你倾向哪类C端场景？"
    assert "- AI效率/助手类" in out
    secret = validate_ask({"type": "secret", "platform": "账号密码"})
    assert "安全输入区" in as_plain_text(secret)


def test_answer_context():
    ctx = answer_context({"question": "你倾向哪类C端场景？", "prompt": "好嘞"})
    assert "你倾向哪类C端场景？" in ctx and "好嘞" in ctx
    assert answer_context(None) == ""
    assert answer_context({"question": ""}) == ""
