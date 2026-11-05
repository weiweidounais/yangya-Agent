"""QA: /api/memory must not return contact details, sandbox values or long bodies."""

from app import db

LONG_SUMMARY = "用户最近在筹备周末的徒步活动，想要一份装备清单。" * 30
SANDBOX_VALUE = "Synthetic-Secret-Project-42"


def test_memory_endpoint_hides_contact_sandbox_and_long_bodies(client, uid):
    db.save_profile(uid, {
        "name": "张三",
        "phone": "13800001111",
        "email": "zhangsan.private@example.com",
        "facts": ["喜欢爬山", f"正在参与 {SANDBOX_VALUE}"],
        "dialogue_summary": LONG_SUMMARY,
        "privacy_sandbox": [SANDBOX_VALUE],
    })
    r = client.get(f"/api/memory/{uid}")
    assert r.status_code == 200
    body = r.text
    assert "13800001111" not in body and "zhangsan.private@example.com" not in body
    assert SANDBOX_VALUE not in body
    assert LONG_SUMMARY not in body
    prof = r.json()["data"]["profile"]
    assert "phone" not in prof and "email" not in prof
    assert prof["name"] == "张三"
    assert prof["facts"][0] == "喜欢爬山"
    assert prof["dialogue_summary"]["chars"] == len(LONG_SUMMARY)
    assert prof["dialogue_summary"]["full_text"] == "hidden"
