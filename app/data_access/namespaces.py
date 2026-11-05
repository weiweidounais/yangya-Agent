"""Collision-free namespaces derived only from the authenticated identity."""
import json
from pathlib import Path
from app.data_access import current_user, IdentityRequired, NotFound


def user_key(*parts):
    uid = current_user()
    if uid is None:
        raise IdentityRequired("User namespace requires a user scope")
    return json.dumps([uid, *parts], ensure_ascii=True, separators=(",", ":"))


def user_directory(root):
    uid = current_user()
    if uid is None:
        raise IdentityRequired("User path requires a user scope")
    # Hex is reversible and cannot contain separators, dots or traversal syntax.
    return Path(root) / uid.encode("utf-8").hex()


def user_file(root, name):
    if Path(name).name != name or name in ("", ".", ".."):
        raise NotFound()
    directory = user_directory(root)
    path = directory / name
    if path.resolve().parent != directory.resolve() or directory.is_symlink():
        raise NotFound()
    return path
