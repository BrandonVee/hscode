#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""hscode MCP Server
====================

把中国海关编码（8 位）与美国 HTS 编码（4/6/8/10 位）的检索、详情、
层级浏览与跨市场对照能力，以 MCP 工具形式提供给 AI 客户端。

范围：**编码查询**。不含税率、监管条件、申报要素（数据源本身没有，或本期不处理）。

工具：
    search_hs_code           关键词检索（中/英）
    get_hs_detail            编码详情
    list_chapter_codes       按章/品目浏览
    compare_across_countries 跨市场对照（以 HS6 为锚点）

资源：hs://dataset-info、hs://chapters
提示词：hs_classify、hs_cross_market

查询层的两处数据补偿（都不改库，下次重建数据仍适用）：
  1. 单位归一：中国源数据用 '?' 占位的第二法定单位 → null + unit_missing 标记；
  2. IDF 加权：补 Postgres ts_rank 缺失的稀有度维度，避免高频词淹没目标词。

口语词与税则用词的差异由**语义召回**兜底（第 4 档，中国数据带 embedding 列）：
  不需要维护词表，任意说法都能按语义找近邻；返回里带 similarity 供调用方判断可信度。
  美国数据没有向量，英文检索继续走 FTS；装不上 fastembed 时该档自动跳过。

数据准备（一次性）
------------------
基础结构 + 数据在 data/hscode_dump.sql 里；向量通过脚本从品名生成：

    uv sync
    createdb hscode
    psql -d hscode -f data/hscode_dump.sql     # 建表 + 灌数据
    uv run hscode-build-vectors               # 生成中国品名向量及 HNSW 索引

    基础导出不含向量字段或向量数据；生成向量需实例支持 pgvector，
    例如 pgvector/pgvector:pg16。未生成向量时仍可使用全文检索。

连接串走环境变量 HSCode_DSN，默认本机实例：
    postgresql://hscode:hscode-dev-password@127.0.0.1:5435/hscode

启动
----
    uv run hscode                 # stdio（本地 MCP 客户端默认）
    uv run hscode --http 8765       # Streamable HTTP
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from typing import Any, Literal

import psycopg
from psycopg.rows import dict_row
from mcp.server.mcpserver import MCPServer
from .config import DEFAULT_DSN
from .embeddings import load_model, vector_literal

SERVER_NAME = "hscode"
SERVER_VERSION = "1.0.0"

Country = Literal["CN", "US"]

DSN = os.environ.get("HSCode_DSN", DEFAULT_DSN)

DATA_NOTE = (
    "数据范围：中国为「2015 年基线全量 + 2016-2023 年变更记录」，"
    "已合并为编码查询目录，非逐年完整税则；美国为 USITC HTS 单版本快照。"
)

mcp = MCPServer(
    SERVER_NAME,
    instructions=(
        "海关商品编码（HS Code）查询服务，覆盖中国与美国两套体系。"
        "可检索编码、查看详情与层级，"
        "并以 HS6 为锚点做中美跨市场对照。"
        "注意：本服务不含税率数据；归类判断属专业行为，工具只提供候选与检索依据。"
    ),
    version=SERVER_VERSION,
)


# --------------------------------------------------------------------------- #
# 连接与分词
# --------------------------------------------------------------------------- #


def connect() -> psycopg.Connection:
    """连接到库（返回 dict 行）。"""
    return psycopg.connect(DSN, row_factory=dict_row)


_PUNCT = re.compile(r"^[\W_]+$", re.UNICODE)
_EN_STOP = {
    "the", "of", "and", "or", "for", "not", "with", "other", "than", "in", "to",
    "a", "an", "by", "on", "as", "etc", "thereof", "whether", "kind", "kinds",
    "including", "exceeding", "exceed", "over", "under", "no", "any", "which",
    "are", "is", "be", "been", "having", "containing", "articles", "article",
    "products", "product", "therefor", "specified", "included", "described",
}


def tokenize_cn(text: str) -> list[str]:
    """中文品名分词（jieba 搜索引擎模式），去除标点与重复词。"""
    import jieba  # 延迟导入：仅检索时用得到

    jieba.setLogLevel(60)
    out: list[str] = []
    seen: set[str] = set()
    for raw in jieba.cut_for_search(text or ""):
        tok = raw.strip()
        if not tok or _PUNCT.match(tok):
            continue
        tok = tok.lower()
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def tokenize_en(text: str) -> list[str]:
    """英文描述分词：小写 + 非字母数字切分 + 去停用词。"""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    out: list[str] = []
    seen: set[str] = set()
    for w in words:
        if len(w) < 2 or w in _EN_STOP or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


def tokenize(text: str, country: str) -> list[str]:
    return tokenize_en(text) if country == "US" else tokenize_cn(text)


# --------------------------------------------------------------------------- #
# 稀有度权重（IDF）
# --------------------------------------------------------------------------- #

_embed_model: object | None = None


