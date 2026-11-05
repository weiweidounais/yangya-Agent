"""Trusted identity, installed only by authenticated server entrypoints."""
from contextlib import contextmanager
from contextvars import ContextVar

_identity = ContextVar("private_identity", default=None)
_SYSTEM = object()


class IdentityRequired(PermissionError):
    pass


class NotFound(LookupError):
    pass


def current_user():
    identity = _identity.get()
    if identity is None:
        raise IdentityRequired("Trusted identity required")
    return None if identity is _SYSTEM else identity


@contextmanager
def user_scope(user_id):
    if not isinstance(user_id, str) or not user_id.strip():
        raise IdentityRequired("Trusted identity required")
    token = _identity.set(user_id)
    try:
        yield user_id
    finally:
        _identity.reset(token)


@contextmanager
def system_scope():
    token = _identity.set(_SYSTEM)
    try:
        yield
    finally:
        _identity.reset(token)
