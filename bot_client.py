"""Telegram: уведомления, управление топиками, глобальный админ (ЛС), автоочистка при кике."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import telebot
from telebot import apihelper as tele_apihelper
from telebot.apihelper import ApiTelegramException

import host_status
from utils import escape_html

logger = logging.getLogger(__name__)


@dataclass
class BotRuntimeContext:
    """Контекст для хендлеров (без модульных глобалей для state_file/state_manager)."""

    state_file: str
    state_manager: Any
    ao3_username: str
    admin_username: str | None
    admin_user_id: int | None
    admin_status_interval_sec: int
    admin_chat_id: int | None = None
    telegram_proxy_url: str | None = None


def _proxy_log_label(url: str) -> str:
    """Схема и хост для лога без логина/пароля."""
    try:
        from urllib.parse import urlparse

        u = urlparse(url)
        host = u.hostname or "?"
        port = u.port
        if port:
            return f"{u.scheme}://{host}:{port}"
        return f"{u.scheme}://{host}"
    except Exception:
        return "прокси"


def _apply_telegram_proxy(proxy_url: str | None) -> None:
    """Только запросы pyTelegramBotAPI к api.telegram.org; AO3 не затрагивается."""
    if proxy_url:
        tele_apihelper.proxy = {"http": proxy_url, "https": proxy_url}
        logger.info("[Telegram] Bot API через прокси: %s", _proxy_log_label(proxy_url))
    else:
        tele_apihelper.proxy = None


def init_bot(
    token: str,
    ctx: BotRuntimeContext,
) -> telebot.TeleBot:
    _apply_telegram_proxy(ctx.telegram_proxy_url)
    bot = telebot.TeleBot(token)
    bot.wtf_ctx = ctx  # type: ignore[attr-defined]
    return bot


def _get_ctx(bot: telebot.TeleBot) -> BotRuntimeContext:
    ctx = getattr(bot, "wtf_ctx", None)
    if ctx is None:
        raise RuntimeError("Bot context missing. Use init_bot(..., ctx).")
    return ctx


def _is_chat_admin(bot: telebot.TeleBot, chat_id: int, user_id: int) -> bool:
    try:
        admins = bot.get_chat_administrators(chat_id)
        return any(getattr(m, "user", None) and getattr(m.user, "id", None) == user_id for m in admins)
    except Exception:
        return False


def _is_global_admin(ctx: BotRuntimeContext, message: telebot.types.Message) -> bool:
    if message.chat.type != "private":
        return False
    uid = message.from_user.id if message.from_user else None
    if ctx.admin_user_id is not None and uid == ctx.admin_user_id:
        return True
    un = (message.from_user.username or "").strip().lower() if message.from_user else ""
    if ctx.admin_username and un == ctx.admin_username.lower():
        return True
    return False


def _resolve_admin_chat(bot: telebot.TeleBot, ctx: BotRuntimeContext) -> None:
    if ctx.admin_user_id is not None:
        ctx.admin_chat_id = ctx.admin_user_id
        logger.info("[Admin] ЛС-отчёты: chat_id из ADMIN_TELEGRAM_USER_ID=%s", ctx.admin_chat_id)
        return
    if ctx.admin_username:
        try:
            ch = bot.get_chat(f"@{ctx.admin_username}")
            ctx.admin_chat_id = ch.id
            logger.info("[Admin] ЛС-отчёты: разрешён @%s → chat_id=%s", ctx.admin_username, ctx.admin_chat_id)
        except Exception as e:
            logger.warning(
                "[Admin] Не удалось get_chat @%s: %s — укажите числовой TELEGRAM_USER_ID в [ADMIN] config.ini "
                "(или ADMIN_TELEGRAM_USER_ID в .env), если resolve по username недоступен.",
                ctx.admin_username,
                e,
            )
    else:
        logger.info("[Admin] ADMIN_TELEGRAM_USERNAME / ADMIN_TELEGRAM_USER_ID не заданы — ЛС-отчёты отключены")


def _subscribe_current_topic(bot: telebot.TeleBot, message: telebot.types.Message, ctx: BotRuntimeContext) -> None:
    chat_id = message.chat.id
    thread_id = getattr(message, "message_thread_id", None) or 0
    if message.chat.type not in ("supergroup", "group"):
        bot.reply_to(message, "Добавьте бота в группу с топиками и выполните команду там.")
        return
    user_id = message.from_user.id if message.from_user else None
    if user_id is None or not _is_chat_admin(bot, chat_id, user_id):
        bot.reply_to(message, "Только администраторы группы могут подписывать топик.")
        return
    added = ctx.state_manager.update_state(
        ctx.state_file,
        ctx.ao3_username,
        lambda state: ctx.state_manager.add_notification_target(state, chat_id, thread_id),
    )
    if added:
        bot.reply_to(message, "Топик подписан: сюда будут приходить уведомления о новых комментариях AO3.")
    else:
        bot.reply_to(message, "Этот топик уже в списке получателей.")


def _unsubscribe_current_topic(bot: telebot.TeleBot, message: telebot.types.Message, ctx: BotRuntimeContext) -> None:
    chat_id = message.chat.id
    thread_id = getattr(message, "message_thread_id", None) or 0
    if message.chat.type in ("supergroup", "group"):
        user_id = message.from_user.id if message.from_user else None
        if user_id is None or not _is_chat_admin(bot, chat_id, user_id):
            bot.reply_to(message, "Только администраторы группы могут отписывать топик.")
            return
    removed = ctx.state_manager.update_state(
        ctx.state_file,
        ctx.ao3_username,
        lambda state: ctx.state_manager.remove_notification_target(state, chat_id, thread_id),
    )
    if removed:
        bot.reply_to(message, "Топик отписан от уведомлений.")
    else:
        bot.reply_to(message, "Этот топик не был в списке получателей.")


def _unsubscribe_all_in_chat(bot: telebot.TeleBot, message: telebot.types.Message, ctx: BotRuntimeContext) -> None:
    chat_id = message.chat.id
    if message.chat.type not in ("supergroup", "group"):
        bot.reply_to(message, "Команда только в группе.")
        return
    user_id = message.from_user.id if message.from_user else None
    if user_id is None or not _is_chat_admin(bot, chat_id, user_id):
        bot.reply_to(message, "Только администраторы группы могут снять все подписки этого чата.")
        return
    n = ctx.state_manager.update_state(
        ctx.state_file,
        ctx.ao3_username,
        lambda state: ctx.state_manager.remove_all_targets_for_chat(state, chat_id),
    )
    bot.reply_to(message, f"Удалены все подписки для этого чата ({n} записей)." if n else "В этом чате не было подписок.")


COLLAPSIBLE_QUOTE_MIN_LENGTH = 280
TELEGRAM_MESSAGE_LIMIT = 4096
TRUNCATION_SUFFIX = "\n\n… (комментарий обрезан, смотрите полный текст на AO3)"


def _assemble_comment_notification(
    title: str,
    url: str,
    author: str,
    date: str,
    raw_text: str,
    *,
    truncated: bool,
    use_expandable_quote: bool,
) -> str:
    quote_tag = "<blockquote expandable>" if use_expandable_quote else "<blockquote>"
    body = escape_html(raw_text)
    if truncated:
        body += TRUNCATION_SUFFIX
    return (
        "<b>💬 Новый комментарий на AO3</b>\n\n"
        "<b>Работа:</b> <a href=\"" + url + "\">" + title + "</a>\n"
        "<b>Автор:</b> " + author + "\n"
        "<b>Когда:</b> " + date + "\n\n"
        "<b>Текст:</b>\n"
        + quote_tag + body + "</blockquote>"
    )


def _truncate_comment_text_for_telegram(
    title: str,
    url: str,
    author: str,
    date: str,
    raw_text: str,
) -> tuple[str, bool]:
    use_expandable_quote = len(raw_text.strip()) >= COLLAPSIBLE_QUOTE_MIN_LENGTH
    full = _assemble_comment_notification(
        title, url, author, date, raw_text, truncated=False, use_expandable_quote=use_expandable_quote
    )
    if len(full) <= TELEGRAM_MESSAGE_LIMIT:
        return raw_text, False

    lo, hi = 0, len(raw_text)
    best = 0
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = raw_text[:mid].rstrip()
        msg = _assemble_comment_notification(
            title, url, author, date, candidate, truncated=True, use_expandable_quote=use_expandable_quote
        )
        if len(msg) <= TELEGRAM_MESSAGE_LIMIT:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1

    truncated_text = raw_text[:best].rstrip()
    logger.info(
        "[Telegram] Текст комментария обрезан: %s → %s символов (лимит Telegram %s)",
        len(raw_text),
        len(truncated_text),
        TELEGRAM_MESSAGE_LIMIT,
    )
    return truncated_text, True


def _format_notification(comment_data: dict[str, Any]) -> str:
    title = escape_html(comment_data.get("work_title") or "Untitled")
    url = (comment_data.get("work_url") or "").replace("&", "&amp;")
    author = escape_html(comment_data.get("author") or "Anonymous")
    date = escape_html(comment_data.get("date") or "—")
    raw_text = comment_data.get("text") or ""
    notif_type = (comment_data.get("notification_type") or "comment").lower()
    if notif_type == "kudos":
        return (
            "<b>Кудас на AO3</b>\n\n"
            "<b>Работа:</b> <a href=\"" + url + "\">" + title + "</a>\n"
            "<b>Кто:</b> " + author + "\n"
            "<b>Когда:</b> " + date
        )
    fitted_text, truncated = _truncate_comment_text_for_telegram(title, url, author, date, raw_text)
    use_expandable_quote = len(raw_text.strip()) >= COLLAPSIBLE_QUOTE_MIN_LENGTH
    return _assemble_comment_notification(
        title, url, author, date, fitted_text, truncated=truncated, use_expandable_quote=use_expandable_quote
    )


def send_comment_notification(
    chat_id: int,
    message_thread_id: int,
    comment_data: dict[str, Any],
    bot: telebot.TeleBot | None = None,
) -> bool:
    """Отправить уведомление в топик. Если bot=None — берётся последний init_bot (через текущий процесс — передайте bot из main)."""
    tb = bot or getattr(send_comment_notification, "_last_bot", None)  # type: ignore[attr-defined]
    if tb is None:
        raise RuntimeError("TeleBot not passed and no default bot bound.")
    text = _format_notification(comment_data)
    max_retries = 3
    for attempt in range(max_retries):
        try:
            tb.send_message(
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


def bind_notifier_bot(bot: telebot.TeleBot) -> None:
    """Сохранить бота для вызова send_comment_notification без явной передачи (main цикл)."""
    setattr(send_comment_notification, "_last_bot", bot)


def _format_targets_overview(bot: telebot.TeleBot, ctx: BotRuntimeContext) -> str:
    state = ctx.state_manager.load_state(ctx.state_file)
    targets = state.get("notification_targets") or []
    if not isinstance(targets, list) or not targets:
        return "Подписок нет."
    lines = [f"<b>Всего подписок: {len(targets)}</b>", ""]
    for i, t in enumerate(targets, start=1):
        if not isinstance(t, dict):
            continue
        cid = t.get("chat_id")
        tid = t.get("message_thread_id") or 0
        title = "?"
        try:
            ch = bot.get_chat(cid)
            title = escape_html(getattr(ch, "title", None) or str(cid))
        except Exception:
            title = escape_html(str(cid))
        lines.append(f"{i}. <b>{title}</b>")
        lines.append(f"   chat_id <code>{cid}</code>, thread <code>{tid}</code>")
        lines.append("")
    return "\n".join(lines).strip()


def _send_vps_status(bot: telebot.TeleBot, ctx: BotRuntimeContext) -> None:
    if ctx.admin_chat_id is None:
        return
    metrics = host_status.collect_host_metrics()
    state = ctx.state_manager.load_state(ctx.state_file)
    n = len(ctx.state_manager.get_notification_targets(state))
    text = host_status.format_status_html(metrics, ao3_user=ctx.ao3_username, targets_count=n)
    try:
        bot.send_message(ctx.admin_chat_id, text, parse_mode="HTML")
    except Exception as e:
        logger.warning("[Admin] Не удалось отправить статус VPS: %s", e)


def _admin_status_loop(bot: telebot.TeleBot, ctx: BotRuntimeContext) -> None:
    interval = ctx.admin_status_interval_sec
    if interval <= 0 or ctx.admin_chat_id is None:
        return
    logger.info("[Admin] Периодический отчёт VPS каждые %s с", interval)
    while True:
        time.sleep(interval)
        try:
            _send_vps_status(bot, ctx)
        except Exception:
            logger.exception("[Admin] Ошибка в потоке отчёта VPS")


def register_handlers(bot: telebot.TeleBot) -> None:
    ctx = _get_ctx(bot)

    @bot.message_handler(commands=["notify_here", "set_topic", "start"])
    def on_subscribe(message: telebot.types.Message) -> None:
        if message.chat.type == "private":
            if _is_global_admin(ctx, message):
                bot.reply_to(
                    message,
                    "Команды глобального админа в ЛС:\n"
                    "/status — снимок VPS и число подписок\n"
                    "/topics — все топики для уведомлений\n\n"
                    "В группе (админ чата):\n"
                    "/notify_here — подписать текущий топик\n"
                    "/notify_off — отписать топик\n"
                    "/notify_stop_all — убрать все подписки этого чата\n"
                    "Короткие алиасы: /set_topic, /unset_topic",
                )
            else:
                bot.reply_to(message, "Добавьте бота в группу с топиками и выполните там /notify_here или /set_topic.")
            return
        _subscribe_current_topic(bot, message, ctx)

    @bot.message_handler(commands=["notify_off", "unset_topic"])
    def on_unsubscribe(message: telebot.types.Message) -> None:
        if message.chat.type == "private":
            bot.reply_to(message, "Отписка выполняется в нужном топике группы: /notify_off или /unset_topic.")
            return
        _unsubscribe_current_topic(bot, message, ctx)

    @bot.message_handler(commands=["notify_stop_all"])
    def on_stop_all(message: telebot.types.Message) -> None:
        if message.chat.type == "private":
            bot.reply_to(message, "Команда только в группе: снимает все подписки этого чата.")
            return
        _unsubscribe_all_in_chat(bot, message, ctx)

    @bot.message_handler(commands=["topics"])
    def on_topics(message: telebot.types.Message) -> None:
        if not _is_global_admin(ctx, message):
            bot.reply_to(message, "Команда только для глобального админа в личке с ботом.")
            return
        text = _format_targets_overview(bot, ctx)
        try:
            bot.send_message(message.chat.id, text, parse_mode="HTML")
        except Exception:
            bot.reply_to(message, text, parse_mode="HTML")

    @bot.message_handler(commands=["status"])
    def on_status(message: telebot.types.Message) -> None:
        if not _is_global_admin(ctx, message):
            bot.reply_to(message, "Команда только для глобального админа в личке с ботом.")
            return
        _send_vps_status(bot, ctx)

    @bot.my_chat_member_handler(func=lambda _: True)
    def on_my_chat_member(update: telebot.types.ChatMemberUpdated) -> None:
        try:
            me = bot.get_me()
            new_cm = update.new_chat_member
            if new_cm.user.id != me.id:
                return
            if new_cm.status not in ("left", "kicked"):
                return
            chat_id = update.chat.id
            n = ctx.state_manager.update_state(
                ctx.state_file,
                ctx.ao3_username,
                lambda state: ctx.state_manager.remove_all_targets_for_chat(state, chat_id),
            )
            if n:
                logger.info("[Telegram] Бот удалён из чата %s — снято подписок: %s", chat_id, n)
            if ctx.admin_chat_id:
                title = getattr(update.chat, "title", None) or str(chat_id)
                try:
                    bot.send_message(
                        ctx.admin_chat_id,
                        f"Бот исключён из чата <b>{escape_html(title)}</b> (<code>{chat_id}</code>). "
                        f"Удалены подписки: <b>{n}</b>.",
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.warning("[Admin] Не удалось уведить админа о кике: %s", e)
        except Exception:
            logger.exception("Ошибка обработки my_chat_member")


def start_polling(bot: telebot.TeleBot) -> None:
    ctx = _get_ctx(bot)
    bind_notifier_bot(bot)
    _resolve_admin_chat(bot, ctx)
    register_handlers(bot)

    if ctx.admin_status_interval_sec > 0 and ctx.admin_chat_id is not None:
        t = threading.Thread(target=_admin_status_loop, args=(bot, ctx), daemon=True)
        t.start()
    elif ctx.admin_status_interval_sec > 0:
        logger.warning(
            "[Admin] ADMIN_STATUS_INTERVAL>0, но chat_id админа не определён — таймер отчётов отключён. "
            "Задайте TELEGRAM_USER_ID в [ADMIN] (config.ini) или ADMIN_TELEGRAM_USER_ID в .env; при необходимости TELEGRAM_USERNAME."
        )

    bot.infinity_polling(allowed_updates=["message", "my_chat_member"])