def embed_model():
    """加载句向量模型（懒加载，进程内单例）。

    仅加载本地模型缓存，不在请求过程中联网下载；不可用时跳过向量档。
    其余检索路径照常工作——语义召回是增强项，不是必备依赖。
    """
    global _embed_model
    if _embed_model is None:
        try:
            _embed_model = load_model(download=False)
        except Exception:  # 缓存尚未准备好时，下次请求可重新尝试加载
            return None
    return _embed_model


def vector_recall(
    conn: psycopg.Connection,
    keyword: str,
    country: str,
    chap_clause: str,
    chap_params: tuple,
    limit: int,
) -> list[dict[str, Any]]:
    """语义召回：把查询和品名各自编码成向量，按余弦距离取最近邻。

    只对口语/语义类查询兜底——「保温杯」和税则用词「保温瓶」在字面上不重叠，
    任何词表或分词办法都救不了（词表只能覆盖人工设定的那几个词），
    而向量天然给得出这种语义相近。中国数据带 embedding 列时才可用，
    美国侧没有向量，继续走 FTS。
    """
    if country != "CN":
        return []
    ready = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_attribute "
        "WHERE attrelid='hs_code'::regclass AND attname='embedding' AND NOT attisdropped) AS ready"
    ).fetchone()["ready"]
    if not ready:
        return []
    complete = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM hs_code WHERE country='CN') "
        "AND NOT EXISTS (SELECT 1 FROM hs_code WHERE country='CN' AND embedding IS NULL) AS ready"
    ).fetchone()["ready"]
    if not complete or not embed_model():
        return []
    try:
        vec = next(iter(embed_model().embed([keyword])))
        literal = vector_literal(vec)
    except Exception:
        return []
    rows = conn.execute(
        f"""
        SELECT {_SELECT_COLS}, 1 - (embedding <=> %s::vector) AS similarity
        FROM hs_code
        WHERE country = %s AND embedding IS NOT NULL {chap_clause}
        ORDER BY embedding <=> %s::vector
        LIMIT %s
        """,
        (literal, country, *chap_params, literal, limit),
    ).fetchall()
    for r in rows:
        r["hit_count"] = 1
    return rows


_idf_cache: dict[tuple[str, str], dict[str, float]] = {}


def idf_map(country: str, vec_col: str = "search_vec") -> dict[str, float]:
    """tok → 逆文档频率，进程内缓存（数据静态，全表扫一次即可）。

    Postgres 的 ts_rank 只看词频与位置，不含 IDF，所以同词命中会全部同分，
    OR 检索里高频词（不锈钢）会把低频词（保温）的候选挤出窗口。这里补上稀有度。

    vec_col 决定统计哪套向量：search_vec（中文/美国英文）或 search_vec_en（中国英文），
    两套词表不同，必须分开统计。
    """
    if country not in ("CN", "US") or vec_col not in (
        "search_vec", "search_vec_path", "search_vec_en",
    ):
        return {}
    key = (country, vec_col)
    if key in _idf_cache:
        return _idf_cache[key]
    # ts_stat 收的是一段 SQL 文本，无法用占位符带 country，故先校验再拼
    with connect() as conn:
        total = conn.execute(
            "SELECT count(*) AS n FROM hs_code WHERE country = %s", (country,)
        ).fetchone()["n"]
        rows = conn.execute(
            "SELECT word, ndoc FROM ts_stat(%s)",
            (f"SELECT {vec_col} FROM hs_code WHERE country = '{country}'",),
        ).fetchall()
    cache = {r["word"]: math.log(1 + max(total, 1) / (1 + r["ndoc"])) for r in rows}
    _idf_cache[key] = cache
    return cache


def clean_row(row: dict[str, Any]) -> dict[str, Any]:
    """输出前的行清洗。

    1. 单位归一：源文件用 '?' 占位的法定单位 → None。
       源数据（code_hs.csv）整份是合法 UTF-8，'?' 是源头的单字节占位符，
       既可能表示「无第二法定单位」也可能是转码损坏，从现有数据无法区分，
       统一按缺失返回，避免把 '?' 当成真实单位用。
    2. en_corrupted：中国数据的英文品名同样有 '?' 污染（1,649 行），
       标记出来供调用方判断该条英文是否可信。
    """
    for key in ("unit1", "unit2"):
        val = row.get(key)
        if isinstance(val, str) and val.strip() in {"?", "？"}:
            row[key] = None
    row["unit_missing"] = row.get("unit2") is None
    en = row.get("description_en")
    row["en_corrupted"] = isinstance(en, str) and "?" in en
    return row


def _sanitize_token(tok: str) -> str:
    """仅保留字母/数字/汉字，用于拼装 tsquery，避免语法注入。"""
    return re.sub(r"[^\w\u4e00-\u9fff]", "", tok, flags=re.UNICODE)


def normalize_code(raw: str, country: str) -> str | None:
    """规整用户输入的编码：去掉点、空格、连字符，校验长度。"""
    digits = re.sub(r"\D", "", str(raw or ""))
    valid = (4, 6, 8, 10) if country == "US" else (8, 10)
    return digits if len(digits) in valid else None


