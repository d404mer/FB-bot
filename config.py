"""Load configuration: один каталог проекта — `config.ini` и (опционально) `.env`.

Порядок значений для каждого параметра:
1. Непустая переменная окружения процесса (systemd, `export`, панель хостинга).
2. Ключ из `.env` в том же каталоге, что и `config.ini` (подгружается автоматически).
3. Ключ из `config.ini`, если файл есть.

Пустая строка в окружении не считается заданным значением — тогда используются `.env` и INI.
"""
import os
import sys
from configparser import ConfigParser

# Default config path (absolute so it works from any cwd)
CONFIG_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "config.ini"))


def _env(key: str) -> str | None:
    """Значение переменной окружения; None если ключ отсутствует или только пробелы."""
    raw = os.environ.get(key)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped if stripped else None


def load_config(path: str | None = None) -> dict:
    """Load config: `.env` рядом с `config.ini`, затем INI и слияние с окружением."""
    path = os.path.abspath(path or CONFIG_PATH)
    ini_dir = os.path.dirname(path)

    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(ini_dir, ".env"), override=False)
    except ImportError:
        pass

    parser = ConfigParser()
    has_ini = os.path.isfile(path)
    if has_ini:
        parser.read(path, encoding="utf-8")

    def get(section: str, key: str, fallback: str | None = None) -> str | None:
        try:
            return parser.get(section, key, fallback=fallback)
        except Exception:
            return fallback

    check_interval_override = _env("CHECK_INTERVAL")

    bot_token = (_env("BOT_TOKEN") or (get("TELEGRAM", "BOT_TOKEN") if has_ini else None) or "").strip()
    telegram_proxy_raw = (
        _env("TELEGRAM_PROXY_URL")
        or (get("TELEGRAM", "PROXY_URL") if has_ini else None)
        or ""
    ).strip()
    telegram_proxy_url = telegram_proxy_raw or None
    username = (
        _env("AO3_USERNAME")
        or (get("AO3", "USERNAME") if has_ini else None)
        or (get("AO3", "AO3_USERNAME") if has_ini else None)
        or ""
    ).strip()
    password = (_env("AO3_PASSWORD") or (get("AO3", "PASSWORD") if has_ini else None) or "").strip() or None
    check_interval_raw = (
        check_interval_override or (get("APP", "CHECK_INTERVAL", "180") if has_ini else "180") or "180"
    ).strip()
    request_delay_raw = (_env("REQUEST_DELAY") or (get("AO3", "REQUEST_DELAY", "4") if has_ini else "15") or "15").strip()
    state_file = (
        _env("STATE_FILE") or (get("APP", "STATE_FILE", "bot_state.json") if has_ini else "bot_state.json") or "bot_state.json"
    ).strip()
    log_file_raw = _env("LOG_FILE") or (get("APP", "LOG_FILE", "bot.log") if has_ini else None)
    log_file = log_file_raw.strip() if log_file_raw else None

    admin_username_raw = (
        _env("ADMIN_TELEGRAM_USERNAME")
        or (get("ADMIN", "TELEGRAM_USERNAME") if has_ini else None)
        or ""
    ).strip()
    admin_username = admin_username_raw.lstrip("@").strip() or None

    admin_user_id_raw = (_env("ADMIN_TELEGRAM_USER_ID") or (get("ADMIN", "TELEGRAM_USER_ID") if has_ini else None) or "").strip()
    admin_user_id: int | None = None
    if admin_user_id_raw:
        try:
            admin_user_id = int(admin_user_id_raw)
        except ValueError:
            print("ADMIN_TELEGRAM_USER_ID must be an integer.")
            sys.exit(1)

    admin_status_raw = (
        _env("ADMIN_STATUS_INTERVAL")
        or (get("ADMIN", "STATUS_INTERVAL", "3600") if has_ini else "3600")
        or "3600"
    ).strip()

    try:
        check_interval = int(check_interval_raw) if check_interval_raw else 180
        request_delay = int(request_delay_raw) if request_delay_raw else 4
        admin_status_interval = int(admin_status_raw) if admin_status_raw else 3600
    except ValueError:
        print("CHECK_INTERVAL, REQUEST_DELAY and ADMIN_STATUS_INTERVAL must be integers.")
        sys.exit(1)

    if not bot_token or not username:
        missing = []
        if not bot_token:
            missing.append("BOT_TOKEN")
        if not username:
            missing.append("AO3_USERNAME")
        print("BOT_TOKEN and AO3 USERNAME are required. Задайте в config.ini или в .env в каталоге проекта.")
        print("  Missing or empty:", ", ".join(missing))
        print("  Есть config.ini:", "да" if has_ini else "нет (только окружение и .env)")
        if not has_ini:
            print(f"  Config file not found: {path}")
            print("  Railway: add variables in Service → Variables (or ensure Shared Variables are linked to this service), then redeploy.")
        sys.exit(1)

    if not password:
        print("AO3 PASSWORD is required (бот работает только через Inbox).")
        print("  Missing or empty: AO3_PASSWORD. Источник: config.ini и/или .env в каталоге проекта.")
        if not has_ini:
            print(f"  Config file not found: {path}")
            print("  Railway: add AO3_PASSWORD in Service → Variables, then redeploy.")
        sys.exit(1)

    return {
        "BOT_TOKEN": bot_token,
        "AO3_USERNAME": username.strip(),
        "AO3_PASSWORD": password.strip(),
        "CHECK_INTERVAL": check_interval,
        "REQUEST_DELAY": request_delay,
        "STATE_FILE": state_file or "bot_state.json",
        "LOG_FILE": log_file or None,
        "ADMIN_TELEGRAM_USERNAME": admin_username,
        "ADMIN_TELEGRAM_USER_ID": admin_user_id,
        "ADMIN_STATUS_INTERVAL": max(0, admin_status_interval),
        "TELEGRAM_PROXY_URL": telegram_proxy_url,
        "_config_path": path if has_ini else "env",
        "_check_interval_source": "переменная окружения или .env" if check_interval_override else ("config.ini" if has_ini else "по умолчанию"),
    }
