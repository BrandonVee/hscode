"""从中国品名生成语义向量；按批提交，重复运行可补齐缺失数据。"""
from __future__ import annotations

import argparse
import time
from sqlalchemy import bindparam, func, select, update
from sqlalchemy.orm import Session

from .embeddings import DIMENSION, MODEL_NAME, load_model, vector_values
from .database import make_engine
from .models import HSCode
from .repository import rows as query_rows
from .postgres import ensure_vector_column, ensure_vector_index
from .config import DEFAULT_DSN


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def build_vectors(dsn: str, *, batch_size: int = 64, threads: int = 4,
                  rebuild: bool = False, limit: int | None = None) -> int:
    # 独占连接贯穿任务；会话锁跨批次提交保留，最终关闭连接释放。
    engine = make_engine(dsn)
    try:
        with engine.connect() as connection, Session(connection) as conn:
            return _build_vectors(conn, batch_size=batch_size, threads=threads,
                                  rebuild=rebuild, limit=limit)
    finally:
        engine.dispose()


def _build_vectors(conn: Session, *, batch_size: int, threads: int,
                   rebuild: bool, limit: int | None) -> int:
    locked = conn.scalar(select(func.pg_try_advisory_lock(func.hashtext("hscode.build_vectors"))))
    if not locked:
        raise RuntimeError("已有向量生成任务正在运行")
    ensure_vector_column(conn.connection())
    conditions = [HSCode.country == "CN"]
    if not rebuild:
        conditions.append(HSCode.embedding.is_(None))
    pending = conn.scalar(select(func.count()).select_from(HSCode).where(*conditions))
    conn.commit()
    target = min(pending, limit) if limit is not None else pending
    print(f"模型 {MODEL_NAME}，维度 {DIMENSION}；待生成 {target} 条", flush=True)
    generated = 0
    if target:
        started = time.perf_counter()
        print("准备模型：读取缓存或首次下载；此时尚未开始生成向量", flush=True)
        try:
            model = load_model(threads=threads)
        except Exception as exc:
            raise RuntimeError(
                "模型准备失败：请配置可访问的 HF_ENDPOINT，或通过 HS_MODEL_PATH 使用本地模型。"
                f"原始错误：{exc}"
            ) from exc
        print(f"模型已就绪，用时 {time.perf_counter() - started:.1f} 秒；开始生成向量", flush=True)
        last_code = ""
        while generated < target:
            rows = query_rows(conn, select(HSCode.code, HSCode.description)
                              .where(*conditions, HSCode.code > last_code)
                              .order_by(HSCode.code).limit(min(batch_size, target - generated)))
            if not rows:
                break
            vectors = list(model.embed(
                [row["description"] for row in rows], batch_size=batch_size
            ))
            updates = [{"vector": vector_values(vector), "row_code": row["code"], "row_description": row["description"]}
                       for row, vector in zip(rows, vectors, strict=True)]
            # 用 Core 批量表达式附加品名检查；不依赖 ORM 按主键更新的限制。
            result = conn.connection().execute(
                update(HSCode.__table__).where(
                    HSCode.country == "CN", HSCode.code == bindparam("row_code"),
                    HSCode.description == bindparam("row_description"),
                ).values(embedding=bindparam("vector")), updates,
            )
            if result.rowcount != len(rows):
                conn.rollback()
                raise RuntimeError("品名在生成期间发生变化，请重新运行")
            conn.commit()
            generated += len(rows)
            last_code = rows[-1]["code"]
            print(f"已生成 {generated}/{target}", flush=True)
    print("建立或检查 HNSW 索引", flush=True)
    ensure_vector_index(conn.connection())
    conn.commit()
    print(f"完成，本次写入 {generated} 条", flush=True)
    return generated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    parser.add_argument("--batch-size", type=positive_int, default=64)
    parser.add_argument("--threads", type=positive_int, default=4)
    parser.add_argument("--rebuild", action="store_true", help="重新生成全部中国品名向量")
    parser.add_argument("--limit", type=positive_int, help="本次最多生成条数，用于分批验证")
    args = parser.parse_args()
    if not args.dsn:
        parser.error("请在 .env 设置 DATABASE_URL，或显式传入 --dsn")
    try:
        build_vectors(args.dsn, batch_size=args.batch_size, threads=args.threads,
                      rebuild=args.rebuild, limit=args.limit)
    except Exception as exc:
        parser.exit(1, f"向量生成失败：{exc}\n")


if __name__ == "__main__":
    main()
