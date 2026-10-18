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
PGHOST=127.0.0.1
PGPORT=5432
PGDATABASE=hscode
PGUSER=hscode
PGPASSWORD=填写实际数据库密码
HSCODE_BIND_HOST=0.0.0.0
HSCODE_PORT=8765
```

Compose 使用 `network_mode: host`，应用直接使用宿主机网络。`PGHOST=127.0.0.1`、`PGPORT=5432` 即连接服务器本机数据库；外部数据库填写实际 IP 或域名。不需要配置 Docker 网络名或端口映射，监听地址和端口直接由 `HSCODE_BIND_HOST`、`HSCODE_PORT` 控制。程序不会读取开发机器的其他配置文件。

该部署方式面向 Linux。macOS / Windows 本地开发推荐 `uv run hscode`，Docker Desktop 如需使用宿主机网络，应先启用对应支持。

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
