-- 清理历史追溯和当前查询未使用的数据。执行前先用 pg_dump 备份。
-- 不使用 CASCADE：有未发现的依赖时整个事务回滚。
BEGIN;

-- 品名修复保留在主表，修复依据仍由 hs_en_repair 提供。
UPDATE hs_code AS c
SET description_en = r.en_fixed
FROM hs_en_repair AS r
WHERE c.country = 'CN' AND c.code = r.code
  AND c.description_en IS DISTINCT FROM r.en_fixed;

DROP TABLE hs_code_version;

ALTER TABLE hs_code
    DROP COLUMN first_year,
    DROP COLUMN last_year,
    DROP COLUMN version_count,
    DROP COLUMN name_changed,
    DROP COLUMN has_corruption,
    DROP COLUMN description_en_raw;

ALTER TABLE hs_en_repair DROP COLUMN en_fixed;
ALTER TABLE data_release DROP COLUMN notes;
DROP EXTENSION unaccent;

COMMIT;
