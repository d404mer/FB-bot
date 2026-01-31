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


def _format_notification(comment_data: dict[str, Any]) -> str:
    """Build HTML message; текст комментария — в <blockquote> (блок цитаты в Telegram)."""
    title = escape_html(comment_data.get("work_title") or "Untitled")
    url = (comment_data.get("work_url") or "").replace("&", "&amp;")
    author = escape_html(comment_data.get("author") or "Anonymous")
    date = escape_html(comment_data.get("date") or "—")
    raw_text = comment_data.get("text") or ""
    text_escaped = escape_html(raw_text)
    return (
        "<b>Новый комментарий на AO3</b>\n\n"
        "<b>Работа:</b> <a href=\"" + url + "\">" + title + "</a>\n"
        "<b>Автор:</b> " + author + "\n"
        "<b>Когда:</b> " + date + "\n\n"
        "<b>Текст:</b>\n"
        "<blockquote>" + text_escaped + "</blockquote>"
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
    """Register /set_topic and /start handlers."""

    @bot.message_handler(commands=["set_topic", "start"])
    def on_set_topic(message: telebot.types.Message) -> None:
        chat_id = message.chat.id
        # In supergroup with topics, message_thread_id is set for the topic
        thread_id = getattr(message, "message_thread_id", None)
        if message.chat.type in ("supergroup", "group") and thread_id is not None:
            state = _state_manager.load_state(_state_file)
            _state_manager.set_notification_target(state, chat_id, thread_id)
            _state_manager.save_state(state, _state_file)
            bot.reply_to(message, "Топик для уведомлений установлен. Новые комментарии AO3 будут приходить сюда.")
        else:
            bot.reply_to(
                message,
                "Добавьте бота в группу с топиками и выполните команду в нужном топике.",
            )


def start_polling(bot: telebot.TeleBot) -> None:
    """Run long polling in current thread (to be run in a daemon thread)."""
    register_handlers(bot)
    bot.infinity_polling(allowed_updates=["message"])
