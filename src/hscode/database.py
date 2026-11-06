"""统一 SQLAlchemy 连接池与 Session，兼容 PostgreSQL URL 和 libpq DSN。"""
from contextlib import contextmanager
from functools import lru_cache
from collections.abc import Iterator

import psycopg
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from .config import DEFAULT_DSN


def make_engine(dsn: str) -> Engine:
    if not dsn:
        raise ValueError("请在 .env 或环境变量中设置 DATABASE_URL")
    # 交给 libpq 解析，保留 options/search_path、SSL 和密码转义语义。
    return create_engine(
        "postgresql+psycopg://",
        creator=lambda: psycopg.connect(dsn),
        pool_pre_ping=True,
        hide_parameters=True,
    )


@lru_cache(maxsize=8)
def get_engine(dsn: str = DEFAULT_DSN) -> Engine:
    return make_engine(dsn)


def session(dsn: str = DEFAULT_DSN) -> Session:
    return Session(get_engine(dsn))


@contextmanager
def maintenance_session(dsn: str) -> Iterator[Session]:
    """维护任务使用独立连接池，事务退出即提交/回滚并释放所有连接。"""
    engine = make_engine(dsn)
    try:
        with Session(engine) as connection, connection.begin():
            yield connection
    finally:
        engine.dispose()
