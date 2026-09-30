# 海关编码查询 MCP

PostgreSQL + pgvector，提供关键词检索、编码详情、按章浏览和中美 HS6 对照四个 MCP 工具。向量由脚本从中国品名生成；基础数据导出不携带向量。

## Docker 启动

```sh
cp .env.example .env
# 按需修改 .env 中的数据库密码、HTTP 端口和推理线程数

docker compose up -d --build
```

启动流程：PostgreSQL 首次导入 `data/hscode_dump.sql` → `vectors` 任务生成 9,579 条中国品名向量和 HNSW 索引 → `app` 启动 MCP HTTP 服务。首次运行需要下载模型；数据库和模型缓存分别保存在 `pgdata`、`model_cache` 卷中。后续启动只补缺失向量。

默认 MCP 地址：`http://127.0.0.1:8765/mcp/hscode`，使用 Streamable HTTP，无状态模式。可通过 `.env` 中的 `HSCODE_PORT` 修改宿主机端口。数据库仅供 Compose 网络内的应用连接。

```sh
docker compose ps -a
docker compose logs -f vectors app
# 停止服务并保留数据库和模型缓存
docker compose down
```

基础 SQL 只在数据库卷首次初始化时导入；已有卷不会因文件更新而重新灌库。`app` 等待数据库健康和向量任务成功后才启动，HTTP 健康检查通过 MCP initialize 请求验证服务。

镜像按构建机器的架构生成。若需在 x86 Linux 运行，可构建 amd64 镜像：

```sh
docker buildx build --platform linux/amd64 -t hscode-mcp:local --load .
```

## 维护命令

```sh
# 补齐缺失向量
docker compose run --rm vectors
# 品名修改后重建全部中国向量
docker compose run --rm vectors hscode-build-vectors --rebuild
# 导出基础 SQL 和压缩版到宿主机 data/；使用当前用户写入文件
docker compose run --rm --user "$(id -u):$(id -g)" export
# 通过真实 MCP HTTP 调用验证四个工具和语义检索
uv sync
uv run python scripts/smoke_test.py
```

向量固定使用 `BAAI/bge-small-zh-v1.5`（512 维），建库和查询共用配置。生成脚本按批提交，中断后重新运行即可继续；支持 `--batch-size`、`--threads`、`--limit` 和 `--rebuild`。美国数据继续使用全文检索。未生成向量的数据库仍支持全文查询。

导出脚本使用同一数据快照复制基础字段，通过临时数据库生成两种导出格式，结束后删除临时库；不改动源数据库中的现有向量。运行账户需要创建数据库权限。镜像包含 PostgreSQL 16 导出工具，数据库文件和模型权重不打入应用镜像。

## 本地开发

```sh
uv sync
# 默认连接原本的本机专用库，127.0.0.1:5435/hscode
# 可用 HSCode_DSN 指定其他库；也支持 libpq 的 PGUSER/PGPASSWORD 等环境变量
uv run hscode-build-vectors
uv run hscode                  # stdio
uv run hscode --http 8765       # HTTP
```

本机恢复基础数据：

```sh
createdb hscode
psql -X -v ON_ERROR_STOP=1 -d hscode -f data/hscode_dump.sql
# 或恢复同一份数据的压缩版
pg_restore --exit-on-error --no-owner --no-privileges -d hscode data/hscode_dump.dump
```

生成向量需要 PostgreSQL 安装 pgvector。基础导出不包含向量字段、向量索引或 pgvector 扩展，可以先导入普通 PostgreSQL。

```sh
# 本机已安装 pg_dump / pg_restore 时导出
uv run hscode-export
# 使用现有本机数据库容器的 PostgreSQL 工具导出
uv run hscode-export --container mcp-hscode-vector
```

本地导出默认输出到 `data/`，通过 `--output-dir` 更改目录。服务、向量脚本和导出脚本均接受 `HSCode_DSN`；脚本也支持 `--dsn`。`HS_MODEL_CACHE` 指定模型缓存目录。

## 项目目录

```text
src/hscode/         MCP 服务、向量生成、基础导出和共享配置
scripts/            部署后的 HTTP 验证脚本
data/               基础 SQL 和压缩导出，无向量
migrations/         旧数据库清理迁移
Dockerfile          应用镜像，多阶段构建
compose.yaml        数据库、向量任务、HTTP 服务和导出任务
.env.example        Compose 配置示例
pyproject.toml      包和依赖配置
uv.lock             锁定依赖
```

`backups/` 中保留清理前的归档备份，已排除在 Git 和镜像构建上下文之外。旧库依次运行 `migrations/001_remove_history.sql` 和 `migrations/002_remove_repair_metadata.sql`；新导入库无需执行这些迁移。

## 数据与工具

| 工具 | 用途 |
| --- | --- |
| `search_hs_code` | 中英文关键词检索，中国数据支持语义召回 |
| `get_hs_detail` | 品名、法定单位和层级详情 |
| `list_chapter_codes` | 按章或品目浏览 |
| `compare_across_countries` | 以 HS6 为锚点进行中美对照 |

数据库保留 `hs_code`、`hs_chapter`、`data_release` 三张表。修复后的英文品名直接存于 `hs_code.description_en`。

中国数据合并自 2015 年基线和 2016–2023 年变更集，不是任何年份的完整有效税则目录；美国数据为 USITC 单版本快照。服务不提供历史追溯、税率、监管条件或申报要素。
