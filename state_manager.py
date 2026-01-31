"""State storage: bot_state.json — known_comments and notification_target."""
import json
import os
from datetime import datetime, timezone
from typing import Any


def _default_state(tracked_user: str = "") -> dict[str, Any]:
    return {
        "tracked_user": tracked_user,
        "last_check_timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "known_comments": {},
        "notification_target": None,
        "initial_seed_done": False,
    }


def load_state(state_file: str, tracked_user: str = "") -> dict[str, Any]:
    """Load state from JSON; if file missing, return default with empty known_comments and notification_target null."""
    if not os.path.isfile(state_file):
        return _default_state(tracked_user)
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "known_comments" not in data:
            data["known_comments"] = {}
        if "notification_target" not in data:
            data["notification_target"] = None
        if "tracked_user" not in data and tracked_user:
            data["tracked_user"] = tracked_user
        # Если уже есть известные комментарии — считаем, что первичное заполнение было
        if "initial_seed_done" not in data:
            data["initial_seed_done"] = bool(data.get("known_comments"))
        return data
    except (json.JSONDecodeError, OSError) as e:
        # Return default on corrupt/missing; caller can re-save
        return _default_state(tracked_user)


def save_state(state: dict[str, Any], state_file: str) -> None:
    """Write state to JSON atomically (temp file + rename)."""
    dirpath = os.path.dirname(os.path.abspath(state_file))
    if dirpath:
        os.makedirs(dirpath, exist_ok=True)
    tmp = state_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, state_file)


def is_comment_known(state: dict[str, Any], work_id: str, comment_hash: str) -> bool:
    """Return True if this comment hash is already in known_comments for work_id."""
    known = state.get("known_comments") or {}
    hashes = known.get(work_id) or []
    return comment_hash in hashes


def add_known_comment(state: dict[str, Any], work_id: str, comment_hash: str) -> None:
    """Add comment hash to known_comments for work_id (in-memory only)."""
    if "known_comments" not in state:
        state["known_comments"] = {}
    if work_id not in state["known_comments"]:
        state["known_comments"][work_id] = []
    if comment_hash not in state["known_comments"][work_id]:
        state["known_comments"][work_id].append(comment_hash)


def get_notification_target(state: dict[str, Any]) -> tuple[int, int] | None:
    """Return (chat_id, message_thread_id) or None if not set."""
    target = state.get("notification_target")
    if not target or not isinstance(target, dict):
        return None
    chat_id = target.get("chat_id")
    thread_id = target.get("message_thread_id")
    if chat_id is None or thread_id is None:
        return None
    return (int(chat_id), int(thread_id))


def set_notification_target(state: dict[str, Any], chat_id: int, message_thread_id: int) -> None:
    """Set notification_target in state (in-memory). Caller must save_state afterward."""
    state["notification_target"] = {"chat_id": chat_id, "message_thread_id": message_thread_id}
