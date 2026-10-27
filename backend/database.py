import os
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base

load_dotenv()

raw_url = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./civicfix.db")

if raw_url.startswith("postgresql://"):
    db_url = raw_url.replace("postgresql://", "postgresql+psycopg://", 1)
elif raw_url.startswith("postgresql+asyncpg://"):
    db_url = raw_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
else:
    db_url = raw_url

if "ssl=require" in db_url:
    db_url = db_url.replace("ssl=require", "sslmode=require")

engine = create_async_engine(
    db_url,
    echo=False,
    pool_pre_ping=True
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)

Base = declarative_base()

async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()