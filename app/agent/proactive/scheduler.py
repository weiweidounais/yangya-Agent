"""In-process asyncio scheduler started with FastAPI lifespan/startup."""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app.agent.proactive.config import ProactiveConfig, get_config
from app.agent.proactive import jobs
from app.data_access import system_scope

logger = logging.getLogger("app.agent.proactive")

_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_loop: Optional[asyncio.AbstractEventLoop] = None
_started_at: Optional[str] = None
_last_tick: Optional[dict[str, Any]] = None
_next_hint: Optional[str] = None


def _estimate_next_run(cfg: ProactiveConfig) -> Optional[str]:
    """Cheap next-run hint for /health (ISO local tz): the next tick."""
    try:
        tz = ZoneInfo(cfg.tz)
    except Exception:
        tz = ZoneInfo("Asia/Shanghai")
    return (datetime.now(tz) + timedelta(seconds=cfg.tick_seconds)).isoformat()


async def _async_loop(cfg: ProactiveConfig) -> None:
    global _last_tick, _next_hint
    logger.info(
        "proactive scheduler running enabled=%s tick=%.1fs tz=%s",
        cfg.enabled,
        cfg.tick_seconds,
        cfg.tz,
    )
    while not _stop.is_set():
        try:
            if cfg.enabled:
                # Re-read env each tick so ops can flip without restart? soft: refresh config
                live = get_config()
                if live.enabled:
                    with system_scope():
                        _last_tick = await asyncio.to_thread(jobs.run_tick, live)
                    _next_hint = _estimate_next_run(live)
                else:
                    _next_hint = None
            else:
                _next_hint = None
        except Exception as exc:
            logger.warning("proactive tick error: %s", exc)
            _last_tick = {"ok": False, "error": str(exc)[:300]}
        try:
            await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, _stop.wait, cfg.tick_seconds),
                timeout=cfg.tick_seconds + 5,
            )
        except asyncio.TimeoutError:
            pass
        if _stop.is_set():
            break
        # sleep remainder if wait returned early with False
        if not _stop.is_set():
            await asyncio.sleep(0.05)


def _thread_main(cfg: ProactiveConfig) -> None:
    global _loop
    loop = asyncio.new_event_loop()
    _loop = loop
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_async_loop(cfg))
    finally:
        try:
            loop.close()
        except Exception:
            pass
        _loop = None


def start(cfg: Optional[ProactiveConfig] = None) -> dict[str, Any]:
    """Start background scheduler thread. Idempotent."""
    global _thread, _started_at, _next_hint
    cfg = cfg or get_config()
    with _lock:
        if _thread is not None and _thread.is_alive():
            return status()
        _stop.clear()
        _started_at = datetime.now().isoformat()
        _next_hint = _estimate_next_run(cfg) if cfg.enabled else None
        t = threading.Thread(
            target=_thread_main,
            args=(cfg,),
            name="proactive-scheduler",
            daemon=True,
        )
        _thread = t
        t.start()
        logger.info("proactive scheduler started enabled=%s", cfg.enabled)
        return status()


def stop(timeout: float = 5.0) -> dict[str, Any]:
    global _thread
    with _lock:
        _stop.set()
        t = _thread
        _thread = None
    if t and t.is_alive():
        t.join(timeout=timeout)
    return status()


def status() -> dict[str, Any]:
    cfg = get_config()
    alive = _thread is not None and _thread.is_alive()
    return {
        "proactive_enabled": bool(cfg.enabled),
        "scheduler_alive": alive,
        "started_at": _started_at,
        "next_run_hint": _next_hint if cfg.enabled else None,
        "last_tick": {
            "ok": (_last_tick or {}).get("ok"),
            "elapsed_ms": (_last_tick or {}).get("elapsed_ms"),
            "users": (_last_tick or {}).get("users"),
            "budget_stop": (_last_tick or {}).get("budget_stop"),
        }
        if _last_tick
        else None,
        "config": cfg.as_dict(),
    }


def trigger_tick_now() -> dict[str, Any]:
    """Synchronous one-shot tick (tests / admin). Honors enabled flag."""
    global _last_tick
    cfg = get_config()
    if not cfg.enabled:
        return {"ok": False, "reason": "disabled"}
    with system_scope():
        _last_tick = jobs.run_tick(cfg)
    return _last_tick
