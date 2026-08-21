import os
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


def _normalize_database_url(value: str) -> str:
    if value.startswith("sqlite:///") and not value.startswith("sqlite+aiosqlite:///"):
        return value.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    if value.startswith("mysql://"):
        return value.replace("mysql://", "mysql+aiomysql://", 1)
    return value


_default_db = Path(__file__).resolve().parents[1] / "data" / "job_competency.db"
_default_db.parent.mkdir(parents=True, exist_ok=True)
ASYNC_DATABASE_URL = _normalize_database_url(
    os.getenv("DATABASE_URL", f"sqlite+aiosqlite:///{_default_db.as_posix()}")
)

engine_options = {"echo": os.getenv("SQL_ECHO", "false").lower() == "true"}
if not ASYNC_DATABASE_URL.startswith("sqlite"):
    engine_options.update(pool_size=10, max_overflow=20, pool_pre_ping=True)

async_engine = create_async_engine(ASYNC_DATABASE_URL, **engine_options)
AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    from model_class.base import Base
    import model_class.job_competency  # noqa: F401
    import model_class.knowledge_base  # noqa: F401
    from src.schema_migration import ensure_competition_schema

    async with async_engine.begin() as connection:
        await ensure_competition_schema(connection)
        await connection.run_sync(Base.metadata.create_all)
