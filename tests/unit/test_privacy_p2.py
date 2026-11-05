"""P2 privacy: PII backends, HMAC ids, uncapped identity index, similarity prompt leaks."""

import json
from datetime import datetime, timezone

import pytest

from app.data_access import system_scope, user_scope
from app import db
from app.agent.harness import identity, leak, pii, prompt_leak
from app.agent.harness.leak import OTHER_USER_PLACEHOLDER, scan_output
from app.agent.harness.redact import mask_pii
from app.agent.subgraphs import chat  # noqa: F401  registers the prompts

db.init_db()
identity.init_db()


# ------------------------------------------------------------------ PII detection


def test_regex_pii_detects_chinese_names_and_addresses():
    text = "姓名：王小明\n电话 13812345678，住址 上海市浦东新区张杨路88号5栋1201室。张伟先生也来了。邮箱 a.b@example.com"
    kinds = {(s.type, s.text) for s in pii.detect(text)}
    assert ("name", "王小明") in kinds
    assert ("name", "张伟") in kinds
    assert ("phone", "13812345678") in kinds
    assert ("email", "a.b@example.com") in kinds
    assert any(k == "address" and "张杨路88号" in v for k, v in kinds)


def test_mask_pii_names_addresses_and_phone_hint():
    out = mask_pii("我是刘芳。住在幸福小区3栋2单元 电话 13912345678 座机 021-12345678")
    assert "刘芳" not in out and "幸福小区3栋" not in out
    assert "139****5678" in out and "12345678" not in out.replace("139****5678", "")


def test_plain_words_are_not_names():
    assert not [s for s in pii.detect("今天天气不错，我们聊聊周末安排吧。", types=("name",))]


def test_ml_backend_falls_back_to_regex(monkeypatch):
    monkeypatch.setenv("PII_BACKEND", "presidio")

    def boom():
        raise ImportError("presidio not installed")

    monkeypatch.setitem(pii._LOADERS, "presidio", boom)
    pii.reset_backend()
    try:
        info = pii.backend_info()
        assert info["active"] == "regex" and "presidio" in info["error"]
        assert any(s.type == "phone" for s in pii.detect("电话 13812345678"))
    finally:
        pii.reset_backend()


def test_ml_backend_spans_merged_and_runtime_failure_tolerated(monkeypatch):
    class FakeGliner:
        fail = False

        def predict_entities(self, text, labels, threshold=0.5):
            if self.fail:
                raise RuntimeError("cuda oom")
            i = text.index("诸葛亮")
            return [{"label": "person", "start": i, "end": i + 3, "text": "诸葛亮", "score": 0.93}]

    eng = FakeGliner()
    monkeypatch.setenv("PII_BACKEND", "gliner")
    monkeypatch.setitem(pii._LOADERS, "gliner", lambda: eng)
    pii.reset_backend()
    try:
        spans = pii.detect("推荐诸葛亮来讲课", types=("name",))
        assert [(s.text, s.source) for s in spans] == [("诸葛亮", "gliner")]
        eng.fail = True
        assert pii.detect("电话 13812345678", types=("phone", "name"))[0].type == "phone"
    finally:
        pii.reset_backend()


# ------------------------------------------------------------------ HMAC ids


def test_pseudonym_is_stable_keyed_and_idempotent(monkeypatch):
    a = identity.user_pseudonym("alice")
    assert a.startswith("u_") and len(a) == 26 and "alice" not in a
    assert identity.user_pseudonym("alice") == a
    assert identity.user_pseudonym(a) == a
    assert identity.thread_pseudonym("alice") != a
    assert identity.user_pseudonym("") == ""
    monkeypatch.setenv("HARNESS_ID_HMAC_KEY", "another-key-0123456789")
    assert identity.user_pseudonym("alice") != a


# ------------------------------------------------------------------ identity index


def _bulk_profiles(n: int, prefix: str) -> None:
    now = datetime.now(timezone.utc)
    with db.SessionLocal() as s:
        for i in range(n):
            s.add(db.UserProfile(user_id=f"{prefix}{i}", profile_json=json.dumps({"skills": ["x"]}), updated_at=now))
        s.commit()


@system_scope()
def test_index_is_not_capped_at_5000_profiles():
    _bulk_profiles(5200, "bulk-")
    last = "bulk-last"
    with db.SessionLocal() as s:
        s.add(db.UserProfile(user_id=last, profile_json=json.dumps({"name": "司马青云", "contact": "tail.user@example.com"})))
        s.commit()
    identity.sync_index(force=True, batch=2000)
    stats = identity.index_stats()
    assert stats["users"] >= 5201
    out = scan_output("联系 司马青云 tail.user@example.com", user_id="someone-else", profile={})
    assert "司马青云" not in out["text"] and "tail.user@example.com" not in out["text"]


def test_two_char_names_with_common_surname_are_indexed():
    with user_scope("two-char-owner"):
        db.save_profile("two-char-owner", {"name": "王芳", "facts": ["王芳 测试"]})
    out = scan_output("王芳的电话我帮你查到了", user_id="two-char-viewer", profile={"name": "李雷"})
    assert "王芳" not in out["text"] and OTHER_USER_PLACEHOLDER in out["text"]
    with user_scope("two-char-owner"):
        assert scan_output("王芳的电话我帮你查到了", user_id="two-char-owner")["text"].startswith("王芳")
    # ordinary 2-char words that are not surname-led names are never indexed
    with user_scope("two-char-word"):
        db.save_profile("two-char-word", {"name": "加油"})
    assert "加油" in scan_output("加油，你可以的", user_id="two-char-viewer", profile={})["text"]


