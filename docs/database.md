# 数据库数据字典

本文说明项目 `public` schema 下的业务表、字段及维护规则，依据基础 SQL、查询代码、人工导入脚本和本机数据库结构核对。核对日期：2026-10-19。

## 1. 表概览与关联

| 表 | 用途 | 主键 | 当前数据 |
| --- | --- | --- | --- |
| `hs_code` | 各国商品编码、品名、单位、层级与检索字段 | `(country, code)` | 中国 9,579 条；美国 29,861 条 |
| `hs_chapter` | 各国章节目录、章标题和收录数量 | `(country, chapter)` | 中国、美国各 98 条 |
| `data_release` | 当前导入数据的版本与来源 | `id` | 中国、美国各 1 条 |

上述数量描述当前快照，后续人工发布可能改变。各国章节是已收录的目录，不代表 01–99 每章都有数据。

逻辑关联如下，数据库**没有建立外键**：

- `hs_code.country = hs_chapter.country` 且 `hs_code.hs2 = hs_chapter.chapter`：查找编码所属章标题。
- `hs_code.country = data_release.country`：查找该国数据的来源和版本。
- 中美编码通过相同 `hs6` 查找同一子目下的候选。还需核对双方 HS 版本，不能将此前缀匹配视为等同归类。

`country` 当前约定为 `CN`（中国）和 `US`（美国）。数据库列为普通 `text`，没有枚举或 CHECK 约束。国家、位数和前缀等业务规则主要由导入及查询程序维护。

## 2. hs_code：商品编码主表

每行代表一个国家体系中的一个编码。编码以字符串存储，保留前导零；不要转换成整数。同一串编码可以分别出现在不同国家，因此必须结合 `country` 使用。

表中“可空”指数据库是否允许 SQL `NULL`，不表示业务一定存在缺失数据。除特别注明外，字段没有默认值。

| 字段 | PostgreSQL 类型 | 可空 | 含义、示例及生成方式 |
| --- | --- | --- | --- |
| `country` | `text` | 否 | 编码所属国家体系，当前为 `CN` / `US`；联合主键的一部分。 |
| `code` | `text` | 否 | 无分隔符编码，如 `03061200`、`0101210010`；联合主键的一部分。 |
| `code_display` | `text` | 是 | 展示用编码；美国通常保留点号，如 `0101.21.00.10`。连接和匹配应使用 `code`。 |
| `code_len` | `smallint` | 否 | `code` 的位数。当前中国均为 8 位，美国为 4 / 6 / 8 / 10 位。它是导入字段，不是数据库生成列。 |
| `description` | `text` | 否 | 当前行的原语种品名：中国为中文，美国为英文。美国某些行仅为 `Other` 等短描述，需结合祖先路径理解。 |
| `description_en` | `text` | 是 | 英文品名。中国保存已有英文及修复结果；美国导入时与 `description` 相同。含 `?` 的内容可能有源数据质量问题。 |
| `unit1` | `text` | 是 | 第一计量单位：中国为源数据的第一法定单位；美国取官方 JSON `units` 第 1 项，如 `kg`、`No.`。保留来源单位表达，不统一换算。 |
| `unit2` | `text` | 是 | 第二计量单位，规则同上，缺失可为 `NULL`。中国源数据部分记录以 `?` 占位，不能将它当成真实单位。 |
| `hs2` | `text` | 否 | 编码前 2 位，所属章，如 `03`。用于章筛选和关联 `hs_chapter`。 |
| `hs4` | `text` | 否 | 编码前 4 位，所属品目，如 `0306`。 |
| `hs6` | `text` | 否 | 编码前 6 位，用作跨国对照锚点，如 `030612`。美国 4 位节点在此字段中只有 4 位，不能把每个非空值都当作完整 HS6。 |
| `hs8` | `text` | 是 | 编码前 8 位。美国编码不足 8 位时为 `NULL`；中国当前等于其 8 位 `code`。 |
| `indent` | `smallint` | 是 | 美国官方数据的缩进深度，用于恢复描述层级。它不等于编码位数或固定层级编号。中国当前为 `NULL`。 |
| `path_desc` | `text` | 是 | 美国当前行的祖先描述，使用 ` > ` 连接，不包含当前行品名。包含官方无编码的中间描述行；根节点及中国当前数据可为 `NULL`。 |
| `is_declarable` | `boolean` | 是 | 项目内部的编码层级标记，默认 `false`。当前中国记录均为 `true`；美国导入按位数 ≥ 8 标为 `true`，4 / 6 位节点为 `false`。**不表示该编码当前可直接用于实际申报**，也不代表 8 位码就是最终申报码。 |
| `search_tokens` | `text` | 是 | 自身品名预处理后的检索词，以空格分隔。中国为中文分词，美国为英文分词并去停用词。修改品名时需要同步维护此列。 |
| `search_vec` | `tsvector` | 是 | 基于 `search_tokens` 自动生成的全文检索列，使用 PostgreSQL `simple` 配置；不可手工指定普通值写入。 |
| `search_tokens_path` | `text` | 是 | 包含祖先描述的检索词文本。美国导入时对“当前品名 + 祖先路径”分词，用于搜索 `Other` 等自身描述信息不足的节点。 |
| `search_vec_path` | `tsvector` | 是 | 基于 `search_tokens_path` 自动生成，使用 `simple` 配置；供包含祖先描述的全文检索使用。 |
| `search_vec_en` | `tsvector` | 是 | 基于 `description_en` 自动生成，使用 `english` 配置，支持英文词干处理；当前查询主要将其用于中国数据的英文品名检索。 |

### 全文检索生成列

三个字段均为 `GENERATED ALWAYS ... STORED`，数据库自动计算并保存：

```sql
search_vec      = to_tsvector('simple',  coalesce(search_tokens, ''))
search_vec_path = to_tsvector('simple',  coalesce(search_tokens_path, ''))
search_vec_en   = to_tsvector('english', coalesce(description_en, ''))
```

以上为简化表达。`search_tokens` 和 `search_tokens_path` 本身不是生成列，不能只修改品名而忽略分词文本。数据库没有自动重新分词的触发器。

### 可选向量字段

| 字段 | 类型 | 可空 | 含义 |
| --- | --- | --- | --- |
| `embedding` | `vector(512)` | 是 | 中国品名的语义向量，由 `hscode-build-vectors` 使用 `BAAI/bge-small-zh-v1.5` 生成。美国不生成向量。 |

`embedding` 不在基础 SQL 中，由向量构建脚本按需添加；依赖 `pgvector` 扩展。当前共享数据库未启用此功能，因此没有该列，服务使用全文检索。基础导出会排除此列及相应向量索引、扩展。

`tsvector` 是全文检索结构，`vector(512)` 是模型语义向量，两者用途不同。品名修改后，语义向量也需要人工重建；它不会随品名自动刷新。

### 层级示例

| 国家 | code | hs2 | hs4 | hs6 | hs8 | indent |
| --- | --- | --- | --- | --- | --- | --- |
| CN | `03061200` | `03` | `0306` | `030612` | `03061200` | `NULL` |
| US | `0101` | `01` | `0101` | `0101` | `NULL` | `0` |
| US | `01012100` | `01` | `0101` | `010121` | `01012100` | `2` |
| US | `0101210010` | `01` | `0101` | `010121` | `01012100` | `3` |

美国 `01012100` 的祖先路径为 `Live horses, asses, mules and hinnies: > Horses:`。其中 `Horses:` 本身没有编码，参与路径构建，但不会作为独立记录进入 `hs_code`。

## 3. hs_chapter：章节目录表

每行代表一个国家体系的一章。服务用它提供章目录、章标题，以及编码详情中的 `chapter_title`。

