"""Entry point: config, two threads (bot polling + check loop), state and notifications."""
import logging
import sys
import threading
import time
from datetime import datetime, timezone

import ao3_parser
import bot_client
import config
import state_manager
import utils

logger = logging.getLogger(__name__)

ERROR_PAUSE_SECONDS = 60
NOTIFICATION_DELAY_SECONDS = 0.8


def main() -> None:
    cfg = config.load_config()
    utils.setup_logging(cfg.get("LOG_FILE"))
    logger.info(
        "Конфиг: %s | CHECK_INTERVAL=%s с, REQUEST_DELAY=%s с (интервал из %s)",
        cfg.get("_config_path", "?"),
        cfg["CHECK_INTERVAL"],
        cfg["REQUEST_DELAY"],
        cfg.get("_check_interval_source", "?"),
    )

    state_file = cfg["STATE_FILE"]
    # Ensure state exists with defaults
    state = state_manager.load_state(state_file, cfg["AO3_USERNAME"])
    state["tracked_user"] = cfg["AO3_USERNAME"]
    state_manager.save_state(state, state_file)

    bot_ctx = bot_client.BotRuntimeContext(
        state_file=state_file,
        state_manager=state_manager,
        ao3_username=cfg["AO3_USERNAME"],
        admin_username=cfg.get("ADMIN_TELEGRAM_USERNAME"),
        admin_user_id=cfg.get("ADMIN_TELEGRAM_USER_ID"),
        admin_status_interval_sec=int(cfg.get("ADMIN_STATUS_INTERVAL", 3600)),
        telegram_proxy_url=cfg.get("TELEGRAM_PROXY_URL"),
    )
    bot = bot_client.init_bot(cfg["BOT_TOKEN"], bot_ctx)
    poll_thread = threading.Thread(target=bot_client.start_polling, args=(bot,), daemon=True)
    poll_thread.start()

    logger.info("[Цикл] Режим: только Inbox, только комментарии")

    ao3_session = ao3_parser.create_ao3_session(cfg["AO3_USERNAME"], cfg["AO3_PASSWORD"])
    if ao3_session is None:
        logger.error("Не удалось войти в AO3. Проверьте USERNAME и PASSWORD в config.ini.")
        sys.exit(1)
    logger.info("[AO3] Вход выполнен один раз на всю сессию; сессия будет использоваться до завершения программы")

    while True:
        try:
            logger.info("[Цикл] Начало проверки")
            state = state_manager.load_state(state_file, cfg["AO3_USERNAME"])
            state["tracked_user"] = cfg["AO3_USERNAME"]
            raw = ao3_parser.get_notifications_from_inbox(
                ao3_session, cfg["AO3_USERNAME"], cfg["REQUEST_DELAY"]
            )
            if raw is None:
                logger.warning("[Цикл] Inbox недоступен (сеть/сессия?), пропуск цикла")
                time.sleep(cfg["CHECK_INTERVAL"])
                continue
            comments = [c for c in raw if c.get("notification_type") == "comment"]
            logger.info("[Цикл] Загружено комментариев из Inbox: %s", len(comments))
            # Перезагружаем состояние после долгой проверки — топик мог быть задан через /set_topic пока мы парсили
            state = state_manager.load_state(state_file, cfg["AO3_USERNAME"])
            state["tracked_user"] = cfg["AO3_USERNAME"]
            targets = state_manager.get_notification_targets(state)
            if targets:
                logger.info("[Цикл] Топиков для уведомлений: %s", len(targets))
            else:
                logger.warning("[Цикл] Нет подписанных топиков. Выполните /set_topic в нужных чатах/топиках.")
            new_count = 0
            # Первый запуск: запомнить все текущие комментарии без отправки — отправляем только те, что появятся после запуска бота
            if not state.get("initial_seed_done"):
                for c in comments:
                    ch = c.get("comment_id") or utils.comment_hash(c["author"], c["date"], c["text"])
                    state_manager.add_known_comment(state, c["work_id"], ch)
                state["initial_seed_done"] = True
                state["last_check_timestamp"] = datetime.now(tz=timezone.utc).isoformat()
                state_manager.save_state(state, state_file)
                logger.info("[Цикл] Первый запуск: запомнены все текущие комментарии (%s). Уведомления только о новых (после запуска).", len(comments))
            else:
                for c in comments:
                    ch = c.get("comment_id") or utils.comment_hash(c["author"], c["date"], c["text"])
                    if state_manager.is_comment_known(state, c["work_id"], ch):
                        continue
                    new_count += 1
                    if not targets:
                        if new_count == 1:
                            logger.warning("Нет подписанных топиков, пропуск отправки. Выполните /set_topic в нужных чатах.")
                        continue
                    logger.info("[Цикл] Новый комментарий: работа %s, автор %s", c["work_id"], c["author"])
                    sent_any = False
                    for chat_id, thread_id in targets:
                        ok = bot_client.send_comment_notification(chat_id, thread_id, c)
                        if ok:
                            sent_any = True
                        time.sleep(NOTIFICATION_DELAY_SECONDS)
                    if sent_any:
                        state_manager.add_known_comment(state, c["work_id"], ch)
                        logger.info("[Цикл] Уведомление отправлено во все подписанные топики")
                    else:
                        logger.warning("[Цикл] Не удалось отправить уведомление ни в один топик")
                state["last_check_timestamp"] = datetime.now(tz=timezone.utc).isoformat()
                state_manager.save_state(state, state_file)
            logger.info("[Цикл] Состояние сохранено, следующий цикл через %s с", cfg["CHECK_INTERVAL"])
            if new_count:
                logger.info("[Цикл] Обработано новых комментариев: %s", new_count)
        except Exception as e:
            logger.exception("Ошибка в цикле проверки: %s", e)
            time.sleep(ERROR_PAUSE_SECONDS)
            continue
        time.sleep(cfg["CHECK_INTERVAL"])


if __name__ == "__main__":
    main()
    sys.exit(0)
