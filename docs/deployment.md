# 服务端部署

项目使用已有 PostgreSQL，应用只读取固定数据快照，不自动更新数据。以下步骤使用 Docker Compose，面向 Linux 服务端，需要服务器已有 PostgreSQL（原生运行或已发布到宿主机端口的容器均可）。

## 首次部署

```sh
git clone git@github.com:BrandonVee/hscode.git
cd hscode
cp .env.example .env
chmod 600 .env
```

编辑 `.env`，填写实际数据库账号及密码：

```dotenv
DATABASE_URL=postgresql://hscode:填写URL编码后的密码@host.docker.internal:5432/hscode
HSCODE_BIND_HOST=0.0.0.0
HSCODE_PORT=8765
```

Compose 使用 bridge 网络，容器内部固定监听 `0.0.0.0:8765`；通过 `HSCODE_BIND_HOST`、`HSCODE_PORT` 将端口发布到宿主机。数据库统一由单个 `DATABASE_URL` 指定，不配置 `PGHOST` 等单独字段，也不读取用户目录中的其他配置。

数据库在宿主机时使用 `host.docker.internal`，Compose 已配置 `host-gateway` 映射。Linux 上数据库必须监听容器可达的宿主机地址，并在 `pg_hba.conf` / 防火墙允许应用的 Docker 网段；若数据库仅绑定 `127.0.0.1`，bridge 容器无法连接。数据库为已有容器时，也需将数据库端口发布到应用可达的宿主机地址。外部数据库则直接填写实际域名或 IP。

本机不用 Docker 直接启动时，将 URL 主机改为 `127.0.0.1` 或实际地址。密码中的 `@`、`:`、`/`、`#` 等字符需 URL 编码，实际 `.env` 可以用单引号包住整个 URL，避免 `$` 被插值。

请管理员预先创建 `hscode` 数据库及专用账号，并在**空库**导入仓库的 `data/hscode_dump.sql`。例如数据库恰好是本机 Docker 容器 `shared-postgres`、管理员为 `postgres` 时：

```sh
# 仅首次导入空库执行；账号和数据库需要预先建好。
docker exec -i shared-postgres psql -X -U postgres -d hscode \
  -v ON_ERROR_STOP=1 --single-transaction < data/hscode_dump.sql
```

以上导入示例使用数据库管理员；应用连接账号仍是 `.env` 中的专用账号。实际认证方式按数据库配置处理。若目标库已有数据，不要直接再次执行完整建库 SQL，先备份并制定迁移方案。

启动应用：

```sh
docker compose config --quiet
docker compose up -d --build app
docker compose ps
docker compose logs --tail=100 app
```

MCP 地址为 `http://127.0.0.1:8765/mcp/hscode`，协议为 Streamable HTTP。默认监听 `0.0.0.0`，远程客户端使用 `http://服务器IP:8765/mcp/hscode`，并配置服务器防火墙；仅通过同机反向代理访问时可设置 `HSCODE_BIND_HOST=127.0.0.1`，代理完整 `/mcp/hscode` 路径。

容器健康检查只验证 MCP 初始化。验证真实查询需在装有 uv 的环境运行：

```sh
uv sync --frozen
uv run python scripts/smoke_test.py --url http://127.0.0.1:8765/mcp/hscode --allow-no-vectors
```

## 后续发布

```sh
git pull --ff-only
docker compose up -d --build app
docker compose logs --tail=100 app
```

`.env` 保留服务器自己的配置，不从仓库覆盖。若发布涉及数据变更，先备份数据库，再由维护者人工同步对应版本数据、核对 `hs://dataset-info`，重启服务并验证。更新镜像或拉取代码不会自动替换数据库。

美国数据人工维护流程见 [README](../README.md#美国-hts-人工维护与发布)。默认不启动向量任务，无需下载模型。