| 字段 | PostgreSQL 类型 | 可空 | 含义及示例 |
| --- | --- | --- | --- |
| `country` | `text` | 否 | `CN` / `US`，联合主键的一部分。 |
| `chapter` | `text` | 否 | 两位章号，如 `03`，联合主键的一部分；关联主表 `hs2`。 |
| `title` | `text` | 否 | 该章标题，中国为中文，美国为英文，如美国 `01` 章为 `Live animals`。 |
| `code_count` | `integer` | 是 | 当前该国该章在 `hs_code` 中收录的行数。包含不同位数节点，不只统计可申报标记为真的记录。 |

这些字段没有默认值。`code_count` 是保存的统计值，没有计数触发器；人工直接改主表可能使其失真。美国维护脚本在导入时重新计算各章计数，章节集合变化则停止并要求人工核对，不能自动新增章标题。

## 4. data_release：数据版本与来源表

记录本项目当前加载的数据快照，不是逐条编码的历史版本表。查询服务通过它生成 `hs://dataset-info` 的版本信息。

| 字段 | PostgreSQL 类型 | 可空 | 含义、示例及默认值 |
| --- | --- | --- | --- |
| `id` | `integer` | 否 | 主键；默认通过 `data_release_id_seq` 序列生成。它不是业务版本号，更新或回滚后可能改变。 |
| `country` | `text` | 否 | 对应国家 `CN` / `US`。当前维护约定每国一条，但数据库没有对 `country` 建唯一约束。 |
| `system` | `text` | 否 | 数据体系标识：中国 `CN_TARIFF`，美国 `HTSUS`。 |
| `release` | `text` | 是 | 来源版本说明，如 `2026 Rev 21` 或 `2015 基线 + 2016-2023 变更集`。 |
| `source_url` | `text` | 是 | 来源地址。虽然字段名为 URL，也可能存放历史导入所用的本地文件路径；不是所有值都能作为网页打开。 |
| `imported_at` | `timestamp with time zone` | 否 | 导入时间，默认 `now()`；**不是官方发布日期、生效日期或该编码的有效期起点**。 |
| `record_count` | `integer` | 是 | 来源导入记录数或维护脚本写入的记录数，不保证始终等于主表行数。当前中国为 11,988（来源导入口径），主表去重合并后为 9,579；美国为 29,861，与当前主表一致。 |

除上述自增主键及导入时间外，其他字段没有默认值。美国人工更新会替换该国版本元数据。不要在同一国家下手工插入多条版本记录：当前服务按每国一个快照使用，维护脚本也会拒绝美国多条元数据的情况。

## 5. 索引与扩展

除三张表各自的主键索引外，`hs_code` 有以下索引：

| 索引 | 类型 / 字段 | 用途 |
| --- | --- | --- |
| `idx_hs_code_country_hs2` | B-tree `(country, hs2)` | 按国家和章筛选。 |
| `idx_hs_code_country_hs4` | B-tree `(country, hs4)` | 按国家和品目筛选。 |
| `idx_hs_code_hs6` | B-tree `(hs6)` | 跨市场同子目查询、美国条目附中文参照名。 |
| `idx_hs_code_fts` | GIN `(search_vec)` | 自身品名全文检索。 |
| `idx_hs_code_fts_path` | GIN `(search_vec_path)` | 含祖先描述的全文检索。 |
| `idx_hs_code_fts_en` | GIN `(search_vec_en)` | 英文品名全文检索。 |
| `idx_hs_code_trgm` | GIN `(description gin_trgm_ops)` | 品名 `ILIKE` 子串匹配。 |
| `idx_hs_code_desc_en` | GIN `(description_en gin_trgm_ops)` | 英文品名 `ILIKE` 子串匹配。 |
| `idx_hs_code_embedding`（可选） | HNSW `(embedding vector_cosine_ops)` | 按余弦距离进行语义召回；向量构建脚本创建。 |

