"""Telegram bot: /set_topic handler and send_comment_notification. Uses pyTelegramBotAPI (telebot)."""
import logging
import time
from typing import Any

import telebot
from telebot.apihelper import ApiTelegramException

from utils import escape_html

logger = logging.getLogger(__name__)

# Lazy singleton
_bot: telebot.TeleBot | None = None
_state_file: str = ""
_state_manager: Any = None


def init_bot(token: str, state_file: str, state_manager_module: Any) -> telebot.TeleBot:
    """Create bot instance and store state_file + state_manager for handlers."""
    global _bot, _state_file, _state_manager
    _bot = telebot.TeleBot(token)
    _state_file = state_file
    _state_manager = state_manager_module
    return _bot


def _get_bot() -> telebot.TeleBot:
    if _bot is None:
        raise RuntimeError("Bot not initialized. Call init_bot first.")
    return _bot


def _is_chat_admin(bot: telebot.TeleBot, chat_id: int, user_id: int) -> bool:
    """Проверить, является ли пользователь администратором чата (только для групп/супергрупп)."""
    try:
        admins = bot.get_chat_administrators(chat_id)
        return any(getattr(m, "user", None) and getattr(m.user, "id", None) == user_id for m in admins)
    except Exception:
        return False


# Порог длины комментария (символов): выше — сворачиваемая цитата (Collapsible Quote), ниже — обычная цитата
COLLAPSIBLE_QUOTE_MIN_LENGTH = 280


def _format_notification(comment_data: dict[str, Any]) -> str:
    """Build HTML message; длинные комментарии — в <blockquote expandable> (раскрываемая цитата). Поддержка type: comment / kudos (Inbox)."""
    title = escape_html(comment_data.get("work_title") or "Untitled")
    url = (comment_data.get("work_url") or "").replace("&", "&amp;")
    author = escape_html(comment_data.get("author") or "Anonymous")
    date = escape_html(comment_data.get("date") or "—")
    raw_text = comment_data.get("text") or ""
    text_escaped = escape_html(raw_text)
    notif_type = (comment_data.get("notification_type") or "comment").lower()
    if notif_type == "kudos":
        return (
            "<b>Кудас на AO3</b>\n\n"
            "<b>Работа:</b> <a href=\"" + url + "\">" + title + "</a>\n"
            "<b>Кто:</b> " + author + "\n"
            "<b>Когда:</b> " + date
        )
    # Длинные комментарии — раскрываемая цитата (Collapsible Quote), короткие — обычная цитата
    if len(raw_text.strip()) >= COLLAPSIBLE_QUOTE_MIN_LENGTH:
        quote_tag = "<blockquote expandable>"
    else:
        quote_tag = "<blockquote>"
    return (
        "<b>💬 Новый комментарий на AO3</b>\n\n"
        "<b>Работа:</b> <a href=\"" + url + "\">" + title + "</a>\n"
        "<b>Автор:</b> " + author + "\n"
        "<b>Когда:</b> " + date + "\n\n"
        "<b>Текст:</b>\n"
        + quote_tag + text_escaped + "</blockquote>"
    )


def send_comment_notification(
    chat_id: int,
    message_thread_id: int,
    comment_data: dict[str, Any],
) -> bool:
    """Send one comment notification to the given topic. Handles FloodWait. Returns True on success."""
    bot = _get_bot()
    text = _format_notification(comment_data)
    max_retries = 3
    for attempt in range(max_retries):
        try:
            bot.send_message(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                text=text,
                parse_mode="HTML",
            )
            logger.info("[Telegram] Уведомление отправлено: работа %s", comment_data.get("work_id"))
            return True
        except ApiTelegramException as e:
            if "retry after" in str(e).lower() or "flood" in str(e).lower():
                retry_after = getattr(e, "retry_after", 30) or 30
                logger.warning("Telegram FloodWait, sleeping %s s", retry_after)
                time.sleep(retry_after)
                continue
            logger.exception("Telegram API error sending notification: %s", e)
            return False
        except Exception as e:
            logger.exception("Error sending notification: %s", e)
            return False
    return False


def register_handlers(bot: telebot.TeleBot) -> None:
    """Register /set_topic, /unset_topic and /start handlers."""

    @bot.message_handler(commands=["set_topic", "start"])
    def on_set_topic(message: telebot.types.Message) -> None:
        chat_id = message.chat.id
        thread_id = getattr(message, "message_thread_id", None)
        if message.chat.type not in ("supergroup", "group"):
            bot.reply_to(message, "Добавьте бота в группу (или группу с топиками) и выполните команду там.")
            return
        user_id = message.from_user.id if message.from_user else None
        if user_id is None or not _is_chat_admin(bot, chat_id, user_id):
            bot.reply_to(message, "Только администраторы группы могут использовать эту команду.")
            return
        state = _state_manager.load_state(_state_file)
        added = _state_manager.add_notification_target(state, chat_id, thread_id or 0)
        _state_manager.save_state(state, _state_file)
        if added:
            bot.reply_to(message, "Топик добавлен. Уведомления о новых комментариях AO3 будут приходить сюда.")
        else:
            bot.reply_to(message, "Этот топик уже в списке получателей уведомлений.")

    @bot.message_handler(commands=["unset_topic"])
    def on_unset_topic(message: telebot.types.Message) -> None:
        chat_id = message.chat.id
        thread_id = getattr(message, "message_thread_id", None)
        if message.chat.type in ("supergroup", "group"):
            user_id = message.from_user.id if message.from_user else None
            if user_id is None or not _is_chat_admin(bot, chat_id, user_id):
                bot.reply_to(message, "Только администраторы группы могут использовать эту команду.")
                return
        state = _state_manager.load_state(_state_file)
        removed = _state_manager.remove_notification_target(state, chat_id, thread_id or 0)
        _state_manager.save_state(state, _state_file)
        if removed:
            bot.reply_to(message, "Топик удалён из списка уведомлений.")
        else:
            bot.reply_to(message, "Этот топик не был в списке получателей.")


def start_polling(bot: telebot.TeleBot) -> None:
    """Run long polling in current thread (to be run in a daemon thread)."""
    register_handlers(bot)
    bot.infinity_polling(allowed_updates=["message"])
