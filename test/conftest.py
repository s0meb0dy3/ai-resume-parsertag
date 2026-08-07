"""为测试套件提供隔离的模型配置和仓库导入路径。"""

import os
import sys
from pathlib import Path


os.environ["MODEL_SERVICE_URL"] = "http://mock-llm:1234/v1"
os.environ["API_KEY"] = "sk-mock-key"
os.environ["MODEL_NAME"] = "Qwen3.5-397B-A17B"
os.environ["FALLBACK_MODELS"] = "Kimi-K2.6,DeepSeek-V4-Pro"
os.environ["LANGFUSE_SECRET_KEY"] = ""
os.environ["LANGFUSE_PUBLIC_KEY"] = ""

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