基础库依赖 PostgreSQL `pg_trgm` 扩展支持三元组索引。`vector` 扩展只在启用可选向量功能时需要。`data_release_id_seq` 是自增序列，不是额外业务表。

## 6. 数据库字段与 MCP 返回字段的区别

以下内容由服务查询时计算或附加，不是物理表字段：

| 返回字段 | 来源和含义 |
| --- | --- |
| `unit_missing` | 清洗后 `unit2` 是否为 `NULL`；不表示第一、第二单位均缺失，也不能确定源数据是否原本没有第二单位。 |
| `en_corrupted` | `description_en` 是否含 `?`；是简单质量提示，不是完整的翻译质量校验。 |
| `chapter_title` | 从 `hs_chapter` 查询得到的章标题。 |
| `hierarchy` | 从 `hs2`、`hs4`、`hs6`、`hs8` 和 `code` 组合出的层级对象。 |
| `zh_hint`、`zh_source`、`zh_is_translation` | 美国条目按相同 HS6 借用中国品名作为参照，明确标记非翻译；不是美国原文的中文译名。 |
| `hit_count`、`similarity` | 查询时计算的命中词数或向量相似度；并非最终归类置信度。 |
| `found`、`match`、`count`、`data_note` | 工具响应中的查询状态、匹配方式、返回数量和数据范围说明。 |

单位清洗仅发生在返回结果时：数据库中为 `?` / `？` 的单位会被返回为 `null`，不回写数据库。

## 7. 常用只读查询

查看各国当前实际收录数量和版本：

```sql
SELECT country, count(*) AS actual_count
FROM hs_code GROUP BY country ORDER BY country;

SELECT country, system, release, imported_at, record_count
FROM data_release ORDER BY country;
```

查看编码及其所属章标题（始终带国家条件）：

```sql
SELECT c.country, c.code, c.description, ch.title AS chapter_title
FROM hs_code AS c
LEFT JOIN hs_chapter AS ch
  ON ch.country = c.country AND ch.chapter = c.hs2
WHERE c.country = 'CN' AND c.code = '03061200';
```

核对已收录章节的保存计数：

```sql
SELECT ch.country, ch.chapter, ch.code_count AS stored_count,
       count(c.code) AS actual_count
FROM hs_chapter AS ch
LEFT JOIN hs_code AS c
  ON c.country = ch.country AND c.hs2 = ch.chapter
GROUP BY ch.country, ch.chapter, ch.code_count
HAVING ch.code_count IS DISTINCT FROM count(c.code);
```

## 8. 维护边界与代码入口

服务使用固定快照，不会自动更新编码数据。人工维护、验证、导出及发布流程见 [README](../README.md#美国-hts-人工维护与发布)。修改数据后应同步发布基础快照并重启服务，刷新进程内词频缓存。

中国目录合并自 2015 年基线及 2016–2023 年变更集，不能作为任一年份的完整有效编码目录。美国当前快照为 `2026 Rev 21`。项目不提供税率、监管条件、申报要素或逐编码的生效 / 失效日期，也不保存完整历史版本；`is_declarable` 等内部标记不能替代正式申报核验。

| 文件 | 用途 |
| --- | --- |
| [基础 SQL](../data/hscode_dump.sql) | 表结构、主键、索引与基础数据；不含语义向量。 |
| [查询服务](../src/hscode/server.py) | 检索、详情、章节目录、跨国对照及返回字段清洗。 |
| [美国人工维护脚本](../scripts/update_us.py) | 从人工指定文件生成美国字段、维护章计数和版本元数据。 |
| [向量构建脚本](../src/hscode/build_vectors.py) | 添加并生成可选 `embedding` 字段和 HNSW 索引。 |
| [导出脚本](../src/hscode/export_database.py) | 导出不含语义向量的基础快照。 |
| [历史清理迁移](../migrations/) | 旧库删除历史表和修复元数据的迁移；新导入库无需执行。 |
