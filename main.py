import asyncio
import logging
import sys
from vkbottle import Bot
from core.config import settings
from database.engine import init_db
from database.service import seed_default_settings
from tasks.scheduler import start_scheduler, set_api

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ── Validate critical env vars ───────────────────────────────────────────────
if settings.VK_TOKEN == "placeholder_vk_token":
    logger.error("VK_TOKEN is not set! Copy .env.example to .env and fill in your credentials.")
    sys.exit(1)

if settings.OPENROUTER_API_KEY == "placeholder_openrouter_key":
    logger.warning("OPENROUTER_API_KEY is not set — AI features will not work.")

# ── Bot init ─────────────────────────────────────────────────────────────────
bot = Bot(token=settings.VK_TOKEN)

# ── Register all blueprints (order matters: specific patterns before catch-all) ──
from handlers.admin import bp as admin_bp
from handlers.messages import bp as messages_bp
from handlers.comments import bp as comments_bp

bot.labeler.load(admin_bp.labeler)       # /помощь, /пост, /настройка — checked first
bot.labeler.load(messages_bp.labeler)    # !профиль, !гороскоп, !кто я, then catch-all <text>
bot.labeler.load(comments_bp.labeler)    # Wall comment moderation (raw events)


# ── Startup ──────────────────────────────────────────────────────────────────
async def on_startup():
    """Run once before bot starts polling."""
    logger.info("Initializing database...")
    await init_db()
    logger.info("Seeding default settings...")
    await seed_default_settings()
    logger.info("Starting post scheduler...")
    set_api(bot.api)
    await start_scheduler()
    logger.info("✅ VK AI Admin Bot is ready!")


if __name__ == "__main__":
    # Use bot.loop_wrapper to run startup before polling
    bot.loop_wrapper.on_startup.append(on_startup())
    logger.info("Polling started...")
    bot.run_forever()
