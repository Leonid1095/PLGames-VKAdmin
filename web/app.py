"""FastAPI application — the main web server for multi-tenant VKAdmin."""

import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from database.engine import init_db
from database.service import create_group, seed_default_settings
from tasks.scheduler import start_scheduler
from web.vk_callback import router as callback_router
from web.oauth import router as oauth_router
from web.dashboard.routes import router as dashboard_router

logger = logging.getLogger(__name__)


async def _migrate_legacy_group():
    """
    If VK_TOKEN is set in .env (legacy single-group mode),
    auto-register it as a group in the new multi-tenant DB.
    """
    from core.config import settings
    if not settings.VK_TOKEN:
        return

    from database.service import get_group
    group_id = int(settings.VK_GROUP_ID)
    existing = await get_group(group_id)
    if existing:
        return

    logger.info(f"Migrating legacy group {group_id} from .env...")
    from core.crypto import encrypt_token
    encrypted = encrypt_token(settings.VK_TOKEN)

    await create_group(
        group_id=group_id,
        group_name=f"Legacy Group {group_id}",
        access_token=encrypted,
        admin_vk_id=int(settings.OWNER_VK_ID),
    )
    await seed_default_settings(group_id)
    logger.info(f"Legacy group {group_id} migrated successfully.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic."""
    logger.info("Starting VKAdmin...")
    await init_db()
    await _migrate_legacy_group()
    await start_scheduler()
    logger.info("VKAdmin is ready!")
    yield
    logger.info("Shutting down VKAdmin...")


app = FastAPI(
    title="VKAdmin",
    description="AI-администратор для групп ВКонтакте",
    lifespan=lifespan,
)

app.include_router(callback_router)
app.include_router(oauth_router)
app.include_router(dashboard_router)


@app.get("/")
async def root():
    return RedirectResponse("/dashboard")


@app.get("/api/health")
async def health():
    return {"status": "ok"}
