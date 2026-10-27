"""Database connection for PostgreSQL (SQLAlchemy)."""
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app import config

# Build the connection address safely (handles special characters in passwords)
DATABASE_URL = URL.create(
    drivername="postgresql+psycopg2",
    username=config.POSTGRES_USER,
    password=config.POSTGRES_PASSWORD,
    host=config.POSTGRES_HOST,
    port=config.POSTGRES_PORT,
    database=config.POSTGRES_DB,
)

# The engine manages a pool of reusable connections.
# pool_pre_ping checks a connection is alive before using it.
# Phase 8: up to 60 connections (PostgreSQL allows 100 by default), because
# the audit log writes on its own connection.
engine = create_engine(DATABASE_URL, pool_pre_ping=True,
                       pool_size=20, max_overflow=40)

# A session factory: call SessionLocal() to get a new session.
# expire_on_commit=False keeps objects readable after commit.
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    """Parent class of every model (table) we define."""


def get_session():
    """Gives one session per API request and always closes it."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()