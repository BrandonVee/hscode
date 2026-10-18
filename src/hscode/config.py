"""从运行目录的 .env 加载配置；已设置的环境变量优先。"""
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path.cwd() / ".env", override=False)

# 连接参数由 .env / 环境变量中的 PG* 提供；HSCode_DSN 可显式覆盖。
DEFAULT_DSN = ""
