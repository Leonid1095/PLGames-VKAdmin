from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from core.config import settings
from database.models import Base
import logging

logger = logging.getLogger(__name__)

_is_sqlite = settings.DATABASE_URL.startswith("sqlite")

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    # Driver-level busy timeout (seconds) — wait for the writer lock instead of
    # raising "database is locked" immediately under concurrent fire-and-forget tasks.
    connect_args={"timeout": 30} if _is_sqlite else {},
)

if _is_sqlite:
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, connection_record):
        # WAL allows concurrent readers alongside a single writer; busy_timeout
        # (ms, per-connection) backs off instead of failing on lock contention.
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

async_session = async_sessionmaker(
    bind=engine, class_=AsyncSession, expire_on_commit=False
)


def _apply_light_migrations(conn):
    """Additive, idempotent schema upgrades that create_all() can't do on its
    own (it never ALTERs existing tables). Safe to run on every startup."""
    if not _is_sqlite:
        return
    rows = conn.exec_driver_sql("PRAGMA table_info(scheduled_posts)").fetchall()
    cols = {r[1] for r in rows}
    if "attempts" not in cols:
        conn.exec_driver_sql(
            "ALTER TABLE scheduled_posts ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
        )
        logger.info("Migration: added scheduled_posts.attempts column.")


async def init_db():
    """Create all tables in the database."""
    logger.info("Initializing database...")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_apply_light_migrations)
    logger.info("Database initialized.")
