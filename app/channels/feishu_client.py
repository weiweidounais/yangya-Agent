"""Feishu / Lark Open API client — tenant token, reply, interactive card.

Env (do not invent secrets; leave empty until user fills):
  FEISHU_APP_ID
  FEISHU_APP_SECRET
  FEISHU_VERIFICATION_TOKEN
  FEISHU_ENCRYPT_KEY
  FEISHU_API_BASE   default https://open.feishu.cn
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from base64 import b64decode
from typing import Any, Optional

import httpx

_token_cache: dict[str, Any] = {"token": "", "expires_at": 0.0}


def app_id() -> str:
    return (os.getenv("FEISHU_APP_ID") or "").strip()


def app_secret() -> str:
    return (os.getenv("FEISHU_APP_SECRET") or "").strip()


def verification_token() -> str:
    return (os.getenv("FEISHU_VERIFICATION_TOKEN") or "").strip()


def encrypt_key() -> str:
    return (os.getenv("FEISHU_ENCRYPT_KEY") or "").strip()


def api_base() -> str:
    return (os.getenv("FEISHU_API_BASE") or "https://open.feishu.cn").rstrip("/")


def is_configured() -> bool:
    return bool(app_id() and app_secret())


def verify_url_token(payload: dict[str, Any]) -> bool:
    """If FEISHU_VERIFICATION_TOKEN set, payload token must match."""
    expected = verification_token()
    if not expected:
        return True
    got = (
        payload.get("token")
        or (payload.get("header") or {}).get("token")
        or ""
    )
    return hmac.compare_digest(str(got), expected)


def verify_signature(
    *,
    timestamp: str,
    nonce: str,
    body: bytes | str,
    signature: str,
) -> bool:
    """x-lark-signature == SHA256(timestamp + nonce + encrypt_key + body)."""
    key = encrypt_key()
    if not key:
        return True  # optional in dev
    if not (timestamp and nonce and signature):
        return False
    body_str = body.decode("utf-8", errors="replace") if isinstance(body, (bytes, bytearray)) else str(body)
    computed = hashlib.sha256(f"{timestamp}{nonce}{key}{body_str}".encode("utf-8")).hexdigest()
    try:
        return hmac.compare_digest(computed.encode(), signature.encode())
    except Exception:
        return False


def decrypt_encrypt_blob(encrypt_b64: str) -> dict[str, Any]:
    """Decrypt Feishu event Encrypt field (AES-256-CBC, key=SHA256(encrypt_key))."""
    key = encrypt_key()
    if not key:
        raise RuntimeError("FEISHU_ENCRYPT_KEY required to decrypt encrypted webhook")
    try:
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
    except ImportError as exc:
        raise RuntimeError("cryptography package required for Feishu encrypt payloads") from exc

    key_bytes = hashlib.sha256(key.encode("utf-8")).digest()
    raw = b64decode(encrypt_b64)
    iv, ciphertext = raw[:16], raw[16:]
    cipher = Cipher(algorithms.AES(key_bytes), modes.CBC(iv), backend=default_backend())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = PKCS7(128).unpadder()
    plain = unpadder.update(padded) + unpadder.finalize()
    return json.loads(plain.decode("utf-8"))


def maybe_unwrap_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """If body is {\"encrypt\": \"...\"}, decrypt to event JSON."""
    enc = payload.get("encrypt")
    if isinstance(enc, str) and enc and not payload.get("event") and not payload.get("type"):
        return decrypt_encrypt_blob(enc)
    return payload


def get_tenant_access_token(*, force: bool = False) -> str:
    if not is_configured():
        raise RuntimeError("FEISHU_APP_ID / FEISHU_APP_SECRET not set")
    now = time.time()
    if not force and _token_cache["token"] and _token_cache["expires_at"] > now + 60:
        return str(_token_cache["token"])
    url = f"{api_base()}/open-apis/auth/v3/tenant_access_token/internal"
    with httpx.Client(timeout=20) as client:
        r = client.post(url, json={"app_id": app_id(), "app_secret": app_secret()})
        r.raise_for_status()
        data = r.json()
    code = data.get("code")
    if code is None:
        code = -1
    if int(code) != 0 or not data.get("tenant_access_token"):
        raise RuntimeError(f"tenant_access_token failed: code={data.get('code')} msg={data.get('msg')}")
    token = str(data["tenant_access_token"])
    expire = int(data.get("expire") or 7200)
    _token_cache["token"] = token
    _token_cache["expires_at"] = now + max(expire - 300, 60)
    return token


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {get_tenant_access_token()}",
        "Content-Type": "application/json; charset=utf-8",
    }


