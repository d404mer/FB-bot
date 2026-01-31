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
    # Показываем путь к конфигу и откуда берётся интервал (CHECK_INTERVAL в окружении перекрывает config.ini)
    import os as _os
    interval_src = "переменная окружения" if _os.environ.get("CHECK_INTERVAL") else "config.ini"
    logger.info("Конфиг: %s | CHECK_INTERVAL=%s с, REQUEST_DELAY=%s с (интервал из %s)", cfg.get("_config_path", "?"), cfg["CHECK_INTERVAL"], cfg["REQUEST_DELAY"], interval_src)

    state_file = cfg["STATE_FILE"]
    # Ensure state exists with defaults
    state = state_manager.load_state(state_file, cfg["AO3_USERNAME"])
    state["tracked_user"] = cfg["AO3_USERNAME"]
    state_manager.save_state(state, state_file)

    bot = bot_client.init_bot(cfg["BOT_TOKEN"], state_file, state_manager)
    poll_thread = threading.Thread(target=bot_client.start_polling, args=(bot,), daemon=True)
    poll_thread.start()

    while True:
        try:
            logger.info("[Цикл] Начало проверки")
            state = state_manager.load_state(state_file, cfg["AO3_USERNAME"])
            state["tracked_user"] = cfg["AO3_USERNAME"]
            comments = ao3_parser.get_all_comments_for_user(cfg["AO3_USERNAME"], cfg["REQUEST_DELAY"])
            logger.info("[Цикл] Загружено комментариев: %s", len(comments))
            # Перезагружаем состояние после долгой проверки — топик мог быть задан через /set_topic пока мы парсили
            state = state_manager.load_state(state_file, cfg["AO3_USERNAME"])
            state["tracked_user"] = cfg["AO3_USERNAME"]
            target = state_manager.get_notification_target(state)
            if target:
                logger.info("[Цикл] Топик задан: chat_id=%s, thread_id=%s", target[0], target[1])
            else:
                logger.warning("[Цикл] Топик не задан")
            new_count = 0
            # Первый запуск: запомнить все текущие комментарии без отправки (не реагировать на старые)
            if not state.get("initial_seed_done"):
                for c in comments:
                    ch = utils.comment_hash(c["author"], c["date"], c["text"])
                    state_manager.add_known_comment(state, c["work_id"], ch)
                state["initial_seed_done"] = True
                state["last_check_timestamp"] = datetime.now(tz=timezone.utc).isoformat()
                state_manager.save_state(state, state_file)
                logger.info("[Цикл] Первый запуск: запомнены все текущие комментарии (%s), уведомления не отправлялись", len(comments))
            else:
                for c in comments:
                    ch = utils.comment_hash(c["author"], c["date"], c["text"])
                    if state_manager.is_comment_known(state, c["work_id"], ch):
                        continue
                    new_count += 1
                    if target is None:
                        if new_count == 1:
                            logger.warning("Топик не задан, пропуск отправки уведомлений. Выполните /set_topic в нужном топике.")
                        continue
                    logger.info("[Цикл] Новый комментарий: работа %s, автор %s", c["work_id"], c["author"])
                    ok = bot_client.send_comment_notification(target[0], target[1], c)
                    if ok:
                        state_manager.add_known_comment(state, c["work_id"], ch)
                        logger.info("[Цикл] Уведомление отправлено в Telegram")
                    else:
                        logger.warning("[Цикл] Не удалось отправить уведомление")
                    time.sleep(NOTIFICATION_DELAY_SECONDS)
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