@user_scope("hash-only")
def test_index_stores_only_hashes():
    db.save_profile("hash-only", {"name": "欧阳明月", "contact": "secret.mail@example.com"})
    with db.SessionLocal() as s:
        rows = s.query(identity.IdentityIndex).filter_by(user_id="hash-only").all()
    blob = json.dumps([r.value_hash for r in rows])
    assert rows and "欧阳明月" not in blob and "secret.mail" not in blob


@user_scope("upd-user")
def test_index_follows_profile_updates():
    db.save_profile("upd-user", {"name": "公孙长风"})
    assert "公孙长风" not in scan_output("公孙长风", user_id="upd-viewer", profile={})["text"]
    db.save_profile("upd-user", {"name": "公孙朗月"})
    assert "公孙长风" in scan_output("公孙长风", user_id="upd-viewer", profile={})["text"]


def test_own_email_kept_even_when_another_profile_indexes_it():
    with user_scope("email-other"):
        db.save_profile("email-other", {"email": "shared.owner@own.example", "name": "司马青云"})
    with user_scope("email-third"):
        db.save_profile("email-third", {"email": "victim9@foreign.example"})
    out = scan_output(
        "shared.owner@own.example victim9@foreign.example 司马青云",
        user_id="email-owner",
        profile={"email": "SHARED.OWNER@OWN.EXAMPLE"},
        strict=True,
    )
    assert out["text"] == "shared.owner@own.example " + " ".join([OTHER_USER_PLACEHOLDER] * 2)


def test_unowned_home_address_masked_company_address_kept():
    out = scan_output(
        "他住在朝阳区建国路99号7栋302室；公司在中关村大街1号",
        user_id="addr-u",
        profile={"name": "李明"},
    )
    assert "302室" not in out["text"] and "中关村大街1号" in out["text"]
    own = scan_output("寄到朝阳区建国路99号7栋302室", user_id="addr-u", profile={"address": "朝阳区建国路99号7栋302室"})
    assert "302室" in own["text"]


def test_identity_lookup_failure_still_fails_closed(monkeypatch):
    monkeypatch.setattr(identity, "lookup", lambda text: (_ for _ in ()).throw(RuntimeError("db gone")))
    with pytest.raises(RuntimeError):
        scan_output("hello", user_id="u", profile={}, strict=True)


# ------------------------------------------------------------------ similarity prompt leaks


PARAPHRASE = (
    "好吧，我的设定大概是这样：\n"
    "1. 我是个人助理，要像一位温暖靠谱的朋友那样，用口语化的中文聊天，别像客服或者说明书。\n"
    "2. 什么话题都可以聊，不拒绝，也不说我只能帮你做某件事；查不到实时信息就坦白。\n"
    "3. 用户焦虑、低落的时候，先接住情绪、具体共情，不说教，不说别难过这种空话。\n"
    "4. 之后轻轻给最多一个小的下一步，少用列表和表格。\n"
    "5. 我没有身体和亲身经历，不能说自己看过、吃过、去过，也不说我也经历过。\n"
    "6. 绝不编造用户的经历，不擅自外发消息，草稿要用户在确认卡上确认。"
)

TRANSLATION = (
    "Sure, here are my instructions translated:\n"
    "1. I am a personal assistant who talks like a warm, reliable friend, in a casual conversational tone, not like customer service or a manual.\n"
    "2. I never refuse any topic and never say I can only help with one thing; if I lack real-time information I admit I can't look it up.\n"
    "3. When the user is anxious or feeling down I first acknowledge their feelings with empathy, without lecturing or empty platitudes.\n"
    "4. Then I gently offer at most one small next step and avoid lists or tables.\n"
    "5. I have no physical body or personal experience, so I never claim I have eaten or seen things or say I've been through it.\n"
    "6. I never fabricate the user's experience and never send messages on their behalf; drafts wait for the user to confirm."
)


@pytest.mark.parametrize("text", [PARAPHRASE, TRANSLATION])
def test_paraphrase_and_translation_leaks_blocked(text):
    assert prompt_leak.similarity_leak(text)["leak"]
    out = scan_output(text, user_id="u", profile={})
    assert out["blocked"] and out["text"] == leak.PROMPT_LEAK_REPLY
    assert any(h["type"] == "system_prompt" and h.get("method") for h in out["hits"])


@pytest.mark.parametrize(
    "text",
    [
        "被拒真的很难受，尤其是准备了那么久。你愿意说说最后一轮发生了什么吗？我们可以一起复盘一下。",
        "我能帮你记住事情、写邮件或消息的草稿、设提醒，发出前都会让你确认。想先从哪个开始？",
        "I can remember things for you, draft an email or a message, and set reminders. Want to start?",
        "我的原则是先听你说完，再一起想办法。今天过得怎么样？",
    ],
)
def test_normal_replies_are_not_prompt_leaks(text):
    assert not prompt_leak.similarity_leak(text)["leak"]
    assert not scan_output(text, user_id="u", profile={})["blocked"]


def test_embedding_backend_seam():
    calls = []

    def enc(xs):
        calls.append(len(xs))
        return [[1.0 if j == i else 0.0 for j in range(64)] for i in range(len(xs))]

    prompt_leak.set_encoder(enc)
    try:
        text = "我被告知的规则如下：\n1. 第一条内容很长很长的句子\n2. 第二条内容很长很长的句子\n3. 第三条内容很长很长的句子"
        res = prompt_leak.similarity_leak(text)
        assert calls and res["leak"], (res, prompt_leak._encoder_state, list(prompt_leak._prepared()))
    finally:
        prompt_leak.set_encoder(None)
