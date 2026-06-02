import asyncio
import logging
from db import init_db
from bot import main as start_bot

# Setup logger for main module
logger = logging.getLogger("main")

async def main():
    logger.info("Initializing Smart Flight Monitor system...")
    # Initialize SQLite tables and populate transit hubs
    init_db()
    # Run Telegram bot polling and APScheduler background tasks
    await start_bot()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("System stopped.")
