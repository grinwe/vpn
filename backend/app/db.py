import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable is required")

# pool_pre_ping: transparently recycle connections that the DB server
# closed out from under us (postgres idle timeout, restart, failover).
# pool_recycle: proactively close connections older than 30 min to avoid
# lingering stale sockets. pool_size/max_overflow are conservative and
# tuned for a single backend container with ~4 uvicorn workers.
engine = create_engine(
    DATABASE_URL,
    future=True,
    pool_pre_ping=True,
    pool_size=int(os.getenv("DB_POOL_SIZE", "10")),
    max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "10")),
    pool_recycle=int(os.getenv("DB_POOL_RECYCLE", "1800")),
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()
