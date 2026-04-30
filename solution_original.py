"""
原始方案：DeepSeek-OCR 两步流水线
链路：PDF（本地路径）→ MinIO 上传 → 预签名URL → DeepSeek-OCR vLLM（OCR→Markdown）→ Qwen LLM（Markdown→JSON）

依赖：
  - DeepSeek-OCR FastAPI 服务通过 One API (DEEPSEEK_OCR_URL) 代理访问
  - MinIO 存储服务用于在本机与 OCR 服务器间传递 PDF 文件

服务端接口（app_ocr.py）:
  POST /v1/chat/completions
  model: "DeepSeek-OCR-JSON"
  content 格式（url 类型）：
    [{"type": "url", "file": {"id": ..., "name": ..., "path": <HTTP URL>}}]
"""
import json
import json_repair
import requests
import tempfile
import os
from pathlib import Path
from datetime import timedelta
from minio import Minio
from config import DEEPSEEK_OCR_URL, DEEPSEEK_OCR_KEY


# ── MinIO 配置 ───────────────────────────────────────────────
MINIO_ENDPOINT  = "10.2.5.114:9000"
MINIO_ACCESS    = "minioadmin"
MINIO_SECRET    = "minioadmin"
MINIO_BUCKET    = "hr-interview"
MINIO_PREFIX    = "benchmark-test"   # 上传目录前缀
MINIO_URL_TTL   = 3600               # 预签名 URL 有效期（秒）

_minio = Minio(MINIO_ENDPOINT, access_key=MINIO_ACCESS,
               secret_key=MINIO_SECRET, secure=False)


def _upload_to_minio(pdf_path: str) -> tuple[str, str]:
    """
    上传本地 PDF 到 MinIO，返回 (object_name, presigned_url)
    """
    filename    = Path(pdf_path).name
    object_name = f"{MINIO_PREFIX}/{filename}"

    # 确保 bucket 存在
    if not _minio.bucket_exists(MINIO_BUCKET):
        _minio.make_bucket(MINIO_BUCKET)

    _minio.fput_object(MINIO_BUCKET, object_name, pdf_path,
                       content_type="application/pdf")

    url = _minio.presigned_get_object(
        MINIO_BUCKET, object_name, expires=timedelta(seconds=MINIO_URL_TTL)
    )
    return object_name, url


def _delete_from_minio(object_name: str):
    """测试完成后清理 MinIO 上的临时文件"""
    try:
        _minio.remove_object(MINIO_BUCKET, object_name)
    except Exception:
        pass


# ── HTTP 客户端 ───────────────────────────────────────────────

class DeepSeekOCRClient:
    """调用 DeepSeek-OCR FastAPI 服务的轻量客户端"""

    def __init__(self, base_url: str = DEEPSEEK_OCR_URL, api_key: str = DEEPSEEK_OCR_KEY):
        self.url = base_url.rstrip("/") + "/v1/chat/completions"
        self.headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

    def parse_by_url(self, pdf_url: str, filename: str = "resume.pdf",
                     verbose: bool = False) -> dict:
        """
        通过 HTTP URL 调用（服务器从 URL 下载 PDF，适合跨机场景）
        app_ocr.py: content_json["type"] == "url" → download_file(path) → pdf_ocr(temp_path)
        """
        payload = {
            "model": "DeepSeek-OCR-JSON",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "url",
                            "file": {
                                "id": Path(filename).stem,
                                "type": "url",
                                "name": filename,
                                "path": pdf_url,
                            },
                        }
                    ],
                }
            ],
            "temperature": 0.1,
            "max_tokens": 4096,
        }
        if verbose:
            print(f"  [Original] → {self.url}  mode=url  file={filename}")
            print(f"  [Original]   url={pdf_url[:80]}...")
        return self._post(payload)

    def _post(self, payload: dict) -> dict:
        try:
            resp = requests.post(self.url, headers=self.headers, json=payload, timeout=180)
            resp.raise_for_status()
            data = resp.json()
            raw = data["choices"][0]["message"]["content"]
            result = json_repair.loads(raw)
            if not isinstance(result, dict):
                raise ValueError(f"服务端返回非 dict: {raw[:200]}")
            return result
        except requests.exceptions.ConnectionError as e:
            raise ConnectionError(
                f"DeepSeek-OCR 服务不可达 ({self.url})。\n{e}"
            ) from e
        except requests.exceptions.Timeout:
            raise TimeoutError(f"DeepSeek-OCR 服务响应超时（180s）: {self.url}")


# ── 公开接口（与 solution_a/b/c/d 保持一致）────────────────────

_client = DeepSeekOCRClient()


def run(source, verbose: bool = False) -> dict:
    """
    原始方案入口，与 solution_a/b/c/d.run() 签名一致。

    source: str（PDF本地路径）或 bytes（字节流）

    流程：
      1. 将 PDF 上传到 MinIO（10.2.5.114:9000）
      2. 生成预签名 URL（TTL=1h）
      3. 调用 DeepSeek-OCR 服务（url 模式）
      4. 清理 MinIO 临时文件
    """
    # bytes 模式：先写临时文件
    tmp_path = None
    if isinstance(source, bytes):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(source)
            tmp_path = tmp.name
        pdf_path = tmp_path
    else:
        pdf_path = str(source)

    filename    = Path(pdf_path).name
    object_name = None
    try:
        if verbose:
            print(f"  [Original] 上传到 MinIO: {filename}")
        object_name, url = _upload_to_minio(pdf_path)
        if verbose:
            print(f"  [Original] MinIO 上传完成，调用 OCR 服务...")
        return _client.parse_by_url(url, filename=filename, verbose=verbose)
    finally:
        if object_name:
            _delete_from_minio(object_name)
        if tmp_path:
            os.unlink(tmp_path)


# ── 独立测试 ──────────────────────────────────────────────────

if __name__ == "__main__":
    import sys, json

    pdf = sys.argv[1] if len(sys.argv) > 1 else \
        "/Users/funian/AIAgent/KC/AIHR/AIPDF/uploads/test_full/【大模型Agent算法工程师_深圳】曹杨凯 25年应届生.pdf"

    print(f"测试文件: {pdf}")
    print(f"OCR 服务: {DEEPSEEK_OCR_URL}")
    print(f"MinIO:    {MINIO_ENDPOINT}/{MINIO_BUCKET}")
    try:
        result = run(pdf, verbose=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ConnectionError, TimeoutError) as e:
        print(f"\n[ERROR] {e}")
    except Exception as e:
        import traceback
        traceback.print_exc()
