from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app import secrets_vault as vault
from app.db import SessionLocal
from app.data_access import user_scope, system_scope

PASSWORD = "Tr0ub4dor&3-vault"


@user_scope("u-vault-1")
def test_round_trip_and_masking(vault_key):
    item = vault.save_secret(
        "u-vault-1", "account_login",
        {"platform_name": "网盘", "account": "13812345678", "password": PASSWORD}, purpose="查看消息",
    )
    assert item["label"] == "账号登录信息"
    previews = {m["name"]: m["preview"] for m in item["masked"]}
    assert previews == {"platform_name": "网盘", "account": "138****5678", "password": "已保存"}
    assert PASSWORD not in repr(item)
    assert vault.load_secret_values("u-vault-1", "account_login") == {
        "platform_name": "网盘", "account": "13812345678", "password": PASSWORD,
    }


@user_scope("u-vault-2")
def test_ciphertext_at_rest(vault_key, data_dir):
    vault.save_secret("u-vault-2", "account_login", {"platform_name": "网盘", "cookie": "lt=" + "x" * 40 + PASSWORD})
    with SessionLocal() as s:
        row = s.execute(select(vault.UserSecret).where(vault.UserSecret.user_id == "u-vault-2")).scalar_one()
        assert PASSWORD not in row.ciphertext and PASSWORD not in row.masked_json
    for db_file in data_dir.glob("*.db*"):
        assert PASSWORD.encode() not in db_file.read_bytes(), db_file.name


@user_scope("alice")
def test_per_user_isolation(vault_key):
    vault.save_secret("alice", "phone", {"phone": "13900001111"})
    assert [i["key"] for i in vault.list_secrets("bob")] == []
    assert vault.load_secret_values("bob", "phone") is None
    assert vault.delete_secret("bob", "phone") is False
    assert vault.load_secret_values("alice", "phone") == {"phone": "13900001111"}


@system_scope()
def test_ciphertext_bound_to_owner_and_key(vault_key):
    vault.save_secret("owner", "id_card", {"id_number": "11010519491231002X"})
    vault.save_secret("thief", "id_card", {"id_number": "110105194912310011"})
    with SessionLocal() as s:
        rows = {r.user_id: r for r in s.execute(select(vault.UserSecret).where(vault.UserSecret.key == "id_card")).scalars()}
        rows["thief"].ciphertext = rows["owner"].ciphertext
        s.commit()
    assert vault.load_secret_values("thief", "id_card") is None
    assert vault.load_secret_values("owner", "id_card") == {"id_number": "11010519491231002X"}


@user_scope("rot")
def test_key_rotation(vault_key, monkeypatch):
    vault.save_secret("rot", "phone", {"phone": "13700002222"})
    monkeypatch.setenv("SECRETS_FERNET_KEY", f"{Fernet.generate_key().decode()},{vault_key}")
    assert vault.load_secret_values("rot", "phone") == {"phone": "13700002222"}


@user_scope("wk")
def test_wrong_key_cannot_decrypt(vault_key, monkeypatch):
    vault.save_secret("wk", "phone", {"phone": "13700003333"})
    monkeypatch.setenv("SECRETS_FERNET_KEY", Fernet.generate_key().decode())
    assert vault.load_secret_values("wk", "phone") is None


@user_scope("nokey")
def test_missing_or_bad_key_refuses(monkeypatch):
    monkeypatch.delenv("SECRETS_FERNET_KEY", raising=False)
    assert vault.is_configured() is False
    with pytest.raises(vault.VaultUnavailable):
        vault.save_secret("nokey", "phone", {"phone": "13700004444"})
    monkeypatch.setenv("SECRETS_FERNET_KEY", "not-a-key")
    with pytest.raises(vault.VaultUnavailable):
        vault.save_secret("nokey", "phone", {"phone": "13700004444"})
    assert vault.list_secrets("nokey") == []


@pytest.mark.parametrize(
    "key,values",
    [
        ("account_login", {"platform_name": "网盘", "account": "a"}),
        ("account_login", {}),
        ("phone", {"phone": "12345"}),
        ("id_card", {"id_number": "abc"}),
        ("account_login", {"account": "a", "password": "b"}),
        ("bank_card", {"number": "1"}),
        ("account_login", "not a dict"),
    ],
)
@user_scope("val")
def test_validation_errors_never_echo_values(vault_key, key, values):
    with pytest.raises(vault.SecretValidationError) as exc:
        vault.save_secret("val", key, values)
    for v in (values.values() if isinstance(values, dict) else [values]):
        assert str(v) not in str(exc.value) or len(str(v)) <= 1


@user_scope("drop")
def test_unknown_fields_are_dropped(vault_key):
    vault.save_secret("drop", "phone", {"phone": "138 1234 5678", "extra": "zzz"})
    assert vault.load_secret_values("drop", "phone") == {"phone": "13812345678"}
