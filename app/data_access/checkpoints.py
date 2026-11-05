"""Ownership enforcement at the LangGraph persistence boundary."""
import threading
from langgraph.checkpoint.sqlite import SqliteSaver
from app.data_access import NotFound, current_user


def _checkpoint_owner(checkpoint):
    values = checkpoint.get("channel_values", {})
    start = values.get("__start__")
    return values.get("user_id") or (start.get("user_id") if isinstance(start, dict) else None)


class UserSqliteSaver(SqliteSaver):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ownership_lock = threading.RLock()

    def _owned(self, config):
        uid = current_user()
        if uid is None:
            return True
        latest = {"configurable": {"thread_id": config["configurable"]["thread_id"]}}
        # Ownership is shared by every namespace in a thread.
        saved = list(super().list(latest, limit=1))
        return not saved or _checkpoint_owner(saved[0].checkpoint) == uid

    def get_tuple(self, config):
        with self._ownership_lock:
            if not self._owned(config):
                return None
            saved = super().get_tuple(config)
            uid = current_user()
            if saved is not None and uid is not None and _checkpoint_owner(saved.checkpoint) != uid:
                return None
            return saved

    def list(self, config, **kwargs):
        uid = current_user()
        with self._ownership_lock:
            if config is not None and not self._owned(config):
                return
            # Apply limit after filtering, so other owners do not affect results.
            limit = kwargs.pop("limit", None)
            count = 0
            for saved in super().list(config, **kwargs):
                if uid is not None and _checkpoint_owner(saved.checkpoint) != uid:
                    continue
                if limit is not None and count >= limit:
                    break
                count += 1
                yield saved

    def put(self, config, checkpoint, metadata, new_versions):
        uid = current_user()
        with self._ownership_lock:
            if not self._owned(config) or (uid is not None and
                    _checkpoint_owner(checkpoint) not in (None, uid)):
                raise NotFound()
            if uid is not None:
                checkpoint = {**checkpoint, "channel_values": {**checkpoint.get("channel_values", {}), "user_id": uid}}
            return super().put(config, checkpoint, metadata, new_versions)

    def put_writes(self, config, writes, task_id, task_path=""):
        uid = current_user()
        writes = list(writes)
        with self._ownership_lock:
            if not self._owned(config) or any(
                key == "user_id" and uid is not None and value != uid for key, value in writes
            ):
                raise NotFound()
            return super().put_writes(config, writes, task_id, task_path)

    def delete_thread(self, thread_id):
        with self._ownership_lock:
            if not self._owned({"configurable": {"thread_id": thread_id}}):
                return
            return super().delete_thread(thread_id)


def open_saver(path):
    import sqlite3
    saver = UserSqliteSaver(sqlite3.connect(str(path), check_same_thread=False))
    saver.setup()
    return saver
