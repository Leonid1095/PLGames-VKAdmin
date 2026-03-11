import logging
import uvicorn

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ── Import the FastAPI app ───────────────────────────────────────────────────
from web.app import app  # noqa: E402

if __name__ == "__main__":
    logger.info("Starting VKAdmin server...")
    uvicorn.run(
        "web.app:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )
