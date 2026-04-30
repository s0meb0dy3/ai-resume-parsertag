"""
统一配置：模型服务、测试数据、输出目录
"""
import os

# ── 内网模型服务 ───────────────────────────────────────────
MODEL_SERVICE_URL = "http://10.2.5.7:13001/v1"
API_KEY           = "ECkDBnvniXH4xbVi2d5476CcB75941339e392e89Ab48Db97"
MODEL_NAME        = "Qwen3.5-397B-A17B"

# ── 推理参数 ───────────────────────────────────────────────
TEMPERATURE  = 0.01
MAX_TOKENS   = 4096

# ── PDF 处理 ───────────────────────────────────────────────
PDF_DPI             = 144       # 渲染分辨率
DIGITAL_CHAR_THRESH = 50        # 每页平均字符数 > 此值视为数字化PDF
PDF_MAX_PAGES_ONCE  = 8         # 方案C单次调用最大页数，超过走方案D
BATCH_SIZE          = 2         # 方案D每批页数
MAX_WORKERS         = 4         # 方案D并发线程数

# ── 原始方案：DeepSeek-OCR FastAPI 服务 ────────────────────
# 服务启动方式：cd .../DeepSeek-OCR-vllm && uvicorn app_ocr:app --port 28800
# 本机调试：http://127.0.0.1:28800
# 内网服务器：http://10.2.5.7:28800（与 test_app_ocr.py 中 base_url 一致）
DEEPSEEK_OCR_URL = "http://10.2.5.7:13001"
DEEPSEEK_OCR_KEY = "sk-Nj51uMYWCC6bO5qg5aDa0cFa167c4aAaBdDf638f12C49434"  # 服务端伪key

# ── 测试数据路径 ────────────────────────────────────────────
TEST_PDF_DIR = "/Users/funian/AIAgent/KC/AIHR/AIPDF/uploads/test_full"
OUTPUT_DIR   = os.path.join(os.path.dirname(__file__), "results")
os.makedirs(OUTPUT_DIR, exist_ok=True)

