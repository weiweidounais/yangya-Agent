from __future__ import annotations

from app.agent.subgraphs.chat import CRISIS_FOOTER

ASK_REPLY = (
    "好嘞，C端AI产品这个方向挺适合你。\n"
    '<ask>{"type":"single","question":"你倾向哪类C端场景？","options":["AI内容/创作工具","AI效率/助手类","AI社交/情感陪伴"]}</ask>'
)


def test_chat_returns_structured_card(client, fake_llm):
    fake_llm.reply = ASK_REPLY
    r = client.post("/api/chat", json={"user_id": "card-user", "message": "周末想出去走走，帮我想想去哪"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "chat"
    ask = body["data"]["ask"]
    assert ask["type"] == "single" and len(ask["options"]) == 3
    content = body["data"]["messages"][-1]["content"]
    assert "<ask>" not in content and content.startswith("好嘞")
    # the format guide is in the system prompt so the model knows to emit cards
    assert "<ask>" in fake_llm.calls[-1][0]


def test_chat_malformed_card_degrades_to_text(client, fake_llm):
    fake_llm.reply = '你更想先做哪个？<ask>{"type":"single","question":"先做哪个",</ask>'
    r = client.post("/api/chat", json={"user_id": "card-user", "message": "帮我规划一下周末"})
    assert r.status_code == 200
    data = r.json()["data"]
    assert "ask" not in data
    assert data["messages"][-1]["content"].startswith("你更想先做哪个？")
    assert "<ask>" not in data["messages"][-1]["content"]


def test_answer_carries_question_context(client, fake_llm):
    fake_llm.reply = "明白，效率类工具的话我们可以先看看这些方向。"
    r = client.post(
        "/api/chat",
        json={
            "user_id": "card-user",
            "message": "AI效率/助手类（补充：偏 ToB 也行）",
            "reply_to": {"ask_id": "ask_1", "type": "single", "question": "你倾向哪类C端场景？", "prompt": "好嘞"},
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "chat"
    user_prompt = [u for s, u in fake_llm.calls if not s.startswith("你是心理安全分类器")][0]
    assert "你倾向哪类C端场景？" in user_prompt
    assert "AI效率/助手类（补充：偏 ToB 也行）" in user_prompt


def test_answer_skips_draft_heuristics(client, fake_llm):
    fake_llm.reply = "收到"
    long_answer = "我比较看重有没有安静的地方可以坐一会儿，交通方便、不用排队的优先，" * 2
    r = client.post(
        "/api/chat",
        json={"user_id": "card-user", "message": long_answer, "reply_to": {"question": "你看重什么？"}},
    )
    assert r.json()["status"] == "chat"


def test_crisis_drops_card_and_keeps_footer(client, fake_llm):
    fake_llm.reply = '我在。<ask>{"type":"single","question":"想聊哪个？","options":["工作","生活"]}</ask>'
    r = client.post("/api/chat", json={"user_id": "card-user", "message": "我真的不想活了"})
    data = r.json()["data"]
    assert "ask" not in data
    content = data["messages"][-1]["content"]
    assert CRISIS_FOOTER in content and "<ask>" not in content
    # crisis turns use the safety note, not the card guide
    assert "<ask>" not in fake_llm.calls[-1][0]


def test_llm_requested_secret_card(client, fake_llm):
    fake_llm.reply = '可以，把账号放到安全输入区我就能记住。<ask>{"type":"secret","platform":"账号密码","purpose":"保存网盘登录信息"}</ask>'
    r = client.post("/api/chat", json={"user_id": "card-user", "message": "帮我记一下网盘的账号"})
    ask = r.json()["data"]["ask"]
    assert ask["type"] == "secret" and ask["key"] == "account_login"
    assert ask["purpose"] == "保存网盘登录信息"


def test_mock_mode_without_llm_stays_plain(client):
    r = client.post("/api/chat", json={"user_id": "card-user", "message": "今天有点累"})
    assert r.status_code == 200
    assert "ask" not in r.json()["data"]