_SELECT_COLS = """
    country, code, code_display, code_len, description, description_en,
    unit1, unit2, hs2, hs4, hs6, hs8, indent, path_desc, is_declarable
"""

# 向量 → 文本搜索配置。关键区别是 english 带 snowball 词干提取：
# 'flasks' 会被归一到 'flask'，否则搜 "vacuum flask" 命中不了 "vacuum flasks"。
# 中文档必须用 simple（english 配置会把中文整段丢掉）。
_VECTOR_CFG = {"search_vec_en": "english"}


def _cfg(vec_col: str) -> str:
    return _VECTOR_CFG.get(vec_col, "simple")


def _chapter_map(conn: psycopg.Connection, country: str) -> dict[str, str]:
    """取该国的章标题（数据自带，见 hs_chapter 表）。"""
    rows = conn.execute(
        "SELECT chapter, title FROM hs_chapter WHERE country = %s", (country,)
    ).fetchall()
    return {r["chapter"]: r["title"] for r in rows}


def _placeholder_desc(desc: str) -> bool:
    """是否是无信息量的兜底描述。

    美国末级大量叫 Other / Parts / Nesoi——它们本身有效，只是不描述商品，
    排序时应让位给同级「说了具体内容」的行。这是 HTS 的行文习惯，
    不是通用规则，故集中在此一处维护（原先散嵌在排序 key 里的硬编码列表）。
    """
    tok = re.sub(r"[^a-z ]", "", (desc or "").strip().lower()).strip()
    return tok in {"other", "parts", "nes", "nesoi", "other nesoi"}


def _cn_anchors(conn: psycopg.Connection, keyword: str, limit: int) -> list[str]:
    """用中文关键词在中国数据里定位 HS6 锚点，按相关度返回去重后的 HS6 列表。

    单独抽出来，避免在 search() 里递归调用自己（原先 US 分支直接 call search(CN)）。
    """
    tokens = [t for t in (_sanitize_token(t) for t in tokenize_cn(keyword)) if t]
    if not tokens:
        return []
    tsq = " & ".join(tokens)
    cfg = _cfg("search_vec")
    rows = conn.execute(
        """
        SELECT hs6, code, description, search_tokens
        FROM hs_code
        WHERE country = 'CN' AND search_vec @@ to_tsquery(%s, %s)
        ORDER BY ts_rank(search_vec, to_tsquery(%s, %s)) DESC, code
        LIMIT %s
        """,
        (cfg, tsq, cfg, tsq, limit * 5),
    ).fetchall()
    if not rows:  # AND 无果放宽为 OR，否则「宠物食品」这类口语表达直接查不到
        tsq = " | ".join(tokens)
        rows = conn.execute(
            """
            SELECT hs6, code, description, search_tokens
            FROM hs_code
            WHERE country = 'CN' AND search_vec @@ to_tsquery(%s, %s)
            ORDER BY ts_rank(search_vec, to_tsquery(%s, %s)) DESC, code
            LIMIT %s
            """,
            (cfg, tsq, cfg, tsq, limit * 5),
        ).fetchall()

    # 重排：命中词数 > 品名长度。只看 ts_rank 会把长描述（带一堆限定语）排到前面，
    # 而它的 HS6 未必是用户想要的锚点（搜「保温瓶」时玻璃胆那行会压过保温瓶本体）。
    qset = set(tokens)
    for r in rows:
        r["_hit"] = len(qset & set((r.get("search_tokens") or "").split()))
    rows.sort(key=lambda r: (-r["_hit"], len(r.get("description") or ""), r["code"]))

    seen: list[str] = []
    for r in rows:
        if r["hs6"] not in seen:
            seen.append(r["hs6"])
    return seen[:limit]


def attach_zh(
    conn: psycopg.Connection, rows: list[dict[str, Any]], country: str
) -> list[dict[str, Any]]:
    """给美国条目挂中文别名（zh_hint）。

    美国数据只有英文。这里**不做翻译**，而是取同一 HS6 子目下中国数据的中文品名
    作为「同级中文名」——HS6 是国际统一层级，所以这个中文名落在同一个国际子目里，
    但美国 8 位以下的定义各国自定，粒度通常更粗（美国末级常叫 Other，
    借到的中文会是上位概念）。一律带 zh_is_translation=false，提醒调用方：
    这是同一子目的参照名，不是原文译文，不能当品名引用。

    覆盖率：10 位码 99%、6 位码 99%、8 位码 49%、4 位码 0%
    （美国 4 位码的 hs6 只截到 4 位，天然对不上）。
    """
    if country != "US" or not rows:
        return rows
    anchors = sorted({r["hs6"] for r in rows if r.get("hs6")})
    if not anchors:
        return rows
    cn_rows = conn.execute(
        """
        SELECT hs6, code, description FROM hs_code
        WHERE country = 'CN' AND hs6 = ANY(%s) ORDER BY hs6, code
        """,
        (anchors,),
    ).fetchall()
    by_hs6: dict[str, list[str]] = {}
    for r in cn_rows:
        by_hs6.setdefault(r["hs6"], []).append(r["description"])

    for r in rows:
        hints = by_hs6.get(r["hs6"], [])
        r["zh_hint"] = hints[:3]
        r["zh_source"] = f"hs6_from_CN_{r['hs6']}" if hints else None
        r["zh_is_translation"] = False
    return rows


