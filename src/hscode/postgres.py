"""PostgreSQL 专用 DDL；数据和元信息查询使用 SQLAlchemy API。

SQLAlchemy 没有建库、扩展、加列、表锁和 ANALYZE 的通用 schema API，
这些 DDL 集中在这里；标识符交给方言引用，列与索引使用 schema 编译器。
"""
from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, Connection, DDL, Index, MetaData, Table, inspect
from sqlalchemy.schema import CreateColumn

from .embeddings import DIMENSION
from .models import HSCode


def create_database(connection: Connection, name: str) -> None:
    connection.execute(DDL("CREATE DATABASE %(database)s TEMPLATE template0", context={
        "database": connection.dialect.identifier_preparer.quote_identifier(name),
    }))


def drop_database(connection: Connection, name: str) -> None:
    connection.execute(DDL("DROP DATABASE %(database)s WITH (FORCE)", context={
        "database": connection.dialect.identifier_preparer.quote_identifier(name),
    }))


def lock_tables(connection: Connection, *tables: Table) -> None:
    connection.execute(DDL("LOCK TABLE %(tables)s IN SHARE ROW EXCLUSIVE MODE", context={
        "tables": ", ".join(connection.dialect.identifier_preparer.format_table(t) for t in tables),
    }))


def ensure_vector_column(connection: Connection) -> None:
    connection.execute(DDL("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public"))
    connection.execute(DDL("ALTER TABLE %(table_name)s ADD COLUMN IF NOT EXISTS %(column_definition)s", context={
        "table_name": connection.dialect.identifier_preparer.format_table(HSCode.__table__),
        "column_definition": str(CreateColumn(HSCode.__table__.c.embedding).compile(dialect=connection.dialect)),
    }))
    column_type = next(c["type"] for c in inspect(connection).get_columns("hs_code") if c["name"] == "embedding")
    if not isinstance(column_type, Vector) or column_type.dim != DIMENSION:
        raise RuntimeError(f"embedding 类型不匹配：{column_type}")


def ensure_vector_index(connection: Connection) -> None:
    # 独立的 DDL metadata，避免将可选索引加入基础 ORM 表的建表定义。
    table = Table("hs_code", MetaData(), Column("embedding", Vector(DIMENSION)))
    Index("idx_hs_code_embedding", table.c.embedding, postgresql_using="hnsw",
          postgresql_ops={"embedding": "public.vector_cosine_ops"},
          postgresql_with={"m": 16, "ef_construction": 64}).create(connection, checkfirst=True)
    connection.execute(DDL("ANALYZE %(table_name)s", context={
        "table_name": connection.dialect.identifier_preparer.format_table(table),
    }))


def remove_vector_schema(connection: Connection) -> None:
    connection.execute(DDL("ALTER TABLE %(table_name)s DROP COLUMN IF EXISTS %(column_name)s", context={
        "table_name": connection.dialect.identifier_preparer.format_table(HSCode.__table__),
        "column_name": connection.dialect.identifier_preparer.quote_identifier("embedding"),
    }))
    connection.execute(DDL("DROP EXTENSION IF EXISTS vector"))
