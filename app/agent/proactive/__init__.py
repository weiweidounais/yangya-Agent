"""Proactive execution: scheduler wake without user chat message."""

from __future__ import annotations

from app.agent.proactive.config import ProactiveConfig, get_config
from app.agent.proactive import jobs, scheduler, store

__all__ = [
    "ProactiveConfig",
    "get_config",
    "jobs",
    "scheduler",
    "store",
    "start",
    "stop",
    "status",
    "trigger_tick_now",
    "list_pending",
]


def start(**kwargs):
    return scheduler.start(**kwargs)


def stop(**kwargs):
    return scheduler.stop(**kwargs)


def status():
    return scheduler.status()


def trigger_tick_now():
    return scheduler.trigger_tick_now()


def list_pending(*args, **kwargs):
    return store.list_pending(*args, **kwargs)
