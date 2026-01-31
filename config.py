"""Load configuration from config.ini and environment variables."""
import os
import sys
from configparser import ConfigParser

# Default config path (absolute so it works from any cwd)
CONFIG_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "config.ini"))


def load_config(path: str | None = None) -> dict:
    """Load config from INI file; env vars override sensitive/important keys."""
    path = path or CONFIG_PATH
    parser = ConfigParser()
    if not os.path.isfile(path):
        print(f"Config file not found: {path}. Copy config.ini.example to config.ini and fill in values.")
        sys.exit(1)
    parser.read(path, encoding="utf-8")

    def get(section: str, key: str, fallback: str | None = None) -> str | None:
        try:
            return parser.get(section, key, fallback=fallback)
        except Exception:
            return fallback

    # Environment overrides (common names); переменные окружения имеют приоритет над config.ini
    bot_token = (os.environ.get("BOT_TOKEN") or get("TELEGRAM", "BOT_TOKEN") or "").strip()
    username = (os.environ.get("AO3_USERNAME") or get("AO3", "USERNAME") or get("AO3", "AO3_USERNAME") or "").strip()
    check_interval_raw = (os.environ.get("CHECK_INTERVAL") or get("APP", "CHECK_INTERVAL", "180") or "").strip()
    request_delay_raw = (os.environ.get("REQUEST_DELAY") or get("AO3", "REQUEST_DELAY", "4") or "").strip()
    state_file = (os.environ.get("STATE_FILE") or get("APP", "STATE_FILE", "bot_state.json") or "").strip()
    log_file = (os.environ.get("LOG_FILE") or get("APP", "LOG_FILE", "bot.log") or "").strip()

    try:
        check_interval = int(check_interval_raw) if check_interval_raw else 180
        request_delay = int(request_delay_raw) if request_delay_raw else 4
    except ValueError:
        print("CHECK_INTERVAL and REQUEST_DELAY must be integers.")
        sys.exit(1)

    if not bot_token or not username:
        print("BOT_TOKEN and AO3 USERNAME are required. Set in config.ini or environment.")
        print("  config.ini: [TELEGRAM] BOT_TOKEN = ... and [AO3] USERNAME = ...")
        sys.exit(1)

    return {
        "BOT_TOKEN": bot_token,
        "AO3_USERNAME": username.strip(),
        "CHECK_INTERVAL": check_interval,
        "REQUEST_DELAY": request_delay,
        "STATE_FILE": state_file or "bot_state.json",
        "LOG_FILE": log_file or None,
        "_config_path": path,
    }
