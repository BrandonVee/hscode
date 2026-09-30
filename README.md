# 海关编码查询 MCP

PostgreSQL + pgvector，提供关键词检索、编码详情、按章浏览和中美 HS6 对照四个 MCP 工具。向量由脚本从中国品名生成；基础数据导出不携带向量。

## Docker 启动

```sh
cp .env.example .env
# 按需修改 .env 中的数据库密码、HTTP 端口和推理线程数

docker compose up -d --build
```

PostgreSQL 首次导入 `data/hscode_dump.sql` 后，`app` 启动全文查询服务，`vectors` 在后台生成 9,579 条中国品名向量和 HNSW 索引。首次生成需要下载模型；数据库和模型缓存分别保存在 `pgdata`、`model_cache` 卷中。后续启动只补缺失向量，模型和向量就绪后服务自动启用语义检索。

默认 MCP 地址：`http://127.0.0.1:8765/mcp/hscode`，使用 Streamable HTTP，无状态模式。可通过 `.env` 中的 `HSCODE_PORT` 修改宿主机端口。数据库仅供 Compose 网络内的应用连接。

### 远程客户端连接

默认 `HSCODE_BIND_HOST=127.0.0.1`，端口仅允许服务器本机访问。若客户端通过服务器 IP 直连，在 `.env` 设置 `HSCODE_BIND_HOST=0.0.0.0`，执行 `docker compose up -d app` 重新创建应用容器；客户端使用 `http://服务器IP:8765/mcp/hscode`，云服务器安全组需允许客户端访问该端口。端口绑定规则见 [Docker 文档](https://docs.docker.com/engine/network/port-publishing/)。

若使用服务器本机的 Nginx，保留本机绑定，代理到 `http://127.0.0.1:8765/mcp/hscode` 并保留完整路径；客户端填写域名下的对应 HTTPS 地址。Nginx 若也在 Docker 内，`127.0.0.1` 指向 Nginx 容器自身，应将代理容器接入应用网络并使用 `http://app:8765/mcp/hscode`。

日志中约每 30 秒出现的本机 `POST /mcp/hscode 200 OK` 和 `Terminating session: None` 来自健康检查。它们验证容器内的 MCP initialize 请求，不能证明客户端到服务器的网络连接可用。客户端地址需要包含 `/mcp/hscode`；协议选择 Streamable HTTP。

可在服务器本机验证 MCP 初始化，然后在客户端所在机器将地址换成实际连接地址，执行同一请求：

```sh
curl --connect-timeout 5 --max-time 15 -i http://127.0.0.1:8765/mcp/hscode \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"connection-check","version":"1.0"}}}'
```

成功时返回 HTTP 200 和 MCP 初始化结果。`fetch failed` 若未对应到应用访问日志，应先检查客户端 URL、监听地址、端口规则及反向代理连通性。

```sh
docker compose ps -a
docker compose logs -f vectors app
# 停止服务并保留数据库和模型缓存
docker compose down
```

基础 SQL 只在数据库卷首次初始化时导入；已有卷不会因文件更新而重新灌库。`app` 仅等待数据库健康，向量下载或生成失败不会阻止全文查询服务启动。HTTP 健康检查通过 MCP initialize 请求验证服务。

构建时若 `apt-get` 或 Python 包下载很慢，可将 `.env` 中的软件源改为：

```dotenv
PYPI_INDEX_URL=https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple
DEBIAN_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian
DEBIAN_SECURITY_MIRROR=https://mirrors.tuna.tsinghua.edu.cn/debian-security
```

然后单独构建共享应用镜像，再启动所有服务：

```sh
docker compose build app
docker compose up -d
```

依赖安装与源码安装分层，使用 BuildKit 缓存保存已下载的软件包；源码修改不会使依赖层失效。切换 Python 镜像源时仍按 `uv.lock` 的版本及哈希安装，不重新选取依赖版本。第一次拉取基础镜像的速度由 Docker Registry 网络决定，上述配置只影响软件包下载。配置参考 [uv Docker 文档](https://docs.astral.sh/uv/guides/integration/docker/)、[清华 PyPI 镜像](https://mirrors.tuna.tsinghua.edu.cn/help/pypi/) 和 [Debian 镜像](https://mirrors.tuna.tsinghua.edu.cn/help/debian/)。

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

向量固定使用 `BAAI/bge-small-zh-v1.5`（512 维），建库和查询共用配置。生成脚本按批提交，中断后重新运行即可继续；支持 `--batch-size`、`--threads`、`--limit` 和 `--rebuild`。脚本会分别显示模型准备、向量生成和索引建立阶段。美国数据继续使用全文检索。请求处理只读本地模型缓存，不联网下载；模型或向量未就绪时使用全文查询。

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
