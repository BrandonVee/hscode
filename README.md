# 海关编码查询 MCP

Python MCP 服务，连接已有 PostgreSQL，提供中美海关编码检索、详情、按章浏览和 HS6 对照。项目不再启动独立数据库。

数据库表结构、逐字段含义、关联和索引详见 [数据库数据字典](docs/database.md)；服务端部署见 [部署说明](docs/deployment.md)。

## 数据库准备

项目只连接已有 PostgreSQL，不创建数据库容器。先由管理员创建专用数据库和账号，再配置项目根目录的 `.env`：

```sh
cp .env.example .env   # 只在首次配置时执行，不要覆盖已有 .env
chmod 600 .env
# 编辑 .env，填写 PGHOST、PGPORT、PGDATABASE、PGUSER、PGPASSWORD
```

`.env.example` 只提供无真实密码的配置模板；`.env` 保存实际配置，已排除在 Git 之外。程序仅加载当前运行目录的 `.env`，不读取用户目录里的其他项目配置。

首次将基础数据导入空库：

```sh
uv sync
uv run python - <<'PYTHON'
import subprocess
from hscode.config import DEFAULT_DSN
from pathlib import Path
with Path("data/hscode_dump.sql").open("rb") as source:
    subprocess.run(["psql", "-X", "--dbname=" + DEFAULT_DSN,
                    "-v", "ON_ERROR_STOP=1", "--single-transaction"],
                   stdin=source, check=True)
PYTHON
```

上述命令需要本机安装 PostgreSQL 客户端。导入只执行一次；启动应用不会自动导入或覆盖已有数据。

## 本地运行

```sh
uv sync
uv run hscode                         # stdio
uv run hscode --http 8765 --stateless  # HTTP
```

从当前运行目录的 `.env` 读取 `PGHOST`、`PGPORT`、`PGDATABASE`、`PGUSER`、`PGPASSWORD`。显式设置的环境变量优先；`HSCode_DSN` 可覆盖连接串。请在项目根目录运行命令。

默认 MCP 地址：`http://127.0.0.1:8765/mcp/hscode`，协议为 Streamable HTTP。

## Docker 运行应用

```sh
# 先按上文创建并填写 .env
docker compose up -d --build app
```

Compose 使用 `network_mode: host`（Linux 宿主机网络），只启动应用；从项目 `.env` 注入数据库账号密码，通过 `PGHOST=127.0.0.1` 和 `PGPORT` 直接连接宿主机数据库。不需要共享 Docker 网络，也不做端口映射。数据库必须已就绪；`docker compose down` 不管理数据库。

macOS / Windows 建议使用上文 `uv run hscode` 本机启动方式；Docker Desktop 的宿主机网络需自行启用相应支持。
默认端口只绑定 `127.0.0.1`；远程直连时通过 `.env` 设置 `HSCODE_BIND_HOST=0.0.0.0`，重新创建应用。反向代理保留完整 `/mcp/hscode` 路径。

```sh
docker compose ps -a
docker compose logs -f app
uv run python scripts/smoke_test.py --allow-no-vectors
```

HTTP 健康检查验证 MCP 初始化，不验证数据库查询或向量就绪情况；冒烟脚本通过真实 HTTP 调用查询工具和资源。

## 可选语义检索

当前共享 PostgreSQL 未安装 `pgvector`，默认使用全文检索，不启动向量任务。中文、英文及中文查询美国编码的 HS6 映射仍可用。

如需向量检索，先由共享数据库维护方为 PostgreSQL 18 安装兼容的 `pgvector`，并在 `hscode` 库中启用 `CREATE EXTENSION vector`；应用普通账号不需要超级用户权限。随后运行：

```sh
docker compose run --rm vectors
# 品名修改后重建
docker compose run --rm vectors hscode-build-vectors --rebuild
# 验证语义检索
uv run python scripts/smoke_test.py
```

`vectors` 属于可选 profile，普通 `docker compose up -d` 不会启动它。模型固定为 `BAAI/bge-small-zh-v1.5`（512 维），中国数据共 9,579 条；美国侧使用全文检索。模型与完整向量就绪后查询自动启用语义召回，请求期间不联网下载模型。

模型缓存保存在 `model_cache` 卷。生成按批提交，中断后重跑可补缺；支持 `--batch-size`、`--threads`、`--limit` 和 `--rebuild`。

## 模型下载失败

`httpx.ConnectError: Network is unreachable` 表示模型端点无法连接，尚未开始推理。构建时的 PyPI 镜像不会改变模型下载地址。国内网络可在 `.env` 设置：

```dotenv
HF_ENDPOINT=https://hf-mirror.com
HF_HUB_DISABLE_XET=1
```

然后重新创建使用新环境变量的容器：

```sh
docker compose up -d
docker compose logs -f vectors
# 也可以在前台运行，直接查看准备阶段及失败原因
docker compose run --rm vectors
```

