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
logger = logging.getLogger("config")

# Credentials
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TRAVELPAYOUTS_TOKEN = os.getenv("TRAVELPAYOUTS_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# DB settings
DATABASE_PATH = os.getenv("DATABASE_PATH", "flight_monitor.db")

# Validation
if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "your_telegram_bot_token_here":
    logger.warning("TELEGRAM_BOT_TOKEN is missing or not set!")

if not TRAVELPAYOUTS_TOKEN or TRAVELPAYOUTS_TOKEN == "your_travelpayouts_token_here":
    logger.warning("TRAVELPAYOUTS_TOKEN is missing or not set! Flight price queries will fail.")

if not OPENROUTER_API_KEY and (not GEMINI_API_KEY or GEMINI_API_KEY == "your_gemini_api_key_here"):
    logger.warning("Neither OPENROUTER_API_KEY nor GEMINI_API_KEY is set! LLM route analysis will fail.")
