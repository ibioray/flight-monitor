import os
import logging
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Setup logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger("config")

# Credentials
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TRAVELPAYOUTS_TOKEN = os.getenv("TRAVELPAYOUTS_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")


def _parse_int_set(value: str) -> set[int]:
    ids = set()
    for part in (value or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            logger.warning("Ignoring invalid integer id in env list: %s", part)
    return ids

# DB settings
DATABASE_PATH = os.getenv("DATABASE_PATH", "flight_monitor.db")

# Admin / quota settings
ADMIN_USER_IDS = _parse_int_set(os.getenv("ADMIN_USER_IDS", ""))
DEFAULT_DAILY_SEARCH_LIMIT = int(os.getenv("DEFAULT_DAILY_SEARCH_LIMIT", "2"))

# Validation
if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "your_telegram_bot_token_here":
    logger.warning("TELEGRAM_BOT_TOKEN is missing or not set!")

if not TRAVELPAYOUTS_TOKEN or TRAVELPAYOUTS_TOKEN == "your_travelpayouts_token_here":
    logger.warning("TRAVELPAYOUTS_TOKEN is missing or not set! Flight price queries will fail.")

if not OPENROUTER_API_KEY and (not GEMINI_API_KEY or GEMINI_API_KEY == "your_gemini_api_key_here"):
    logger.warning("Neither OPENROUTER_API_KEY nor GEMINI_API_KEY is set! LLM route analysis will fail.")
