"""HSCODE_TEST_DATABASE=1 uv run python -m unittest discover -s tests -v

数据库测试只写入独立临时 schema，退出后清理，不修改业务数据。
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from psycopg.conninfo import make_conninfo
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateSchema, DropSchema

from hscode import repository, server
from hscode.config import DEFAULT_DSN
from hscode.database import make_engine
from hscode.export_database import copy_base_codes, export_columns
from hscode.models import DataRelease, HSChapter, HSCode
from scripts import update_us


def code(country, value, description, tokens, description_en=None, path_tokens=None):
    return dict(zip(update_us.COLUMNS, (
        country, value, value, len(value), description, description_en,
        "kg", "?" if country == "CN" else None,
        value[:2], value[:4], value[:6], value[:8], None, None, True,
        tokens, path_tokens or tokens,
    ), strict=True))


@unittest.skipUnless(os.environ.get("HSCODE_TEST_DATABASE") == "1", "需要 HSCODE_TEST_DATABASE=1")
class ORMDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.schema = "test_orm_" + uuid.uuid4().hex
        self.admin = make_engine(DEFAULT_DSN)
        with self.admin.begin() as conn:
            conn.execute(CreateSchema(self.schema))
        self.dsn = make_conninfo(DEFAULT_DSN, options=f"-c search_path={self.schema},public")
        self.engine = make_engine(self.dsn)
        self.addCleanup(self.cleanup_database)
        with self.engine.begin() as conn:
            # 基础表不包含可选向量，生成列由 PostgreSQL 负责计算。
            conn.execute(text("CREATE TABLE hs_code (LIKE public.hs_code INCLUDING GENERATED)"))
            conn.execute(text("ALTER TABLE hs_code DROP COLUMN IF EXISTS embedding"))
            conn.execute(text("ALTER TABLE hs_code ADD PRIMARY KEY (country, code)"))
            HSChapter.__table__.create(conn)
            DataRelease.__table__.create(conn)
        self.cn = code("CN", "03061200", "冻螯龙虾", "冻 螯 龙虾", "Frozen lobsters")
        self.us = [
            code("US", "0306120010", "Frozen lobsters", "frozen lobsters", "Frozen lobsters"),
            code("US", "0306120090", "Other", "", "Other", "frozen lobsters"),
        ]
        with Session(self.engine) as conn, conn.begin():
            conn.execute(insert(HSCode).execution_options(render_nulls=True), [self.cn, *self.us])
            conn.add_all([HSChapter(country=c, chapter="03", title="Fish", code_count=n)
                          for c, n in [("CN", 1), ("US", 2)]])
            conn.add_all([DataRelease(country=c, system=c, release="2026 Rev 19", record_count=n)
                          for c, n in [("CN", 1), ("US", 2)]])
        server._idf_cache.clear()
        self.addCleanup(server._idf_cache.clear)

    def cleanup_database(self):
        self.engine.dispose()
        with self.admin.begin() as conn:
            conn.execute(DropSchema(self.schema, cascade=True))
        self.admin.dispose()

    def snapshot(self):
        with Session(self.engine) as conn:
            codes = repository.rows(conn, select(*(getattr(HSCode, k) for k in update_us.COLUMNS))
                                    .order_by(HSCode.country, HSCode.code))
            chapters = repository.chapters(conn)
            _, releases, _ = repository.dataset_summary(conn)
            return codes, chapters, releases

    def test_search_details_and_resources_without_vector_column(self):
        with patch.object(server, "connect", side_effect=lambda: Session(self.engine)), \
                patch.object(server, "embed_model", return_value=None):
            for keyword, country, mode in [
                ("龙虾", "CN", "fts_and"), ("lobster", "CN", "fts_and_en"),
                ("lobsters", "US", "fts_and"), ("龙虾", "US", "zh_via_cn_hs6"),
                ("龙虾 不存在", "CN", "fts_or"), ("Other", "US", "like"),
            ]:
                with self.subTest(keyword=keyword, country=country):
                    result = server.search(keyword, country, chapter="03")
                    self.assertEqual(result["match"], mode)
                    self.assertGreater(result["count"], 0)
                    self.assertTrue(all(r["hs2"] == "03" for r in result["results"]))
            self.assertEqual(server.search("龙虾", "CN", chapter="04")["count"], 0)
            self.assertEqual(server.search("definitelymissing", "CN")["match"], "none")
            detail = server.get_detail("03061200")
            self.assertIsNone(detail["unit2"])
            self.assertEqual(detail["chapter_title"], "Fish")
            self.assertIsNone(server.get_detail("00000000"))
            self.assertEqual(server.list_chapter("03", "US", "0306")["total"], 2)
            self.assertEqual(server.compare("03061200")["count"], 2)
            self.assertEqual(json.loads(server.dataset_info())["countries"]["CN"]["unique_codes"], 1)
            self.assertEqual(len(json.loads(server.chapters_resource())), 2)
        with Session(self.engine) as conn:
            self.assertFalse(repository.vector_ready(conn))
            self.assertIsNotNone(conn.get(HSCode, ("CN", "03061200")))
            self.assertIsNone(repository.detail(conn, "CN", "' OR 1=1 --"))
            path_hits = repository.full_text(conn, "US", "search_vec_path", "lobsters", 5)
            self.assertEqual(len(path_hits), 2)

    def test_update_failure_idempotence_and_restore(self):
        before = self.snapshot()
        updated = deepcopy(self.us)
        updated[0].update(description="Frozen crustaceans", description_en="Frozen crustaceans",
                          search_tokens="frozen crustaceans", search_tokens_path="frozen crustaceans")
        with tempfile.TemporaryDirectory() as work, patch.object(update_us, "parse_rows", return_value=updated):
            options = dict(dsn=self.dsn, release="2026 Rev 21", raw=b"[]", directory=Path(work))
            self.assertFalse(update_us.update(**options, apply=False)["applied"])
            self.assertEqual(self.snapshot(), before)
            with self.engine.begin() as conn:
                conn.execute(text("CREATE FUNCTION reject_update() RETURNS trigger LANGUAGE plpgsql AS $$ "
                                  "BEGIN RAISE EXCEPTION 'test failure'; END $$"))
                conn.execute(text("CREATE TRIGGER reject_update BEFORE INSERT ON hs_code "
                                  "FOR EACH STATEMENT EXECUTE FUNCTION reject_update()"))
            with self.assertRaises(SQLAlchemyError):
                update_us.update(**options, apply=True)
            self.assertEqual(self.snapshot(), before)
            with self.engine.begin() as conn:
                conn.execute(text("DROP TRIGGER reject_update ON hs_code"))
            applied = update_us.update(**options, apply=True)
            self.assertTrue(applied["applied"])
            self.assertFalse(update_us.update(**options, apply=True)["applied"])
            self.assertEqual(self.snapshot()[0][0], self.cn)
            with self.assertRaises(ValueError):
                update_us.update(**{**options, "release": "2025 Basic"}, apply=True)
            update_us.restore(self.dsn, Path(applied["backup"]))
            self.assertEqual(self.snapshot(), before)

    def test_export_reflection_generated_columns_and_consistent_snapshot(self):
        stage_schema = "test_export_" + uuid.uuid4().hex
        stage_engine = make_engine(make_conninfo(DEFAULT_DSN, options=f"-c search_path={stage_schema},public"))
        try:
            with self.admin.begin() as conn:
                conn.execute(CreateSchema(stage_schema))
            with stage_engine.begin() as stage:
                stage.execute(text("CREATE TABLE hs_code (LIKE public.hs_code INCLUDING GENERATED)"))
                stage.execute(text("ALTER TABLE hs_code DROP COLUMN IF EXISTS embedding"))
                with self.engine.connect().execution_options(
                    isolation_level="REPEATABLE READ", postgresql_readonly=True,
                ) as source, source.begin():
                    names = export_columns(source)
                    self.assertEqual(set(names), set(update_us.COLUMNS))
                    # 在快照创建后提交另一连接的改动，导出仍须读取原快照。
                    with self.engine.begin() as writer:
                        writer.execute(update(HSCode).where(HSCode.country == "CN")
                                       .values(description="changed after snapshot"))
                    copy_base_codes(source, stage)
            with Session(stage_engine) as conn:
                self.assertEqual(repository.detail(conn, "CN", "03061200")["description"], "冻螯龙虾")
                self.assertEqual(len(repository.full_text(conn, "US", "search_vec_path", "lobsters", 5)), 2)
        finally:
            stage_engine.dispose()
            with self.admin.begin() as conn:
                conn.execute(DropSchema(stage_schema, cascade=True, if_exists=True))
