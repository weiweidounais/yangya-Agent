"""Optional owner-email hook; no email transport is installed in this service."""


def channels(value):
    """Keep legacy delivery strings while accepting explicit channel lists."""
    if isinstance(value, str):
        return {'in_app', 'channel'} if value == 'both' else set(value.split(','))
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        return set(value)
    return set()


def email_to_owner(uid, title, body):
    # A future configured transport must resolve the owner's verified address
    # server-side and return ok=True only after the send succeeds.
    return {'ok': False, 'skipped': True, 'reason': 'email_unconfigured'}
