"""从中国品名生成语义向量；按批提交，重复运行可补齐缺失数据。"""
from __future__ import annotations

import argparse
import os
import psycopg
from psycopg.rows import dict_row

from .embeddings import DIMENSION, MODEL_NAME, load_model, vector_literal
from .config import DEFAULT_DSN


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def build_vectors(dsn: str, *, batch_size: int = 64, threads: int = 4,
                  rebuild: bool = False, limit: int | None = None) -> int:
    # 会话锁避免两个构建进程同时改写同一批数据。
    with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
        locked = conn.execute(
            "SELECT pg_try_advisory_lock(hashtext('hscode.build_vectors')) AS locked"
        ).fetchone()["locked"]
        if not locked:
            raise RuntimeError("已有向量生成任务正在运行")
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
        conn.execute(
            f"ALTER TABLE hs_code ADD COLUMN IF NOT EXISTS embedding public.vector({DIMENSION})"
        )
        column_type = conn.execute(
            "SELECT format_type(atttypid, atttypmod) AS type FROM pg_attribute "
            "WHERE attrelid = 'hs_code'::regclass AND attname = 'embedding' AND NOT attisdropped"
        ).fetchone()["type"]
        if column_type != f"vector({DIMENSION})":
            raise RuntimeError(f"embedding 类型不匹配：{column_type}")
        predicate = "" if rebuild else " AND embedding IS NULL"
        pending = conn.execute(
            f"SELECT count(*) AS n FROM hs_code WHERE country = 'CN'{predicate}"
        ).fetchone()["n"]
        target = min(pending, limit) if limit is not None else pending
        print(f"模型 {MODEL_NAME}，维度 {DIMENSION}；待生成 {target} 条", flush=True)
        generated = 0
        if target:
            model = load_model(threads=threads)
            last_code = ""
            while generated < target:
                rows = conn.execute(
                    f"SELECT code, description FROM hs_code "
                    f"WHERE country = 'CN' AND code > %s{predicate} ORDER BY code LIMIT %s",
                    (last_code, min(batch_size, target - generated)),
                ).fetchall()
                if not rows:
                    break
                vectors = list(model.embed(
                    [row["description"] for row in rows], batch_size=batch_size
                ))
                updates = [(vector_literal(vector), row["code"], row["description"])
                           for row, vector in zip(rows, vectors, strict=True)]
                with conn.transaction():
                    with conn.cursor() as cursor:
                        # 推理期间品名被编辑时，回滚本批，避免写入过时向量。
                        cursor.executemany(
                            "UPDATE hs_code SET embedding = %s::vector "
                            "WHERE country = 'CN' AND code = %s AND description = %s",
                            updates,
                        )
                        if cursor.rowcount != len(rows):
                            raise RuntimeError("品名在生成期间发生变化，请重新运行")
                generated += len(rows)
                last_code = rows[-1]["code"]
                print(f"已生成 {generated}/{target}", flush=True)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_hs_code_embedding ON hs_code "
            "USING hnsw (embedding public.vector_cosine_ops) "
            "WITH (m = 16, ef_construction = 64)"
        )
        conn.execute("ANALYZE hs_code")
        print(f"完成，本次写入 {generated} 条", flush=True)
        return generated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("HSCode_DSN", DEFAULT_DSN))
    parser.add_argument("--batch-size", type=positive_int, default=64)
    parser.add_argument("--threads", type=positive_int, default=4)
    parser.add_argument("--rebuild", action="store_true", help="重新生成全部中国品名向量")
    parser.add_argument("--limit", type=positive_int, help="本次最多生成条数，用于分批验证")
    args = parser.parse_args()
    build_vectors(args.dsn, batch_size=args.batch_size, threads=args.threads,
                  rebuild=args.rebuild, limit=args.limit)


if __name__ == "__main__":
    main()
