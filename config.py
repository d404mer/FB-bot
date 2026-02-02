"""Load configuration from config.ini and/or environment variables.

Important:
- `config.ini` is typically gitignored (contains secrets) and may be absent in deployments.
- Environment variables override values from INI and can be used without any config file.
"""
import os
import sys
from configparser import ConfigParser

# Default config path (absolute so it works from any cwd)
CONFIG_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "config.ini"))


def load_config(path: str | None = None) -> dict:
    """Load config from INI file (if present) and environment variables.

    Env vars override INI. If config.ini is missing, works from env only.
    """
    path = path or CONFIG_PATH
    parser = ConfigParser()
    has_ini = os.path.isfile(path)
    if has_ini:
        parser.read(path, encoding="utf-8")

    def get(section: str, key: str, fallback: str | None = None) -> str | None:
        try:
            return parser.get(section, key, fallback=fallback)
        except Exception:
            return fallback

    # Environment overrides (common names); переменные окружения имеют приоритет над config.ini
    bot_token = (os.environ.get("BOT_TOKEN") or (get("TELEGRAM", "BOT_TOKEN") if has_ini else None) or "").strip()
    username = (
        os.environ.get("AO3_USERNAME")
        or (get("AO3", "USERNAME") if has_ini else None)
        or (get("AO3", "AO3_USERNAME") if has_ini else None)
        or ""
    ).strip()
    # Optional (used in Inbox mode); keep for forward-compatibility
    password = (os.environ.get("AO3_PASSWORD") or (get("AO3", "PASSWORD") if has_ini else None) or "").strip() or None
    check_interval_raw = (os.environ.get("CHECK_INTERVAL") or (get("APP", "CHECK_INTERVAL", "180") if has_ini else "180") or "180").strip()
    request_delay_raw = (os.environ.get("REQUEST_DELAY") or (get("AO3", "REQUEST_DELAY", "4") if has_ini else "4") or "4").strip()
    state_file = (os.environ.get("STATE_FILE") or (get("APP", "STATE_FILE", "bot_state.json") if has_ini else "bot_state.json") or "bot_state.json").strip()
    log_file_raw = os.environ.get("LOG_FILE") or (get("APP", "LOG_FILE", "bot.log") if has_ini else None)
    log_file = log_file_raw.strip() if log_file_raw else None

    try:
        check_interval = int(check_interval_raw) if check_interval_raw else 180
        request_delay = int(request_delay_raw) if request_delay_raw else 4
    except ValueError:
        print("CHECK_INTERVAL and REQUEST_DELAY must be integers.")
        sys.exit(1)

    if not bot_token or not username:
        print("BOT_TOKEN and AO3 USERNAME are required. Set in config.ini or environment.")
        if has_ini:
            print("  config.ini: [TELEGRAM] BOT_TOKEN = ... and [AO3] USERNAME = ...")
        else:
            print(f"  Config file not found: {path}")
            print("  Tip: you can run without config.ini by setting env vars: BOT_TOKEN, AO3_USERNAME (and optionally AO3_PASSWORD).")
        sys.exit(1)

    return {
        "BOT_TOKEN": bot_token,
        "AO3_USERNAME": username.strip(),
        "AO3_PASSWORD": password.strip() if password else None,
        "CHECK_INTERVAL": check_interval,
        "REQUEST_DELAY": request_delay,
        "STATE_FILE": state_file or "bot_state.json",
        "LOG_FILE": log_file or None,
        "_config_path": path if has_ini else "env",
    }
