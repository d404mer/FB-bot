"""State storage: bot_state.json — known_comments and notification_targets (список чатов/топиков)."""
import json
import os
from datetime import datetime, timezone
from typing import Any


def _default_state(tracked_user: str = "") -> dict[str, Any]:
    return {
        "tracked_user": tracked_user,
        "last_check_timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "known_comments": {},
        "notification_targets": [],
        "initial_seed_done": False,
    }


def load_state(state_file: str, tracked_user: str = "") -> dict[str, Any]:
    """Load state from JSON; if file missing, return default. Поддерживает notification_targets (список) и миграцию со старого notification_target."""
    if not os.path.isfile(state_file):
        return _default_state(tracked_user)
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "known_comments" not in data:
            data["known_comments"] = {}
        if "notification_targets" not in data:
            # Миграция: один старый notification_target → список из одного элемента
            old = data.get("notification_target")
            if old and isinstance(old, dict) and old.get("chat_id") is not None and old.get("message_thread_id") is not None:
                data["notification_targets"] = [{"chat_id": int(old["chat_id"]), "message_thread_id": int(old["message_thread_id"])}]
            else:
                data["notification_targets"] = []
        if not isinstance(data["notification_targets"], list):
            data["notification_targets"] = []
        if "tracked_user" not in data and tracked_user:
            data["tracked_user"] = tracked_user
        if "initial_seed_done" not in data:
            data["initial_seed_done"] = bool(data.get("known_comments"))
        return data
    except (json.JSONDecodeError, OSError):
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


def get_notification_targets(state: dict[str, Any]) -> list[tuple[int, int]]:
    """Return list of (chat_id, message_thread_id) для всех подписанных чатов/топиков."""
    targets = state.get("notification_targets") or []
    if not isinstance(targets, list):
        return []
    result = []
    for t in targets:
        if not t or not isinstance(t, dict):
            continue
        cid, tid = t.get("chat_id"), t.get("message_thread_id")
        if cid is None:
            continue
        result.append((int(cid), int(tid) if tid is not None else 0))
    return result


def add_notification_target(state: dict[str, Any], chat_id: int, message_thread_id: int) -> bool:
    """Добавить чат/топик в список получателей. Возвращает True если добавлен, False если уже был. Caller must save_state."""
    if "notification_targets" not in state:
        state["notification_targets"] = []
    tid = message_thread_id if message_thread_id is not None else 0
    key = (int(chat_id), int(tid))
    for t in state["notification_targets"]:
        if isinstance(t, dict) and t.get("chat_id") == key[0] and (t.get("message_thread_id") or 0) == key[1]:
            return False
    state["notification_targets"].append({"chat_id": key[0], "message_thread_id": key[1]})
    return True


def remove_notification_target(state: dict[str, Any], chat_id: int, message_thread_id: int) -> bool:
    """Удалить чат/топик из списка получателей. Возвращает True если удалён. Caller must save_state."""
    targets = state.get("notification_targets") or []
    if not isinstance(targets, list):
        return False
    tid = message_thread_id if message_thread_id is not None else 0
    key = (int(chat_id), int(tid))
    new_list = [t for t in targets if not (isinstance(t, dict) and t.get("chat_id") == key[0] and (t.get("message_thread_id") or 0) == key[1])]
    removed = len(new_list) < len(targets)
    state["notification_targets"] = new_list
    return removed


def remove_all_targets_for_chat(state: dict[str, Any], chat_id: int) -> int:
    """Удалить все подписки для данного chat_id (все топики супергруппы). Возвращает число удалённых записей."""
    targets = state.get("notification_targets") or []
    if not isinstance(targets, list):
        return 0
    cid = int(chat_id)
    new_list = [t for t in targets if not (isinstance(t, dict) and int(t.get("chat_id") or 0) == cid)]
    removed = len(targets) - len(new_list)
    state["notification_targets"] = new_list
    return removed
