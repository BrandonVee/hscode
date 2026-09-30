"""导出基础数据库；向量字段、索引与扩展由 hscode-build-vectors 创建。"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from .config import DEFAULT_DSN


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
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(stage_name)))
        try:
            with tempfile.TemporaryDirectory(prefix="hscode_export_") as directory:
                work = Path(directory)
                with psycopg.connect(dsn) as source:
                    source.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                    snapshot = source.execute("SELECT pg_export_snapshot()").fetchone()[0]
                    with (work / "schema.dump").open("wb") as handle:
                        run_pg("pg_dump", ["--format=custom", "--no-owner", "--no-privileges",
                               "--exclude-table-data=public.hs_code", f"--snapshot={snapshot}"],
                               dsn, database=source.info.dbname, container=container, stdout=handle)
                    with (work / "schema.dump").open("rb") as handle:
                        run_pg("pg_restore", ["--dbname=" + stage_name, "--exit-on-error",
                               "--no-owner", "--no-privileges"], dsn, database=stage_name,
                               container=container, stdin=handle)
                    columns = [row[0] for row in source.execute(
                        "SELECT attname FROM pg_attribute WHERE attrelid='hs_code'::regclass "
                        "AND attnum > 0 AND NOT attisdropped AND attgenerated = '' "
                        "AND attname <> 'embedding' ORDER BY attnum"
                    ).fetchall()]
                    column_sql = sql.SQL(", ").join(map(sql.Identifier, columns))
                    # 在同一快照中读取基础数据，源库的现有向量不发生变动。
                    with psycopg.connect(stage_dsn) as stage:
                        stage.execute("ALTER TABLE hs_code DROP COLUMN IF EXISTS embedding")
                        stage.execute("DROP EXTENSION IF EXISTS vector")
                        with source.cursor().copy(sql.SQL("COPY hs_code ({}) TO STDOUT").format(column_sql)) as reader:
                            with stage.cursor().copy(sql.SQL("COPY hs_code ({}) FROM STDIN").format(column_sql)) as writer:
                                for block in reader:
                                    writer.write(block)
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
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(stage_name)))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=os.environ.get("HSCode_DSN", DEFAULT_DSN))
    parser.add_argument("--output-dir", type=Path, default=Path("data"))
    parser.add_argument("--container", help="同一 PostgreSQL 实例的 Docker 容器名；省略时使用本机 PG 工具")
    args = parser.parse_args()
    export_database(args.dsn, args.output_dir, container=args.container)


if __name__ == "__main__":
    main()
