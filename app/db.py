from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from app.config import Config

class Base(DeclarativeBase):
    pass

def make_engine(cfg: Config):
    return create_async_engine(cfg.database_url, pool_pre_ping=True)

def make_session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
