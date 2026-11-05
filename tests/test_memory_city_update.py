"""Changing the memory city to 杭州 takes effect: replies no longer say 上海."""

from __future__ import annotations

from app import db
from app.agent.memory import profile as profile_mem


def _seed(client, uid):
    r = client.post("/api/profile", json={
        "user_id": uid,
        "profile": {"city": "上海", "occupation": "后端工程师", "preferences": ["远程友好团队"]},
    })
    assert r.status_code == 200
    assert db.get_profile(uid)["city"] == "上海"
    profile_mem.merge(uid, {"dialogue_summary": "用户住在上海。偏好远程友好团队。"})


def _set_city(client, uid, city):
    r = client.patch(f"/api/memory/{uid}/facts/city", json={"value": city})
    assert r.status_code == 200, r.text


def test_prompt_uses_new_city_and_marks_old_one(client, uid, fake_llm):
    _seed(client, uid)
    _set_city(client, uid, "杭州")
    text = profile_mem.format_for_prompt(uid)
    assert "城市：杭州" in text
    assert "用户已把城市改为「杭州」" in text
    assert "「上海」是旧城市" in text
    summary = db.get_profile(uid)["dialogue_summary"]
    assert "上海" not in summary and "远程友好" in summary


def test_thread_checkpoint_does_not_resurrect_old_city(client, uid, fake_llm):
    _seed(client, uid)
    first = client.post("/api/chat", json={"user_id": uid, "message": "我现在住在哪个城市？"}).json()
    tid = first["thread_id"]
    assert "城市：上海" in fake_llm.all_text()  # sanity: the thread captured 上海
    _set_city(client, uid, "杭州")
    from app.agent.subgraphs.profile import load_profile
    from app.graph import get_thread_snapshot

    state = (get_thread_snapshot(tid).get("values") or {})
    assert (state.get("profile") or {}).get("city") == "上海"  # stale copy in checkpoint
    assert load_profile({**state, "user_id": uid})["city"] == "杭州"
    fake_llm.calls.clear()
    again = client.post("/api/chat", json={
        "user_id": uid, "thread_id": tid, "message": "我现在住在哪个城市？",
    }).json()
    assert again["data"]["profile"]["city"] == "杭州"
    memory = fake_llm.all_text().split("<memory>")[-1].split("</memory>")[0]
    assert "城市：杭州" in memory
    assert "城市：上海" not in memory


def test_chat_reply_prompt_after_switch(client, uid, fake_llm):
    _seed(client, uid)
    _set_city(client, uid, "杭州")
    fake_llm.calls.clear()
    client.post("/api/chat", json={"user_id": uid, "message": "我现在住的城市是哪？"})
    memory = fake_llm.all_text().split("<memory>")[-1].split("</memory>")[0]
    assert "城市：杭州" in memory
    assert "城市：上海" not in memory
