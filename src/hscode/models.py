"""现有数据库的 ORM 映射；应用启动不会建表或修改结构。"""
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, Computed, DateTime, Integer, SmallInteger, Text, func
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .embeddings import DIMENSION


class Base(DeclarativeBase):
    pass


class HSCode(Base):
    __tablename__ = "hs_code"

    country: Mapped[str] = mapped_column(Text, primary_key=True)
    code: Mapped[str] = mapped_column(Text, primary_key=True)
    code_display: Mapped[str | None] = mapped_column(Text)
    code_len: Mapped[int] = mapped_column(SmallInteger)
    description: Mapped[str] = mapped_column(Text)
    description_en: Mapped[str | None] = mapped_column(Text)
    unit1: Mapped[str | None] = mapped_column(Text)
    unit2: Mapped[str | None] = mapped_column(Text)
    hs2: Mapped[str] = mapped_column(Text)
    hs4: Mapped[str] = mapped_column(Text)
    hs6: Mapped[str] = mapped_column(Text)
    hs8: Mapped[str | None] = mapped_column(Text)
    indent: Mapped[int | None] = mapped_column(SmallInteger)
    path_desc: Mapped[str | None] = mapped_column(Text)
    is_declarable: Mapped[bool | None] = mapped_column(Boolean, server_default="false")
    search_tokens: Mapped[str | None] = mapped_column(Text)
    search_tokens_path: Mapped[str | None] = mapped_column(Text)
    search_vec: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple'::regconfig, coalesce(search_tokens, ''))", persisted=True),
    )
    search_vec_path: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple'::regconfig, coalesce(search_tokens_path, ''))", persisted=True),
    )
    search_vec_en: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english'::regconfig, coalesce(description_en, ''))", persisted=True),
    )
    # 基础库没有该列；普通实体读取也不能隐式查询它。
    embedding: Mapped[list[float] | None] = mapped_column(Vector(DIMENSION), deferred=True)


class HSChapter(Base):
    __tablename__ = "hs_chapter"

    country: Mapped[str] = mapped_column(Text, primary_key=True)
    chapter: Mapped[str] = mapped_column(Text, primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    code_count: Mapped[int | None] = mapped_column(Integer)


class DataRelease(Base):
    __tablename__ = "data_release"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    country: Mapped[str] = mapped_column(Text)
    system: Mapped[str] = mapped_column(Text)
    release: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    record_count: Mapped[int | None] = mapped_column(Integer)