def _fold_en(word: str) -> str:
    """粗略还原词干，只为比对命中数用（真正的检索 ringing 交给 Postgres 的 english 配置）。

    英文数据里 'flasks'/'lobsters' 是常态，查询词往往是单数，不折叠会算不中。
    """
    w = word.lower()
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("es"):
        return w[:-2]
    if len(w) > 3 and w.endswith("s"):
        return w[:-1]
    return w


def _ascii_tokens(text: str | None) -> set[str]:
    """英文文本的 token 集合（已折叠词干）。"""
    return {_fold_en(w) for w in re.findall(r"[a-z0-9]+", (text or "").lower())}


def _rank(rows: list[dict[str, Any]], tokens: list[str]) -> list[dict[str, Any]]:
    """排序权重：命中 token 数 > ts_rank > 品名长度（短品名通常更精确）。"""
    qset = set(tokens)
    # 英文查询不能直接比对中文分词列，改看英文品名
    ascii_only = bool(tokens) and all(re.fullmatch(r"[a-z0-9]+", t) for t in tokens)
    if ascii_only:
        qset = {_fold_en(t) for t in tokens}

    def row_tokens(r: dict[str, Any]) -> set[str]:
        return _ascii_tokens(r.get("description_en")) if ascii_only else \
            set((r.get("_tokens") or "").split())

    for r in rows:
        r["hit_count"] = len(qset & row_tokens(r))
    rows.sort(
        key=lambda r: (
            -r["hit_count"],
            -float(r.get("_rank") or 0.0),
            len(r.get("description") or ""),
            r["code"],
        )
    )
    return rows


def _or_candidates(
    conn: psycopg.Connection,
    tokens: list[str],
    vec_col: str,
    country: str,
    chap_clause: str,
    chap_params: tuple,
    limit: int,
) -> list[dict[str, Any]]:
    """OR 检索：按 token 稀有度逐词取候选，再按 IDF 之和排序。

    不能用一个 OR tsquery 再 ORDER BY ts_rank LIMIT——ts_rank 不含 IDF，
    高频词（不锈钢，数百行同分）会把低频词（保温）的候选挤出窗口，
    导致「不锈钢保温杯」只返回不锈钢。逐词取候选可保证每个词都有代表进入候选集。
    """
    idf = idf_map(country, vec_col)
    cfg = _cfg(vec_col)
    # 命中判定要用与向量对应的原文：英文向量不能再看中文分词列
    if vec_col == "search_vec_en":
        def row_tokens(r: dict[str, Any]) -> set[str]:
            return _ascii_tokens(r.get("description_en"))
    else:
        def row_tokens(r: dict[str, Any]) -> set[str]:
            return set((r.get("_tokens") or "").split())

    if not idf:
        tsq = " | ".join(tokens)
        sql = f"""
            SELECT {_SELECT_COLS}, search_tokens AS _tokens,
                   ts_rank({vec_col}, to_tsquery(%s, %s)) AS _rank
            FROM hs_code
            WHERE country = %s AND {vec_col} @@ to_tsquery(%s, %s) {chap_clause}
            ORDER BY _rank DESC LIMIT %s
        """
        rows = conn.execute(
            sql, (cfg, tsq, country, cfg, tsq, *chap_params, limit * 5)
        ).fetchall()
        return _rank(rows, tokens)

    by_code: dict[str, dict[str, Any]] = {}
    for tok in sorted(tokens, key=lambda t: -idf.get(t, 0.0)):
        sql = f"""
            SELECT {_SELECT_COLS}, search_tokens AS _tokens
            FROM hs_code
            WHERE country = %s AND {vec_col} @@ plainto_tsquery(%s, %s)
              {chap_clause}
            ORDER BY length(description) LIMIT %s
        """
        for row in conn.execute(
            sql, (country, cfg, tok, *chap_params, limit * 3)
        ).fetchall():
            by_code.setdefault(row["code"], row)

    if not by_code:
        return []

    qset = set(tokens)
    rows = list(by_code.values())
    for r in rows:
        hit = qset & row_tokens(r)
        r["hit_count"] = len(hit)
        r["_score"] = sum(idf.get(t, 0.0) for t in hit)
        r["_rank"] = 0.0
    rows.sort(
        key=lambda r: (
            -r["_score"],
            -r["hit_count"],
            len(r.get("description") or ""),
            r["code"],
        )
    )
    return rows


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #


