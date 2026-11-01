"""读取 .env 与环境变量。环境变量优先，.env 只做补充。"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(ROOT / ".env")

DATABASE_URL = os.getenv("DATABASE_URL", "")
JWT_SECRET = os.getenv("JWT_SECRET", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
JWT_HOURS = int(os.getenv("JWT_HOURS", "12"))

# 通义千问的 OpenAI 兼容接口；换 DeepSeek 等只需改这三项
LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("DASHSCOPE_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://token-plan.maas.qianwenaiapi.com/compatible-mode/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen3.8-flash")

# 可在“系统管理”中修改的默认值，首次启动时写入 settings 表
DEFAULT_SETTINGS = {
    "llm_model": LLM_MODEL,
    "min_margin": "0.10",       # 调价后的最低毛利率
    "max_price_change": "0.15",  # 单次调价的最大幅度
    "agent_max_steps": "6",     # 单次任务最多调用工具的轮数
}