端点配置参考 [HF-Mirror 说明](https://hf-mirror.com/)；缓存和离线环境配置见 [Hugging Face 文档](https://huggingface.co/docs/huggingface_hub/en/package_reference/environment_variables)。

服务器完全无法下载时，可在联网机器下载 [Qdrant/bge-small-zh-v1.5](https://huggingface.co/Qdrant/bge-small-zh-v1.5/tree/main) 的模型目录，上传至项目的 `models/`。目录中应直接包含 `model_optimized.onnx`、`tokenizer.json` 及模型配套 JSON 配置文件，然后使用离线配置启动：

```sh
docker compose -f compose.yaml -f compose.offline.yaml up -d
```

该配置将模型目录只读挂载到 `/opt/model`，生成和查询都跳过网络下载；模型权重不提交到 Git，也不属于数据库导出。

导出脚本使用同一数据快照复制基础字段，通过临时数据库生成两种导出格式，结束后删除临时库；不改动源数据库中的现有向量。运行账户需要创建数据库权限。镜像包含 PostgreSQL 18 导出工具，数据库文件和模型权重不打入应用镜像。

## 美国 HTS 人工维护与发布

服务使用固定数据快照，不检查官方版本、不联网下载编码数据、不自动更新数据库，也不配置定时更新任务。维护脚本位于 `scripts/`，不打入运行镜像。

每次更新由维护者人工检查 [USITC 官方公告](https://www.usitc.gov/harmonized_tariff_information/announcement_archive)，下载对应完整 JSON、核对版本，再按以下流程发布：

```sh
# 1. 预览人工指定文件的差异，不写数据库
uv run python scripts/update_us.py --file /path/hts.json --release '2026 Rev 21'
# 2. 核对差异，在维护窗口备份并导入
uv run python scripts/update_us.py --file /path/hts.json --release '2026 Rev 21' --apply
# 3. 验证查询；随后用有权限的维护账号导出 data/ 基础快照
uv run python scripts/smoke_test.py --allow-no-vectors
# 4. 将数据快照和版本说明纳入发布，并重启服务刷新进程缓存
```

发布到其他环境时，代码升级不会自动替换数据库。维护者需同步导入同版本数据、核对 `hs://dataset-info`，再启动服务并执行冒烟验证；仅更换镜像不会更新数据库。

导入仅涉及美国编码、章节计数和版本元数据，中国数据保持不变。无编码的层级描述仍用于生成祖先路径；不导入税率、附加税和脚注。程序检查重复编码、编码位数、章节覆盖及数据量变化，拒绝版本降级；章节变化或总量变化超过 20% 时需人工核对。

写入前保留 `backups/us/` 下的美国回滚快照、官方原始 JSON 和 SHA-256 差异报告，该目录不提交 Git。暂存和替换在单一数据库事务中完成，失败自动回滚；同一版本且内容相同时不重复写入。需要回滚时人工执行：

```sh
uv run python scripts/update_us.py --restore backups/us/us-时间-before.json --apply
# 回滚后重启服务，刷新词频缓存
```

当前仓库基础快照为 `2026 Rev 21`。脚本仅读取本地文件，版本标签必须由维护者与官方来源核对。

验证更新逻辑：

```sh
uv run python -m unittest discover -s tests -v
# 可选：真实 PostgreSQL 集成测试，在独立临时 schema 内执行并清理
HSCODE_TEST_US_JSON=/path/hts_2026_revision_21_json.json uv run python -m unittest discover -s tests -v
```

## 导出与维护

```sh
# 使用共享容器内的 PostgreSQL 18 工具导出
uv run hscode-export --container shared-postgres
```

导出生成不含向量的 `data/hscode_dump.sql` 和 `data/hscode_dump.dump`，不修改源数据。导出过程需要创建临时数据库，普通应用账号默认无此权限；使用有 CREATEDB 权限的维护账号运行，通过 `HSCode_DSN` 指定连接。Docker 镜像内置 PostgreSQL 18 客户端工具。

旧库清理迁移位于 `migrations/`，新导入库无需执行。`src/hscode/` 包含服务、模型配置、向量生成和导出逻辑；`scripts/smoke_test.py` 是部署后的 HTTP 冒烟验证。

## 数据与工具

| 工具 | 用途 |
| --- | --- |
| `search_hs_code` | 中英文关键词检索，中国数据支持语义召回 |
| `get_hs_detail` | 品名、法定单位和层级详情 |
| `list_chapter_codes` | 按章或品目浏览 |
| `compare_across_countries` | 以 HS6 为锚点进行中美对照 |

数据库保留 `hs_code`、`hs_chapter`、`data_release` 三张表。修复后的英文品名直接存于 `hs_code.description_en`。

中国数据合并自 2015 年基线和 2016–2023 年变更集，不是任何年份的完整有效税则目录；美国数据为 USITC 单版本快照。服务不提供历史追溯、税率、监管条件或申报要素。

## 调用日志

默认 INFO 日志写入 stderr，HTTP 和 stdio 模式均可用。每次工具、资源或提示词调用记录唯一 `call_id`、名称、参数摘要、开始 / 结束、耗时；工具结果记录匹配方式、数量或是否找到。异常记录类型和调用堆栈，不记录完整结果或数据库连接串。字符串参数最多保留 200 个字符。

```sh
uv run hscode --http 8765 --stateless --log-level DEBUG
# Docker 查看日志
docker compose logs -f app
```

`--log-level`（或 `HSCODE_LOG_LEVEL` 环境变量）控制业务调用日志；底层 HTTP 访问日志独立保留。无状态模式的重复 `Terminating session: None` 已过滤，其他协议日志保留。初始化、工具列表请求仍可只出现 HTTP 日志，它们不会执行业务查询。
