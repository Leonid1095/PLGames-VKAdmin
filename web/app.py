"""FastAPI application — the main web server for multi-tenant VKAdmin."""

import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import RedirectResponse

# Configure logging at import time. Production runs `uvicorn web.app:app`
# directly (see vkadmin.service), which never executes main.py — so without
# this, app-level logs (incl. the LLM health-check failure) never reach the
# journal and the operator is blind. basicConfig is a no-op if already set.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

from database.engine import init_db
from database.service import create_group, seed_default_settings
from tasks.scheduler import start_scheduler
from web.vk_callback import router as callback_router
from web.oauth import router as oauth_router
from web.dashboard.routes import router as dashboard_router
from web.api_public import router as api_router
from web.miniapp.routes import router as miniapp_router

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

    # Auto-setup AI personality
    try:
        from core.group_setup import setup_group_ai
        await setup_group_ai(group_id, settings.VK_TOKEN)
    except Exception as e:
        logger.warning(f"AI setup for legacy group failed: {e}")

    logger.info(f"Legacy group {group_id} migrated successfully.")


async def _probe_llm_health():
    """Background LLM probe — logs the result without blocking startup."""
    from core.agent import check_llm_health
    from core.config import settings
    try:
        ok, detail = await check_llm_health()
    except asyncio.CancelledError:
        return
    if ok:
        logger.info("LLM health-check OK (model=%s)", settings.DEFAULT_MODEL)
    else:
        logger.error(
            "LLM HEALTH-CHECK FAILED — AI replies will NOT work until fixed: %s",
            detail,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic."""
    logger.info("Starting VKAdmin...")
    from core.config import validate_critical_settings
    validate_critical_settings()
    await init_db()
    await _migrate_legacy_group()
    await start_scheduler()

    # Probe the AI provider so a dead key surfaces in the log instead of
    # masquerading as a generic "Произошла ошибка" in every chat reply.
    # Run it in the BACKGROUND: the probe hits a cold local model and can take
    # 20–40s, and blocking the lifespan here would keep the HTTP server (and
    # the VK Callback endpoint VK pings to confirm the integration) unreachable
    # for that whole window. Fire-and-forget so the server binds immediately.
    health_task = asyncio.create_task(_probe_llm_health())

    logger.info("VKAdmin is ready!")
    yield
    health_task.cancel()
    logger.info("Shutting down VKAdmin...")


app = FastAPI(
    title="VKAdmin",
    description="AI-администратор для групп ВКонтакте",
    lifespan=lifespan,
)

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest


class VKFrameMiddleware(BaseHTTPMiddleware):
    """Security headers. VK Mini App pages must be embeddable in the VK iframe
    (and load vk-bridge from a CDN); everything else (dashboard) is locked down:
    no framing, nosniff, and a CSP that still allows the inline scripts/handlers
    the dashboard relies on."""
    async def dispatch(self, request: StarletteRequest, call_next):
        response = await call_next(request)
        path = request.url.path

        # Baseline hardening applied to every response.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")

        is_vk_frame = (
            path.startswith("/miniapp")
            or (path == "/api/vk/callback" and request.query_params.get("vk_app_id"))
        )
        if is_vk_frame:
            response.headers["X-Frame-Options"] = ""
            response.headers["Content-Security-Policy"] = (
                "frame-ancestors https://*.vk.com https://vk.com"
            )
        else:
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: https:; "
                "connect-src 'self'; "
                "base-uri 'self'; "
                "form-action 'self'; "
                "frame-ancestors 'none'"
            )
        return response

app.add_middleware(VKFrameMiddleware)

app.include_router(callback_router)
app.include_router(oauth_router)
app.include_router(dashboard_router)
app.include_router(api_router)
app.include_router(miniapp_router)


@app.get("/")
async def root():
    return RedirectResponse("/dashboard")


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/health/llm")
async def health_llm():
    """Live probe of the AI provider — distinguishes a rejected key from an
    unreachable provider. Does a real (tiny) call, so don't poll it hard."""
    from core.agent import check_llm_health
    ok, detail = await check_llm_health()
    return {"status": "ok" if ok else "error", "detail": detail}
