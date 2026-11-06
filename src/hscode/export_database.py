"""导出基础数据库；向量字段、索引与扩展由 hscode-build-vectors 创建。"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from sqlalchemy import MetaData, Table, func, inspect, select

from .config import DEFAULT_DSN
from .database import make_engine
from .postgres import create_database, drop_database, remove_vector_schema


def export_columns(connection) -> list[str]:
    """反射实际字段，排除数据库生成列和可选语义向量。"""
    return [column["name"] for column in inspect(connection).get_columns("hs_code")
            if not column.get("computed") and column["name"] != "embedding"]


def copy_base_codes(source, stage) -> None:
    """在源快照中分批读取、写入基础数据，不加载整表到内存。"""
    names = export_columns(source)
    table = Table("hs_code", MetaData(), autoload_with=source)
    statement = select(*(table.c[name] for name in names)).execution_options(yield_per=1000)
    with source.execute(statement) as result:
        for batch in result.mappings().partitions(1000):
            stage.execute(table.insert(), [dict(row) for row in batch])


def run_pg(tool: str, args: list[str], dsn: str, *, database: str,
           container: str | None, stdin=None, stdout=None) -> None:
    settings = conninfo_to_dict(dsn)
    env = os.environ.copy()
    values = {
        "PGDATABASE": database,
        "PGUSER": settings.get("user", env.get("PGUSER", "")),
        "PGHOST": "127.0.0.1" if container else settings.get("host", env.get("PGHOST", "")),
        "PGPORT": "5432" if container else settings.get("port", env.get("PGPORT", "5432")),
        "PGPASSWORD": settings.get("password", env.get("PGPASSWORD", "")),
    }
    env.update(values)
    command = [tool, *args]
    if container:
        flags = [flag for key in values for flag in ("--env", key)]
        command = ["docker", "exec", "-i", *flags, container, *command]
    subprocess.run(command, env=env, stdin=stdin, stdout=stdout, check=True)


def export_database(dsn: str, output: Path, *, container: str | None = None) -> None:
    stage_name = "hscode_export_" + uuid.uuid4().hex[:12]
    stage_dsn = make_conninfo(dsn, dbname=stage_name)
    output.mkdir(parents=True, exist_ok=True)
    engine = make_engine(dsn)
    stage_engine = make_engine(stage_dsn)
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as admin:
            create_database(admin, stage_name)
            try:
                _export_database(engine, stage_engine, dsn, stage_name, output, container)
            finally:
                stage_engine.dispose()
                drop_database(admin, stage_name)
    finally:
        stage_engine.dispose()
        engine.dispose()


def _export_database(engine, stage_engine, dsn: str, stage_name: str,
                     output: Path, container: str | None) -> None:
    with tempfile.TemporaryDirectory(prefix="hscode_export_") as directory:
        work = Path(directory)
        with engine.connect().execution_options(
            isolation_level="REPEATABLE READ", postgresql_readonly=True,
        ) as source, source.begin():
            snapshot = source.scalar(select(func.pg_export_snapshot()))
            database = source.scalar(select(func.current_database()))
            with (work / "schema.dump").open("wb") as handle:
                run_pg("pg_dump", ["--format=custom", "--no-owner", "--no-privileges",
                       "--exclude-table-data=public.hs_code", f"--snapshot={snapshot}"],
                       dsn, database=database, container=container, stdout=handle)
            with (work / "schema.dump").open("rb") as handle:
                run_pg("pg_restore", ["--dbname=" + stage_name, "--exit-on-error",
                       "--no-owner", "--no-privileges"], dsn, database=stage_name,
                       container=container, stdin=handle)
            # 在同一快照中读取基础数据，源库的现有向量不发生变动。
            with stage_engine.begin() as stage:
                remove_vector_schema(stage)
                copy_base_codes(source, stage)
        # 两种格式都成功生成后，才替换输出文件。
        for name, format_name in (("hscode_dump.sql", "plain"), ("hscode_dump.dump", "custom")):
            with (work / name).open("wb") as handle:
                run_pg("pg_dump", [f"--format={format_name}", "--no-owner", "--no-privileges"],
                       dsn, database=stage_name, container=container, stdout=handle)
        for name in ("hscode_dump.sql", "hscode_dump.dump"):
            with tempfile.NamedTemporaryFile(dir=output, delete=False) as handle:
                pending = Path(handle.name)
                try:
                    with (work / name).open("rb") as source_file:
                        shutil.copyfileobj(source_file, handle)
                except BaseException:
                    pending.unlink(missing_ok=True)
                    raise
            # PostgreSQL 初始化进程使用独立用户，需要能够读取基础导出。
            pending.chmod(0o644)
            pending.replace(output / name)
        print(f"已导出基础数据：{output / 'hscode_dump.sql'}、{output / 'hscode_dump.dump'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    parser.add_argument("--output-dir", type=Path, default=Path("data"))
    parser.add_argument("--container", help="同一 PostgreSQL 实例的 Docker 容器名；省略时使用本机 PG 工具")
    args = parser.parse_args()
    if not args.dsn:
        parser.error("请在 .env 设置 DATABASE_URL，或显式传入 --dsn")
    export_database(args.dsn, args.output_dir, container=args.container)


if __name__ == "__main__":
    main()
