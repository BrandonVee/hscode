"""本地开发的默认数据库连接；部署通过 HSCode_DSN 或 libpq 环境变量覆盖。"""

DEFAULT_DSN = "postgresql://hscode:hscode-dev-password@127.0.0.1:5435/hscode"
