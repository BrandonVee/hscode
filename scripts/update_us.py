"""发布前人工导入美国 HTS 官方文件；须指定文件和版本，默认仅预览。"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re

from sqlalchemy import delete, func, insert, select, update as sql_update
from sqlalchemy.exc import SQLAlchemyError

from hscode.config import DEFAULT_DSN
from hscode.database import maintenance_session
from hscode.models import DataRelease, HSChapter, HSCode
from hscode.repository import rows as query_rows
from hscode.postgres import lock_tables
from hscode.server import tokenize_en

COLUMNS = ("country", "code", "code_display", "code_len", "description", "description_en",
           "unit1", "unit2", "hs2", "hs4", "hs6", "hs8", "indent", "path_desc",
           "is_declarable", "search_tokens", "search_tokens_path")
COL_SQL = ", ".join(COLUMNS)


def release_key(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(20\d{2}) (?:Rev (\d+)|Basic)", value)
    if not match:
        raise ValueError(f"无法识别版本：{value}；格式如 2026 Rev 21 或 2027 Basic")
    return int(match[1]), int(match[2] or 0)


def source_url(release: str) -> str:
    year, revision = release_key(release)
    suffix = f"revision_{revision}" if revision else "basic"
    return f"https://www.usitc.gov/sites/default/files/tata/hts/hts_{year}_{suffix}_json.json"


def plain(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("描述必须为字符串")
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def parse_rows(payload: object) -> list[dict]:
    if not isinstance(payload, list) or not payload:
        raise ValueError("官方 JSON 必须是非空列表")
    rows = []
    seen = set()
    stack: list[tuple[int, str]] = []
    chapter = None
    for item in payload:
        display = item["htsno"].strip()
        code = display.replace(".", "")
        if code and (not re.fullmatch(r"[0-9]+", code) or len(code) not in {4, 6, 8, 10}):
            raise ValueError(f"非法编码：{display}")
        indent = int(item["indent"])
        if not 0 <= indent <= 30:
            raise ValueError(f"非法层级：{indent}")
        description = plain(item["description"])
        if not description:
            raise ValueError("发现空品名")
        if code and code[:2] != chapter:
            stack.clear()
            chapter = code[:2]
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = " > ".join(desc for _, desc in stack)
        # 无编码行仍参与缩进栈，如 Horses:、Of chickens:。
        stack.append((indent, description))
        if not code:
            continue
        if code in seen:
            raise ValueError(f"重复编码：{code}")
        seen.add(code)
        units = item["units"]
        if not isinstance(units, list) or len(units) > 2 or any(not isinstance(u, str) for u in units):
            raise ValueError(f"单位格式变化：{code}")
        rows.append(dict(zip(COLUMNS, (
            "US", code, display, len(code), description, description,
            (units[0].strip() or None) if units else None,
            (units[1].strip() or None) if len(units) > 1 else None,
            code[:2], code[:4], code[:6], code[:8] if len(code) >= 8 else None,
            indent, path or None, len(code) >= 8,
            " ".join(tokenize_en(description)), " ".join(tokenize_en(description + " " + path)),
        ), strict=True)))
    if not 20000 <= len(rows) <= 60000:
        raise ValueError(f"编码数量异常：{len(rows)}；停止更新")
    return rows


def diff(old: list[dict], new: list[dict]) -> dict:
    before = {r["code"]: r for r in old}
    after = {r["code"]: r for r in new}
    added = sorted(after.keys() - before.keys())
    removed = sorted(before.keys() - after.keys())
    changed = sorted(code for code in before.keys() & after.keys()
                     if any(before[code].get(k) != after[code].get(k) for k in COLUMNS))
    return {"added": len(added), "removed": len(removed), "changed": len(changed),
            "added_codes": added, "removed_codes": removed, "changed_codes": changed}


def save_json(path: Path, value: object) -> None:
    # 独占创建，避免覆盖已有回滚快照。
    with path.open("x") as handle:
        json.dump(value, handle, ensure_ascii=False, default=str)
        handle.flush()
        os.fsync(handle.fileno())


def update(dsn: str, release: str, raw: bytes, *, apply: bool, directory: Path) -> dict:
    rows = parse_rows(json.loads(raw))
    with maintenance_session(dsn) as conn:
        conn.execute(select(func.pg_advisory_xact_lock(func.hashtext("hscode.update_us"))))
        if apply:
            lock_tables(conn.connection(), HSCode.__table__, HSChapter.__table__, DataRelease.__table__)
        old = query_rows(conn, select(*(getattr(HSCode, name) for name in COLUMNS))
                         .where(HSCode.country == "US").order_by(HSCode.code))
        releases = query_rows(conn, select(*DataRelease.__table__.c).where(DataRelease.country == "US"))
        chapters = query_rows(conn, select(*HSChapter.__table__.c)
                              .where(HSChapter.country == "US").order_by(HSChapter.chapter))
        current = releases[0]["release"] if releases else None
        if len(releases) > 1:
            raise ValueError("美国版本元数据不唯一")
        if current and release_key(release) < release_key(current):
            raise ValueError(f"拒绝版本降级：{current} → {release}")
        counts = Counter(r["hs2"] for r in rows)
        if set(counts) != {r["chapter"] for r in chapters}:
            raise ValueError("章节覆盖变化，需人工核对章节标题；停止更新")
        if old and not 0.8 <= len(rows) / len(old) <= 1.2:
            raise ValueError("数据量变化超过 20%，停止更新")
        changes = diff(old, rows)
        report = {"current": current, "target": release, "count": len(rows),
                  "sha256": hashlib.sha256(raw).hexdigest(), "source_url": source_url(release),
                  "applied": False, **changes}
        if not apply or (current == release and not any(changes[k] for k in ("added", "removed", "changed"))):
            return report
        directory.mkdir(parents=True, exist_ok=True)
        tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = directory / f"us-{tag}-before.json"
        save_json(backup, {"format": 1, "rows": old, "chapters": chapters, "releases": releases})
        (directory / f"us-{tag}-source.json").write_bytes(raw)
        # 删除与批量插入处于同一事务；生成列由数据库计算，任何失败均整体回滚。
        conn.execute(delete(HSCode).where(HSCode.country == "US"))
        conn.execute(insert(HSCode).execution_options(render_nulls=True), rows)
        for ch, count in counts.items():
            conn.execute(sql_update(HSChapter).where(HSChapter.country == "US", HSChapter.chapter == ch)
                         .values(code_count=count))
        conn.execute(delete(DataRelease).where(DataRelease.country == "US"))
        conn.add(DataRelease(country="US", system="HTSUS", release=release,
                             source_url=source_url(release), record_count=len(rows)))
        conn.flush()
        report.update(applied=True, backup=str(backup.resolve()))
        save_json(directory / f"us-{tag}-report.json", report)
    return report


def restore(dsn: str, path: Path) -> None:
    snapshot = json.loads(path.read_text())
    if snapshot.get("format") != 1 or not snapshot["rows"] or any(
            row["country"] != "US" for key in ("rows", "chapters", "releases") for row in snapshot[key]):
        raise ValueError("非法美国回滚快照")
    with maintenance_session(dsn) as conn:
        conn.execute(select(func.pg_advisory_xact_lock(func.hashtext("hscode.update_us"))))
        lock_tables(conn.connection(), HSCode.__table__, HSChapter.__table__, DataRelease.__table__)
        conn.execute(delete(HSCode).where(HSCode.country == "US"))
        conn.execute(insert(HSCode).execution_options(render_nulls=True),
                     [{key: row[key] for key in COLUMNS} for row in snapshot["rows"]])
        conn.execute(delete(HSChapter).where(HSChapter.country == "US"))
        conn.execute(insert(HSChapter).execution_options(render_nulls=True), snapshot["chapters"])
        conn.execute(delete(DataRelease).where(DataRelease.country == "US"))
        if snapshot["releases"]:
            releases = [{key: row[key] for key in (
                "country", "system", "release", "source_url", "imported_at", "record_count",
            )} for row in snapshot["releases"]]
            for row in releases:
                row["imported_at"] = datetime.fromisoformat(row["imported_at"])
            conn.execute(insert(DataRelease).execution_options(render_nulls=True), releases)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    parser.add_argument("--apply", action="store_true", help="实际更新；默认只预览")
    parser.add_argument("--file", type=Path, help="人工下载的官方 JSON，须同时指定 --release")
    parser.add_argument("--release", help="人工核对的官方版本，如 '2026 Rev 21'")
    parser.add_argument("--backup-dir", type=Path, default=Path("backups/us"))
    parser.add_argument("--restore", type=Path, help="回滚至更新前快照，须同时指定 --apply")
    args = parser.parse_args()
    if not args.dsn:
        parser.error("请在 .env 设置 DATABASE_URL，或显式传入 --dsn")
    try:
        if args.restore:
            if not args.apply or args.file or args.release:
                parser.error("--restore 须搭配 --apply，且不能搭配 --file / --release")
            restore(args.dsn, args.restore)
            print("美国数据已回滚；中国数据未改动")
            return
        if not args.file or not args.release:
            parser.error("人工导入必须同时指定 --file 和 --release")
        release = args.release
        source_url(release)  # 验证版本格式；不会访问网络。
        print(f"人工指定版本：{release}；读取 {args.file}", flush=True)
        raw = args.file.read_bytes()
        report = update(args.dsn, release, raw, apply=args.apply, directory=args.backup_dir)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError, TypeError, SQLAlchemyError) as exc:
        parser.exit(1, f"美国数据更新失败（事务未提交）：{type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
