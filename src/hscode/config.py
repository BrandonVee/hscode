"""从运行目录的 .env 加载配置；已设置的环境变量优先。"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path.cwd() / ".env", override=False)

# 所有入口共用一个连接 URL，不从分散的 PG* 字段拼接。
DEFAULT_DSN = os.environ.get("DATABASE_URL", "")