def search(
    keyword: str,
    country: str = "CN",
    chapter: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """关键词检索。

    检索阶梯（逐步放宽，先命中先返回）：
      1. 自身品名 FTS(AND)   —— 精度优先，最符合直觉
      2. 含祖先描述 FTS(AND) —— 覆盖 "Live"/"Other" 这类极短的末级品名
      3. 中国数据的英文品名 FTS(AND)
      4. 语义召回（向量近邻）—— 覆盖字面不重叠的口语说法
      5. 上述各自的 FTS(OR)  —— 放宽为任一词命中，按 IDF 加权重排
      6. 中文搜美国：借中国侧的 HS6 锚点映射
      7. 整串 ILIKE 兜底（pg_trgm），覆盖分词未切准的情况

    三套 tsvector 各管一路，都用同一套阶梯和同一套 IDF 排序：
      search_vec      中文分词 / 美国英文
      search_vec_path 含祖先描述
      search_vec_en   中国数据的英文品名（过去这里没索引，只能 ILIKE 子串，
                      词序一换就查不到；现在是真正的 FTS）
    """
    keyword = (keyword or "").strip()
    limit = max(1, min(int(limit or 10), 50))
    empty = {"keyword": keyword, "country": country, "chapter": chapter,
             "match": "none", "count": 0, "results": [], "data_note": DATA_NOTE}
    if not keyword:
        return empty

    tokens = [t for t in (_sanitize_token(t) for t in tokenize(keyword, country)) if t]
    en_tokens = ([t for t in (_sanitize_token(t) for t in tokenize_en(keyword)) if t]
                 if country == "CN" and re.search(r"[a-z]", keyword.lower()) else [])
    chap_clause = " AND hs2 = %s" if chapter else ""
    chap_params: tuple = (re.sub(r"\D", "", chapter).zfill(2),) if chapter else ()

    results: list[dict[str, Any]] = []
    match = "none"

    with connect() as conn:
        if tokens:
            ladder: list[tuple[str, str, str, list[str]]] = [
                ("fts_and", " & ", "search_vec", tokens),
                ("fts_and_path", " & ", "search_vec_path", tokens),
            ]
            if en_tokens:
                ladder.append(("fts_and_en", " & ", "search_vec_en", en_tokens))
            for mode, joiner, vec_col, tk in ladder:
                tsq = joiner.join(tk)
                if not tsq:
                    continue
                sql = f"""
                    SELECT {_SELECT_COLS}, search_tokens AS _tokens,
                           ts_rank({vec_col}, to_tsquery(%s, %s)) AS _rank
                    FROM hs_code
                    WHERE country = %s AND {vec_col} @@ to_tsquery(%s, %s)
                      {chap_clause}
                    -- ts_rank 经常同分（都命中同一个词），加一道长度做稳定排序：
                    -- 短品名通常更精确，也让「Frozen lobsters」压过一堆限定语的长句
                    ORDER BY _rank DESC, length(coalesce(description_en, description))
                    LIMIT %s
                """
                cfg = _cfg(vec_col)
                rows = conn.execute(
                    sql, (cfg, tsq, country, cfg, tsq, *chap_params, limit * 5)
                ).fetchall()
                if rows:
                    results = _rank(rows, tk)
                    match = mode
                    break

        # 语义召回档：AND 都没命中说明字面不匹配，交给向量找语义相近的条目
        if not results:
            rows = vector_recall(conn, keyword, country, chap_clause, chap_params, limit)
            if rows:
                results = rows
                match = "vector"

        # OR 阶梯：逐个 token 取候选（稀有词优先），再按 IDF 加权重排
        if not results:
            or_rungs = [
                ("fts_or", "search_vec", tokens),
                ("fts_or_path", "search_vec_path", tokens),
            ]
            if en_tokens:
                or_rungs.append(("fts_or_en", "search_vec_en", en_tokens))
            for mode, vec_col, tk in or_rungs:
                rows = _or_candidates(conn, tk, vec_col, country,
                                      chap_clause, chap_params, limit)
                if rows:
                    results = rows
                    match = mode
                    break

        # 中文搜美国：先用中国侧的中文 FTS 定位 HS6，再把锚点映射回美国编码。
        # 不需要翻译也不用改写 US 行检索字段（那要重导 dump），查一次 join 就够。
        if not results and country == "US" and re.search(r"[\u4e00-\u9fff]", keyword):
            anchors = _cn_anchors(conn, keyword, limit)
            if anchors:
                sql = f"""
                    SELECT {_SELECT_COLS}, search_tokens AS _tokens, 0::float4 AS _rank
                    FROM hs_code
                    WHERE country = 'US' AND hs6 = ANY(%s) {chap_clause}
                    ORDER BY code_len, code LIMIT %s
                """
                rows = conn.execute(sql, (anchors, *chap_params, limit * 5)).fetchall()
                if rows:
                    pos = {a: i for i, a in enumerate(anchors)}
                    rows.sort(key=lambda r: (pos.get(r["hs6"], 999),
                                             r["code_len"],
                                             _placeholder_desc(r.get("description")),
                                             len(r.get("description") or ""),
                                             r["code"]))
                    for r in rows:
                        r["hit_count"] = 1
                    results = rows
                    match = "zh_via_cn_hs6"

        # 兜底：整串子串匹配（走 pg_trgm 索引）
        if not results:
            sql = f"""
                SELECT {_SELECT_COLS}, search_tokens AS _tokens, 0::float4 AS _rank
                FROM hs_code
                WHERE country = %s
                  AND (description ILIKE %s OR description_en ILIKE %s)
                  {chap_clause}
                LIMIT %s
            """
            like = f"%{keyword}%"
            rows = conn.execute(sql, (country, like, like, *chap_params, limit * 5)).fetchall()
            if rows:
                results = _rank(rows, tokens or [keyword.lower()])
                match = "like"

        if not results:
            return empty

        for r in results:
            r.pop("_tokens", None)
            r.pop("_rank", None)
            r.pop("_score", None)
            clean_row(r)
        attach_zh(conn, results, country)

    return {
        "keyword": keyword,
        "country": country,
        "chapter": chapter,
        "match": match,
        "count": len(results[:limit]),
        "results": results[:limit],
        "data_note": DATA_NOTE,
    }


def get_detail(code: str, country: str = "CN") -> dict[str, Any] | None:
    """按编码取品名、单位与层级详情。"""
    norm = normalize_code(code, country)
    if not norm:
        return None
    with connect() as conn:
        row = conn.execute(
            f"SELECT {_SELECT_COLS} FROM hs_code WHERE country = %s AND code = %s",
            (country, norm),
        ).fetchone()
        if not row:
            return None

        row["chapter_title"] = _chapter_map(conn, country).get(row["hs2"], f"Chapter {row['hs2']}")
        row["hierarchy"] = {
            "hs2": row["hs2"], "hs4": row["hs4"], "hs6": row["hs6"],
            "hs8": row["hs8"], "code": row["code"],
        }

        # 先清洗（定出 en_corrupted），再挂中文别名
        clean_row(row)
        attach_zh(conn, [row], country)
    return row


def list_chapter(
    chapter: str,
    country: str = "CN",
    heading: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """列出某章（可再按品目收窄）下的编码。"""
    ch = re.sub(r"\D", "", str(chapter or "")).zfill(2)
    limit = max(1, min(int(limit or 50), 200))
    clauses = ["country = %s", "hs2 = %s"]
    params: list[Any] = [country, ch]
    if heading:
        clauses.append("hs4 = %s")
        params.append(re.sub(r"\D", "", heading).zfill(4))

    with connect() as conn:
        total = conn.execute(
            f"SELECT count(*) AS n FROM hs_code WHERE {' AND '.join(clauses)}", params
        ).fetchone()["n"]
        rows = conn.execute(
            f"""
            SELECT code, code_display, code_len, description, description_en,
                   unit1, unit2, hs4, hs6, path_desc, is_declarable
            FROM hs_code WHERE {' AND '.join(clauses)}
            ORDER BY code LIMIT %s
            """,
            (*params, limit),
        ).fetchall()
        title = _chapter_map(conn, country).get(ch, f"Chapter {ch}")

        for row in rows:
            clean_row(row)
        attach_zh(conn, rows, country)

    return {
        "country": country,
        "chapter": ch,
        "chapter_title": title,
        "heading": heading,
        "total": total,
        "count": len(rows),
        "codes": rows,
        "data_note": DATA_NOTE,
    }


def compare(code: str, source_country: str = "CN", target_country: str = "US",
            limit: int = 20) -> dict[str, Any] | None:
    """跨市场对照：以 HS6 为锚点，找出目标国的对应编码。"""
    src = get_detail(code, source_country)
    if not src:
        return None
    anchor = src["hs6"]
    limit = max(1, min(int(limit or 20), 50))

    with connect() as conn:
        targets = conn.execute(
            f"""
            SELECT {_SELECT_COLS} FROM hs_code
            WHERE country = %s AND hs6 = %s
            ORDER BY code_len, code LIMIT %s
            """,
            (target_country, anchor, limit),
        ).fetchall()
        titles = _chapter_map(conn, target_country)

        for t in targets:
            t["chapter_title"] = titles.get(t["hs2"], f"Chapter {t['hs2']}")
            clean_row(t)
        attach_zh(conn, targets, target_country)

    return {
        "anchor_hs6": anchor,
        "source": {
            "country": source_country, "code": src["code"],
            "description": src["description"], "path_desc": src.get("path_desc"),
        },
        "target_country": target_country,
        "count": len(targets),
        "targets": targets,
        "caveat": (
            "中美（各国）编码仅在前 6 位（HS6）国际统一，后 2-4 位由各国自行定义启用。"
            "本结果基于 HS6 前缀匹配，说明两者处于同一国际子目之下，"
            "但不构成「等同归类」结论；具体归类请核对目标国品目注释或申请海关预裁定。"
        ),
        "data_note": DATA_NOTE,
    }


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


@mcp.tool()
def search_hs_code(
    keyword: str,
    country: Country = "CN",
    chapter: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """按关键词检索商品编码。

    支持中文（中国数据）与英文（美国数据）。检索策略为「全部词都命中」优先，
    不足时自动放宽为「任一词命中」，因此结果按相关度排序。

    中国数据双语：既可用中文查，也可用英文查自带的英文品名
    （如 "vacuum flask" / "lobster"）。返回的 en_corrupted=true 表示该条英文品名
    被源头数据污染，参考价值低；美国数据的英文品名干净，该标记恒为 false。

    美国数据可用中文查：country="US" + 中文关键词时，先用中国侧的中文检索定位
    HS6 子目，再映射回美国编码（match="zh_via_cn_hs6"）。这不是翻译，而是借同一
    国际子目的中国中文名做锚点，覆盖率约 85%（10 位码 99%）。

    Args:
        keyword: 商品名称关键词，如 "冻螯龙虾"、"stainless steel flask"、"猪肉"。
        country: 编码体系，CN=中国（8 位）、US=美国 HTS（4/6/8/10 位）。
        chapter: 可选，限定章（两位数字字符串），如 "03" 表示第三章。
        limit: 返回条数，1-50，默认 10。
    """
    return search(keyword, country=country, chapter=chapter, limit=limit)


@mcp.tool()
def get_hs_detail(code: str, country: Country = "CN") -> dict[str, Any]:
    """按编码查详情：品名、法定单位、所属章节层级与层级路径。

    Args:
        code: 商品编码。中国为 8 位（如 02011000）；美国可为 4/6/8/10 位
              （如 0101.21.00 或 0101210010）。分隔符会被自动忽略。
        country: 编码体系，CN 或 US。country="US" 时会附 zh_hint：同一 HS6 子目下
                 中国数据的中文品名，用于快速看懂这个美国条目大概指什么。
                 它是「同级参照名」不是译文（zh_is_translation=false），请勿当品名引用。
    """
    detail = get_detail(code, country=country)
    if detail is None:
        return {
            "found": False,
            "code": code,
            "country": country,
            "message": f"未找到该编码。请确认位数与体系（{'中国 8 位' if country == 'CN' else '美国 4/6/8/10 位'}），"
                       "必要时先调用 search_hs_code 检索。",
        }
    detail["found"] = True
    return detail


@mcp.tool()
def list_chapter_codes(
    chapter: str,
    country: Country = "CN",
    heading: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """列出某章（可再按品目收窄）下的编码，用于归类时的逐层浏览。

    归类实践中通常先看整章范围，再收窄到品目，最后定位具体编码——
    本工具即模拟这条路径。

    Args:
        chapter: 章号，两位，如 "03"。
        country: CN 或 US。
        heading: 可选，品目号四位，如 "0306"。
        limit: 返回条数，1-200，默认 50。
    """
    return list_chapter(chapter, country=country, heading=heading, limit=limit)


@mcp.tool()
def compare_across_countries(
    code: str,
    source_country: Country = "CN",
    target_country: Country = "US",
    limit: int = 20,
) -> dict[str, Any]:
    """跨市场编码对照：给一个编码，找出目标国体系下处于同一 HS6 子目的编码。

    典型用法是做美国市场时，从中国编码出发看美国对应条目。

    Args:
        code: 源国编码。
        source_country: 源体系，默认 CN。
        target_country: 目标体系，默认 US。
        limit: 返回条数，1-50，默认 20。
    """
    if source_country == target_country:
        return {"found": False, "message": "源体系与目标体系相同，无需对照。"}
    res = compare(code, source_country=source_country, target_country=target_country,
                  limit=limit)
    if res is None:
        return {"found": False, "code": code, "source_country": source_country,
                "message": "源编码不存在，无法对照。"}
    res["found"] = True
    return res


# --------------------------------------------------------------------------- #
# 资源
# --------------------------------------------------------------------------- #


@mcp.resource("hs://dataset-info")
def dataset_info() -> str:
    """数据版本与覆盖范围说明（供 AI 自查数据边界）。"""
    with connect() as conn:
        counts = {
            r["country"]: r["n"] for r in conn.execute(
                "SELECT country, count(*) AS n FROM hs_code GROUP BY country"
            ).fetchall()
        }
        releases = {
            r["country"]: r for r in conn.execute(
                "SELECT country, system, release, source_url, record_count, imported_at "
                "FROM data_release"
            ).fetchall()
        }
        # 质量数字现算，避免写死后和库里实际状态脱节
        quality = conn.execute(
            """
            SELECT count(*) FILTER (WHERE description LIKE '%?%')    AS bad_desc,
                   count(*) FILTER (WHERE description_en LIKE '%?%') AS bad_en
            FROM hs_code WHERE country = 'CN'
            """
        ).fetchone()

    def rel(country: str, key: str):
        return (releases.get(country) or {}).get(key)

    def when(country: str):
        v = rel(country, "imported_at")
        return v.isoformat(timespec="seconds") if v else None

    return json.dumps({
        "countries": {
            "CN": {
                "system": "中国进出口税则（8 位税则号列）",
                "system_code": rel("CN", "system"),
                "scope": "2015 年基线全量 + 2016-2023 年变更记录",
                "release": rel("CN", "release"),
                "source_url": rel("CN", "source_url"),
                "record_count": rel("CN", "record_count"),
                "imported_at": when("CN"),
                "caveat": "非逐年完整税则：2016-2023 每年仅含发生变更的编码，"
                          "不可当作某年有效编码目录使用",
                "unique_codes": counts.get("CN"),
                "known_issues": [
                    f"中文品名 {quality['bad_desc']} 行含 '?'（源数据转码损坏，无法还原）",
                    f"英文品名 {quality['bad_en']} 行仍含 '?'（无推导依据，未处理）",
                ],
                "missing": ["税率", "监管条件", "申报要素", "10 位报关码"],
            },
            "US": {
                "system": "USITC Harmonized Tariff Schedule (HTS)",
                "system_code": rel("US", "system"),
                "scope": "单版本快照",
                "release": rel("US", "release"),
                "source_url": rel("US", "source_url"),
                "record_count": rel("US", "record_count"),
                "imported_at": when("US"),
                "caveat": "层级由 JSON 的 indent 缩进栈还原；4/6 位为层级节点，"
                          "8 位为法定层，10 位为统计申报层",
                "codes": counts.get("US"),
                "missing": ["税率（本期不处理）"],
            },
        },
        "data_note": DATA_NOTE,
    }, ensure_ascii=False, indent=2)


@mcp.resource("hs://chapters")
def chapters_resource() -> str:
    """中美两国章目录。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT country, chapter, title, code_count FROM hs_chapter "
            "ORDER BY country, chapter"
        ).fetchall()
    return json.dumps(rows, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# 提示词
# --------------------------------------------------------------------------- #


@mcp.prompt()
def hs_classify(description: str, countries: str = "CN") -> str:
    """商品归类工作流提示词（拆解属性 → 定位章 → 检索候选 → 说明理由）。"""
    return (
        f"请为以下商品做归类辅助：{description}\n\n"
        f"目标体系：{countries}\n\n"
        "按以下步骤执行，并在回答中给出每一步的依据：\n"
        "1. 拆解商品属性：材质、功能、用途、加工程度；\n"
        "2. 依据《进出口税则》的编排逻辑推断可能落入的章，可用 list_chapter_codes 浏览该章；\n"
        "3. 用 search_hs_code 检索候选编码，必要时换多个关键词；\n"
        "   注意：口语商品的叫法常常不写在税则品名里，一次检索无果属正常，\n"
        "   应换用材质/用途/加工工艺等词再试，而不是判定「无此编码」；\n"
        "4. 对候选逐个用 get_hs_detail 查看品名与层级，比较差异；\n"
        "5. 给出推荐编码，说明理由，并列出被排除的候选及排除原因；\n"
        "6. 若涉及多国，用 compare_across_countries 给出对应体系下的编码；\n"
        "7. 引用法定单位时注意：中国数据里 unit2 为 null 且 unit_missing=true 表示源数据\n"
        "   未给出第二法定单位（源文件中的占位符），不要当作确定的空值使用。\n\n"
        "重要：归类是专业性判断，结论仅供参考，正式申报前请核对品目注释，"
        "必要时向海关申请预裁定。"
    )


@mcp.prompt()
def hs_cross_market(code: str) -> str:
    """跨市场对照工作流提示词。"""
    return (
        f"请对编码 {code} 做中美跨市场对照：\n"
        "1. 用 get_hs_detail 确认源编码的品名与层级；\n"
        "2. 用 compare_across_countries 取目标体系下同一 HS6 子目的编码；\n"
        "3. 对比双方描述的措辞差异，指出哪些细节（材质/规格/用途）可能影响归类；\n"
        "4. 明确说明：仅前 6 位国际统一，后 2-4 位各自定义，结果不构成等同归类结论。"
    )


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def main() -> None:
    ap = argparse.ArgumentParser(description=f"{SERVER_NAME} MCP server")
    ap.add_argument("--http", type=int, metavar="PORT",
                    help="以 Streamable HTTP 方式运行并监听端口（默认 stdio）")
    ap.add_argument("--host", default="127.0.0.1", help="HTTP 监听地址")
    ap.add_argument("--path", default=os.environ.get("MCP_HTTP_PATH", "/mcp/hscode"),
                    help="HTTP 端点路径")
    ap.add_argument("--stateless", action="store_true",
                    default=os.environ.get("MCP_STATELESS", "").lower() in {"1", "true"},
                    help="HTTP 无状态模式：请求不依赖 Mcp-Session-Id（服务重启后客户端"
                         "不会陷入 'unknown or expired session ID' 404 循环）")
    args = ap.parse_args()

    if args.http:
        path = args.path if args.path.startswith("/") else "/" + args.path
        print(f"[{SERVER_NAME}] HTTP 模式：http://{args.host}:{args.http}{path}"
              f"（{'stateless' if args.stateless else 'stateful'}）", file=sys.stderr)
        mcp.run(transport="streamable-http", host=args.host, port=args.http,
                streamable_http_path=path, stateless_http=args.stateless)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
