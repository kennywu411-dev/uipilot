"""WebHands：浏览器 Agent 底座 + 两个落地应用（探索式测试、单据录入）。"""

import os
from pathlib import Path


def _load_dotenv() -> None:
    """读项目根目录的 .env（只补充环境里没有的变量）。.env 已在 .gitignore 里。"""
    env = Path(__file__).resolve().parents[2] / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()