def send_text(
    *,
    receive_id: str,
    text: str,
    receive_id_type: str = "open_id",
) -> dict[str, Any]:
    """POST /open-apis/im/v1/messages"""
    if not is_configured():
        return {"ok": False, "skipped": True, "reason": "feishu_not_configured"}
    url = f"{api_base()}/open-apis/im/v1/messages"
    body = {
        "receive_id": receive_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    with httpx.Client(timeout=20) as client:
        r = client.post(
            url,
            params={"receive_id_type": receive_id_type},
            headers=_auth_headers(),
            json=body,
        )
        data = r.json() if r.content else {}
        if r.status_code >= 400:
            return {"ok": False, "status_code": r.status_code, "data": data}
        return {"ok": int(data.get("code", -1)) == 0, "data": data}


def send_interactive_card(
    *,
    receive_id: str,
    card: dict[str, Any],
    receive_id_type: str = "open_id",
) -> dict[str, Any]:
    if not is_configured():
        return {"ok": False, "skipped": True, "reason": "feishu_not_configured"}
    url = f"{api_base()}/open-apis/im/v1/messages"
    # strip internal-only keys from card copy
    card_body = {k: v for k, v in card.items() if k not in ("thread_id", "preview_len")}
    body = {
        "receive_id": receive_id,
        "msg_type": "interactive",
        "content": json.dumps(card_body, ensure_ascii=False),
    }
    with httpx.Client(timeout=20) as client:
        r = client.post(
            url,
            params={"receive_id_type": receive_id_type},
            headers=_auth_headers(),
            json=body,
        )
        data = r.json() if r.content else {}
        if r.status_code >= 400:
            return {"ok": False, "status_code": r.status_code, "data": data}
        return {"ok": int(data.get("code", -1)) == 0, "data": data}


def reply_message(
    *,
    message_id: str,
    text: str = "",
    card: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Reply to an inbound message_id."""
    if not is_configured():
        return {"ok": False, "skipped": True, "reason": "feishu_not_configured"}
    if not message_id:
        return {"ok": False, "reason": "no_message_id"}
    url = f"{api_base()}/open-apis/im/v1/messages/{message_id}/reply"
    if card:
        card_body = {k: v for k, v in card.items() if k not in ("thread_id", "preview_len")}
        payload = {
            "msg_type": "interactive",
            "content": json.dumps(card_body, ensure_ascii=False),
        }
    else:
        payload = {
            "msg_type": "text",
            "content": json.dumps({"text": text or ""}, ensure_ascii=False),
        }
    with httpx.Client(timeout=20) as client:
        r = client.post(url, headers=_auth_headers(), json=payload)
        data = r.json() if r.content else {}
        if r.status_code >= 400:
            return {"ok": False, "status_code": r.status_code, "data": data}
        return {"ok": int(data.get("code", -1)) == 0, "data": data}


def deliver_outbound(
    *,
    external_user_id: str,
    text: str,
    card: Optional[dict[str, Any]] = None,
    message_id: str = "",
) -> dict[str, Any]:
    """Prefer reply-to message; else DM by open_id; card if present."""
    if not is_configured():
        return {"ok": False, "skipped": True, "reason": "feishu_not_configured", "queued_locally": True}
    try:
        if message_id and card:
            return reply_message(message_id=message_id, card=card)
        if message_id and text:
            return reply_message(message_id=message_id, text=text)
        if card and external_user_id:
            return send_interactive_card(receive_id=external_user_id, card=card)
        if external_user_id and text:
            return send_text(receive_id=external_user_id, text=text)
    except Exception as exc:
        return {"ok": False, "reason": "feishu_deliver_error", "error": f"{type(exc).__name__}"}
    return {"ok": False, "reason": "no_target"}


def config_status() -> dict[str, Any]:
    return {
        "configured": is_configured(),
        "has_verification_token": bool(verification_token()),
        "has_encrypt_key": bool(encrypt_key()),
        "api_base": api_base(),
        "app_id_set": bool(app_id()),
    }
