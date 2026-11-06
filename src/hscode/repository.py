"""业务数据查询：基于 ORM 模型构造表达式，不拼接查询条件或用户输入。"""
from typing import Any

from sqlalchemy import exists, func, inspect, literal, or_, select
from sqlalchemy.orm import Session

from .models import DataRelease, HSChapter, HSCode

CODE_FIELDS = (
    "country", "code", "code_display", "code_len", "description", "description_en",
    "unit1", "unit2", "hs2", "hs4", "hs6", "hs8", "indent", "path_desc", "is_declarable",
)
CODE_COLUMNS = tuple(getattr(HSCode, name) for name in CODE_FIELDS)
SEARCH_VECTORS = {
    "search_vec": HSCode.search_vec,
    "search_vec_path": HSCode.search_vec_path,
    "search_vec_en": HSCode.search_vec_en,
}


def rows(session: Session, statement) -> list[dict[str, Any]]:
    # 排序和返回清洗会修改字典；SQLAlchemy RowMapping 本身不可变。
    return [dict(row) for row in session.execute(statement).mappings()]


def code_filter(country: str, chapter: str | None = None) -> list:
    conditions = [HSCode.country == country]
    if chapter is not None:
        conditions.append(HSCode.hs2 == chapter)
    return conditions


def full_text(session: Session, country: str, vector: str, query: str,
              limit: int, chapter: str | None = None) -> list[dict]:
    vec = SEARCH_VECTORS[vector]
    tsquery = func.to_tsquery("english" if vector == "search_vec_en" else "simple", query)
    rank = func.ts_rank(vec, tsquery).label("_rank")
    return rows(session, select(*CODE_COLUMNS, HSCode.search_tokens.label("_tokens"), rank)
                .where(*code_filter(country, chapter), vec.bool_op("@@")(tsquery))
                .order_by(rank.desc(), func.length(func.coalesce(HSCode.description_en, HSCode.description)))
                .limit(limit))


def token_candidates(session: Session, country: str, vector: str, token: str,
                     limit: int, chapter: str | None = None) -> list[dict]:
    vec = SEARCH_VECTORS[vector]
    query = func.plainto_tsquery("english" if vector == "search_vec_en" else "simple", token)
    return rows(session, select(*CODE_COLUMNS, HSCode.search_tokens.label("_tokens"))
                .where(*code_filter(country, chapter), vec.bool_op("@@")(query))
                .order_by(func.length(HSCode.description)).limit(limit))


def anchor_candidates(session: Session, query: str, limit: int) -> list[dict]:
    tsquery = func.to_tsquery("simple", query)
    return rows(session, select(HSCode.hs6, HSCode.code, HSCode.description, HSCode.search_tokens)
                .where(HSCode.country == "CN", HSCode.search_vec.bool_op("@@")(tsquery))
                .order_by(func.ts_rank(HSCode.search_vec, tsquery).desc(), HSCode.code).limit(limit))


def chinese_hints(session: Session, anchors: list[str]) -> list[dict]:
    return rows(session, select(HSCode.hs6, HSCode.code, HSCode.description)
                .where(HSCode.country == "CN", HSCode.hs6.in_(anchors))
                .order_by(HSCode.hs6, HSCode.code))


def by_anchors(session: Session, country: str, anchors: list[str], limit: int,
               chapter: str | None = None) -> list[dict]:
    return rows(session, select(*CODE_COLUMNS)
                .where(*code_filter(country, chapter), HSCode.hs6.in_(anchors))
                .order_by(HSCode.code_len, HSCode.code).limit(limit))


def substring(session: Session, country: str, keyword: str, limit: int,
              chapter: str | None = None) -> list[dict]:
    pattern = f"%{keyword}%"
    return rows(session, select(*CODE_COLUMNS, HSCode.search_tokens.label("_tokens"), literal(0.0).label("_rank"))
                .where(*code_filter(country, chapter), or_(HSCode.description.ilike(pattern),
                                                        HSCode.description_en.ilike(pattern)))
                .order_by(HSCode.code).limit(limit))


def detail(session: Session, country: str, code: str) -> dict | None:
    found = rows(session, select(*CODE_COLUMNS).where(HSCode.country == country, HSCode.code == code))
    return found[0] if found else None


def chapters(session: Session, country: str | None = None) -> list[dict]:
    statement = select(HSChapter.country, HSChapter.chapter, HSChapter.title, HSChapter.code_count)
    if country is not None:
        statement = statement.where(HSChapter.country == country)
    return rows(session, statement.order_by(HSChapter.country, HSChapter.chapter))


def chapter_codes(session: Session, country: str, chapter: str,
                  heading: str | None, limit: int) -> tuple[int, list[dict]]:
    conditions = code_filter(country, chapter)
    if heading:
        conditions.append(HSCode.hs4 == heading)
    total = session.scalar(select(func.count()).select_from(HSCode).where(*conditions))
    fields = (HSCode.code, HSCode.code_display, HSCode.code_len, HSCode.description,
              HSCode.description_en, HSCode.unit1, HSCode.unit2, HSCode.hs4,
              HSCode.hs6, HSCode.path_desc, HSCode.is_declarable)
    return total, rows(session, select(*fields).where(*conditions).order_by(HSCode.code).limit(limit))


def word_stats(session: Session, country: str, vector: str) -> tuple[int, list[dict]]:
    total = session.scalar(select(func.count()).select_from(HSCode).where(HSCode.country == country))
    # ts_stat 的参数必须是 SQL 文本；由白名单列和 ORM 编译器生成。
    statement = select(SEARCH_VECTORS[vector]).where(HSCode.country == country)
    query = str(statement.compile(dialect=session.get_bind().dialect,
                                  compile_kwargs={"literal_binds": True}))
    stats = func.ts_stat(query).table_valued("word", "ndoc", "nentry")
    return total, rows(session, select(stats.c.word, stats.c.ndoc))


def vector_ready(session: Session) -> bool:
    present = any(c["name"] == "embedding" for c in inspect(session.connection()).get_columns("hs_code"))
    if not present:
        return False
    return bool(session.scalar(select(
        exists(select(1).where(HSCode.country == "CN"))
        & ~exists(select(1).where(HSCode.country == "CN", HSCode.embedding.is_(None)))
    )))


def vector_neighbors(session: Session, vector, limit: int, chapter: str | None = None) -> list[dict]:
    distance = HSCode.embedding.cosine_distance(vector)
    return rows(session, select(*CODE_COLUMNS, (1 - distance).label("similarity"))
                .where(*code_filter("CN", chapter), HSCode.embedding.is_not(None))
                .order_by(distance).limit(limit))


def dataset_summary(session: Session) -> tuple[dict, dict, dict]:
    counts = dict(session.execute(select(HSCode.country, func.count()).group_by(HSCode.country)).all())
    releases = {row["country"]: row for row in rows(session, select(
        DataRelease.country, DataRelease.system, DataRelease.release,
        DataRelease.source_url, DataRelease.record_count, DataRelease.imported_at,
    ))}
    quality = rows(session, select(
        func.count().filter(HSCode.description.like("%?%")).label("bad_desc"),
        func.count().filter(HSCode.description_en.like("%?%")).label("bad_en"),
    ).where(HSCode.country == "CN"))[0]
    return counts, releases, quality
