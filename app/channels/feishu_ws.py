"""Feishu event long connection (WebSocket) — no public webhook URL needed.

Uses ``lark.ws.Client`` + ``EventDispatcherHandler`` for:
  - ``im.message.receive_v1``
  - ``card.action.trigger``

Inbound is normalized via ``parse_feishu_webhook`` then
``app.channels.gateway.handle_inbound`` / ``deliver_result``.

Env:
  FEISHU_APP_ID / FEISHU_APP_SECRET  (required)
  FEISHU_ENABLED                     default OFF; legacy alias FEISHU_WS_ENABLED=1
  FEISHU_WS_LOG_LEVEL                DEBUG|INFO|WARNING|ERROR (default INFO)

Console (user must do once):
  开放平台 → 事件与回调 → 订阅方式选「使用长连接接收事件/回调」
  并订阅 im.message.receive_v1 + card.action.trigger；机器人能力与相应权限。

Note: lark-oapi 1.7.x still drops CARD frames in ``_handle_data_frame``;
we monkey-patch that so interactive-card callbacks work over WS.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

logger = logging.getLogger("feishu_ws")

_state: dict[str, Any] = {
    "enabled": False,
    "running": False,
    "connected": False,
    "started_at": None,
    "last_event_at": None,
    "last_error": None,
    "events_ok": 0,
    "events_err": 0,
    "thread": None,
    "proc": None,
    "executor": None,
}

_lock = threading.Lock()


def ws_enabled() -> bool:
    """Default OFF: requires FEISHU_ENABLED=1 (or legacy FEISHU_WS_ENABLED=1) plus credentials."""
    from app.channels import feishu_client
    from app.channels.settings import feishu_enabled

    return feishu_enabled() and feishu_client.is_configured()


def _external_ws_alive() -> bool:
    """True if a scripts/feishu_ws supervisor/worker is running (even if not our child)."""
    try:
        import subprocess

        out = subprocess.check_output(["ps", "ax", "-o", "command="], text=True)
    except Exception:
        return False
    for line in out.splitlines():
        if "scripts/feishu_ws.py" in line:
            return True
        if "feishu_ws.run_forever" in line:
            return True
    return False


def status() -> dict[str, Any]:
    with _lock:
        proc = _state.get("proc")
        proc_alive = bool(proc is not None and getattr(proc, "poll", lambda: 1)() is None)
        th = _state.get("thread")
        th_alive = bool(th is not None and th.is_alive())
        if proc is not None and not proc_alive and _state.get("running"):
            # child exited — surface exit code once
            code = proc.poll()
            if not _state.get("last_error"):
                _state["last_error"] = f"subprocess_exit_{code}"
            _state["running"] = False
            _state["connected"] = False
        running = proc_alive or th_alive
        external = False
        if not running:
            external = _external_ws_alive()
            running = external
        return {
            "enabled": bool(_state["enabled"]) or running or ws_enabled(),
            "running": running,
            "connected": (bool(_state["connected"]) if (proc_alive or th_alive) else external),
            "mode": (
                "subprocess"
                if proc_alive
                else ("thread" if th_alive else ("external" if external else None))
            ),
            "pid": getattr(proc, "pid", None) if proc_alive else None,
            "started_at": _state["started_at"],
            "last_event_at": _state["last_event_at"],
            # Health is public; inbound-handler exceptions may contain user data.
            "last_error": "channel_error" if _state["last_error"] else None,
            "events_ok": int(_state["events_ok"] or 0),
            "events_err": int(_state["events_err"] or 0),
        }


def _mark(**kwargs: Any) -> None:
    with _lock:
        _state.update(kwargs)


def _patch_card_frames() -> None:
    """lark-oapi drops MessageType.CARD; treat CARD like EVENT (issue #126)."""
    try:
        from lark_oapi.ws import client as ws_client
        from lark_oapi.ws.enum import MessageType
        from lark_oapi.ws.const import (
            HEADER_BIZ_RT,
            HEADER_MESSAGE_ID,
            HEADER_SEQ,
            HEADER_SUM,
            HEADER_TRACE_ID,
            HEADER_TYPE,
        )
        from lark_oapi.core.const import UTF_8
        from lark_oapi.core.json import JSON
        from lark_oapi.ws.model import Response
        import base64
        import http
        import time as _time
    except Exception as exc:
        logger.warning("cannot patch CARD frames: %s", exc)
        return

    if getattr(ws_client.Client, "_personal_agent_card_patched", False):
        return

    _get_by_key = ws_client._get_by_key

    async def _handle_data_frame(self, frame):  # type: ignore[no-untyped-def]
        hs = frame.headers
        msg_id = _get_by_key(hs, HEADER_MESSAGE_ID)
        trace_id = _get_by_key(hs, HEADER_TRACE_ID)
        sum_ = _get_by_key(hs, HEADER_SUM)
        seq = _get_by_key(hs, HEADER_SEQ)
        type_ = _get_by_key(hs, HEADER_TYPE)

        pl = frame.payload
        if int(sum_) > 1:
            pl = self._combine(msg_id, int(sum_), int(seq), pl)
            if pl is None:
                return

        message_type = MessageType(type_)
        logger.debug(
            "feishu_ws recv type=%s id=%s trace=%s",
            message_type.value,
            msg_id,
            trace_id,
        )

        resp = Response(code=http.HTTPStatus.OK)
        try:
            start = int(round(_time.time() * 1000))
            if message_type in (MessageType.EVENT, MessageType.CARD):
                result = self._event_handler._do_without_validation(pl)
            else:
                return
            end = int(round(_time.time() * 1000))
            header = hs.add()
            header.key = HEADER_BIZ_RT
            header.value = str(end - start)
            if result is not None:
                resp.data = base64.b64encode(JSON.marshal(result).encode(UTF_8))
        except Exception as e:
            logger.error(
                "feishu_ws handle failed type=%s id=%s err=%s",
                message_type.value,
                msg_id,
                e,
            )
            resp = Response(code=http.HTTPStatus.INTERNAL_SERVER_ERROR)

        frame.payload = JSON.marshal(resp).encode(UTF_8)
        await self._write_message(frame.SerializeToString())

    ws_client.Client._handle_data_frame = _handle_data_frame  # type: ignore[method-assign]
    ws_client.Client._personal_agent_card_patched = True  # type: ignore[attr-defined]
    logger.info("patched lark_oapi ws CARD frame dispatch")


def _sdk_to_dict(data: Any) -> dict[str, Any]:
    import lark_oapi as lark

    try:
        raw = lark.JSON.marshal(data)
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8")
        obj = json.loads(raw) if isinstance(raw, str) else raw
        return obj if isinstance(obj, dict) else {"raw": obj}
    except Exception:
        # best-effort attribute walk
        out: dict[str, Any] = {}
        header = getattr(data, "header", None)
        event = getattr(data, "event", None)
        if header is not None:
            try:
                out["header"] = json.loads(lark.JSON.marshal(header))
            except Exception:
                out["header"] = {}
        if event is not None:
            try:
                out["event"] = json.loads(lark.JSON.marshal(event))
            except Exception:
                out["event"] = {}
        return out


def _is_bot_message(payload: dict[str, Any]) -> bool:
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    st = str(sender.get("sender_type") or "").lower()
    return st in ("app", "bot")


def _process_inbound(payload: dict[str, Any], *, kind: str) -> None:
    """Run shared webhook → graph → deliver path off the WS callback thread."""
    from app.channels.feishu import parse_feishu_webhook

    try:
        parsed = parse_feishu_webhook(payload, skip_auth=True)
        if parsed.get("type") != "inbound":
            logger.info("feishu_ws skip kind=%s parsed_type=%s", kind, parsed.get("type"))
            return
        from app.channels.gateway import deliver_result, handle_inbound

        result = handle_inbound(parsed, "feishu")
        inbound = parsed.get("inbound") or {}
        deliver_result("feishu", inbound if isinstance(inbound, dict) else {}, result)
        _mark(
            last_event_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            events_ok=int(_state["events_ok"] or 0) + 1,
            last_error=None,
        )
        logger.info(
            "feishu_ws handled kind=%s status=%s",
            kind,
            result.get("status"),
        )
    except Exception as exc:
        _mark(
            last_error=f"{type(exc).__name__}: {exc}",
            events_err=int(_state["events_err"] or 0) + 1,
        )
        logger.exception("feishu_ws process failed kind=%s", kind)


def _submit(payload: dict[str, Any], *, kind: str) -> None:
    ex: Optional[ThreadPoolExecutor] = _state.get("executor")
    if ex is None:
        _process_inbound(payload, kind=kind)
        return
    ex.submit(_process_inbound, payload, kind=kind)


def build_event_handler():
    """Build dispatcher for IM + card callbacks (encrypt/token args must be "")."""
    import lark_oapi as lark
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTrigger,
        P2CardActionTriggerResponse,
    )

    def on_message(data: lark.im.v1.P2ImMessageReceiveV1) -> None:
        payload = _sdk_to_dict(data)
        if _is_bot_message(payload):
            return
        # Feishu requires <3s without exception — queue work, return immediately
        _submit(payload, kind="message")

    def on_card(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        payload = _sdk_to_dict(data)
        # Flatten so parse_feishu_webhook sees action/operator
        event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
        if isinstance(event, dict):
            if "action" in event and "action" not in payload:
                payload = dict(payload)
                payload["action"] = event.get("action")
            if "operator" in event and "operator" not in payload:
                payload = dict(payload)
                payload["operator"] = event.get("operator")
            # open_message_id / open_chat_id helpers used by parser
            ctx = event.get("context") if isinstance(event.get("context"), dict) else {}
            if ctx.get("open_message_id"):
                payload.setdefault("open_message_id", ctx["open_message_id"])
            if ctx.get("open_chat_id"):
                payload.setdefault("open_chat_id", ctx["open_chat_id"])
        _submit(payload, kind="card_action")
        return P2CardActionTriggerResponse(
            {
                "toast": {
                    "type": "info",
                    "content": "已收到，助理处理中…",
                }
            }
        )

    return (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .register_p2_card_action_trigger(on_card)
        .build()
    )


def _log_level() -> Any:
    import lark_oapi as lark

    name = (os.getenv("FEISHU_WS_LOG_LEVEL") or "INFO").strip().upper()
    return getattr(lark.LogLevel, name, lark.LogLevel.INFO)


def run_forever() -> None:
    """Blocking: connect Feishu WS and serve events (for scripts/feishu_ws.py)."""
    from pathlib import Path as _Path
    from dotenv import load_dotenv

    # Absolute path: uvicorn/thread cwd must not hide .env
    _root = _Path(__file__).resolve().parents[2]
    load_dotenv(_root / ".env", override=False)

    from app.channels import feishu_client
    import lark_oapi as lark

    if not feishu_client.is_configured():
        raise RuntimeError("FEISHU_APP_ID / FEISHU_APP_SECRET required for long connection")

    import asyncio

    # lark_oapi.ws.client caches a module-level loop at import time.
    # When this worker runs under uvicorn, that loop may be the server's —
    # rebind a fresh loop for this thread before constructing Client.
    new_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(new_loop)
    import lark_oapi.ws.client as _ws_client

    _ws_client.loop = new_loop

    _patch_card_frames()
    if _state.get("executor") is None:
        _state["executor"] = ThreadPoolExecutor(max_workers=4, thread_name_prefix="feishu_ws")

    handler = build_event_handler()
    app_id = feishu_client.app_id()
    app_secret = feishu_client.app_secret()
    logger.info(
        "feishu_ws creds app_id_len=%s secret_len=%s app_id_prefix=%s",
        len(app_id),
        len(app_secret),
        app_id[:12] if app_id else "",
    )
    # Note: avoid hammering /callback/ws/endpoint — repeated discovery itself
    # can trigger transient 1000040345. Let lark.ws.Client.start() do it once.

    cli = lark.ws.Client(
        app_id,
        app_secret,
        event_handler=handler,
        log_level=_log_level(),
    )
    _mark(
        enabled=True,
        running=True,
        connected=True,  # start() blocks after connect; mark optimistic
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        last_error=None,
    )
    logger.info("feishu_ws starting long connection (app_id=%s…)", app_id[:12])
    # Single attempt here — scripts/feishu_ws.py reconnects with long backoff.
    # Rapid retries against /callback/ws/endpoint trigger transient 1000040345.
    try:
        cli.start()
    except Exception as exc:
        _mark(running=False, connected=False, last_error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        _mark(running=False, connected=False)


def start_background(*, force: bool = False) -> dict[str, Any]:
    """Start long connection as a subprocess (preferred) or daemon thread.

    Subprocess avoids sharing uvicorn's asyncio loop with ``lark.ws.Client``.
    Set ``FEISHU_WS_MODE=thread`` to force in-process thread instead.
    """
    if not force and not ws_enabled():
        _mark(enabled=False)
        return {"started": False, "reason": "disabled_or_unconfigured", **status()}

    # Avoid duplicate long connections (Feishu allows one WS per app).
    if not force and _external_ws_alive():
        _mark(enabled=True, connected=True, running=True)
        return {"started": False, "reason": "external_already_running", **status()}

    mode = (os.getenv("FEISHU_WS_MODE") or "subprocess").strip().lower()
    with _lock:
        proc = _state.get("proc")
        if proc is not None and getattr(proc, "poll", lambda: 1)() is None:
            return {"started": False, "reason": "already_running", "mode": "subprocess", **status()}
        th = _state.get("thread")
        if th is not None and th.is_alive():
            return {"started": False, "reason": "already_running", "mode": "thread", **status()}

    if mode != "thread":
        import subprocess
        import sys
        from pathlib import Path as _Path

        root = _Path(__file__).resolve().parents[2]
        script = root / "scripts" / "feishu_ws.py"
        log_path = root / "data" / "feishu_ws.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logf = open(log_path, "a", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, str(script)],
            cwd=str(root),
            stdout=logf,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        _mark(enabled=True, proc=proc, running=True, connected=True,
              started_at=time.strftime("%Y-%m-%dT%H:%M:%S"), last_error=None)
        time.sleep(1.5)
        alive = proc.poll() is None
        if not alive:
            _mark(running=False, connected=False, last_error="subprocess_exited_early")
            return {"started": False, "reason": "subprocess_exited_early", "mode": "subprocess",
                    "log": str(log_path), **status()}
        return {"started": True, "mode": "subprocess", "pid": proc.pid, "log": str(log_path), **status()}

    def _target() -> None:
        try:
            run_forever()
        except Exception as exc:
            logger.exception("feishu_ws background exited: %s", exc)
            _mark(last_error=f"{type(exc).__name__}: {exc}", running=False, connected=False)

    thread = threading.Thread(target=_target, name="feishu-ws", daemon=True)
    _mark(enabled=True, thread=thread)
    thread.start()
    time.sleep(0.5)
    return {"started": True, "mode": "thread", **status()}


def stop_background() -> None:
    """Best-effort: cannot cleanly stop lark.ws.Client; mark disabled for status."""
    _mark(enabled=False)
    logger.info("feishu_ws marked disabled (process exit stops the socket)")
