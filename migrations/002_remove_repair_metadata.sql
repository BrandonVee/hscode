-- 接续 001：修复后的英文品名已在 hs_code.description_en 中。
-- 仅删除修复记录表，不改动品名、全文检索字段或语义向量。
BEGIN;
DROP TABLE hs_en_repair;
COMMIT;
