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
#
# pool_timeout: почти все эндпоинты — sync def и исполняются в anyio-threadpool
# (дефолт 40 потоков), а каждый через Depends(get_db) берёт соединение из пула
# (pool_size + max_overflow = 20 по умолчанию). Если одновременных запросов
# больше, чем соединений, лишние ждут свободного соединения. С дефолтным
# pool_timeout=30 они висят полминуты и только потом падают — под всплеском
# (bot flood) это выглядит как «всё зависло». Короткий таймаут даёт быстрый
# отказ (500) вместо долгой очереди. Чтобы согласовать лимиты полностью, на
# уровне entrypoint стоит либо поднять пул до размера threadpool'а, либо
# ограничить threadpool до размера пула (anyio thread limiter).
engine = create_engine(
    DATABASE_URL,
    future=True,
    pool_pre_ping=True,
    pool_size=int(os.getenv("DB_POOL_SIZE", "10")),
    max_overflow=int(os.getenv("DB_MAX_OVERFLOW", "10")),
    pool_recycle=int(os.getenv("DB_POOL_RECYCLE", "1800")),
    pool_timeout=int(os.getenv("DB_POOL_TIMEOUT", "5")),
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()
