"""
模型回退链模拟测试。

通过 mock 替换 OpenAI client，模拟各种失败场景，验证 _llm_call 的回退逻辑。
不依赖真实 LLM 服务，可离线运行。

用法：
  cd <项目根目录>
  python test/test_model_fallback.py
"""
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch, call

# 确保从项目根目录导入
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 设置必要的环境变量，避免 config.py 因缺少变量而报错
os.environ.setdefault("MODEL_SERVICE_URL", "http://mock-llm:1234/v1")
os.environ.setdefault("API_KEY", "sk-mock-key")
os.environ.setdefault("MODEL_NAME", "Qwen3.5-397B-A17B")
os.environ.setdefault("FALLBACK_MODELS", "Kimi-K2.6,DeepSeek-V4-Pro")

from openai import APIStatusError, APIConnectionError, APITimeoutError
import httpx

import core.parser as parser_module
from core.parser import _llm_call, _llm_try_model, FALLBACK_MODELS

SEP = "=" * 60


def _make_resp(text: str = '{"candidate":{"name":"测试"}}') -> MagicMock:
    """构造一个模拟的 chat completion 响应对象。"""
    choice  = MagicMock()
    choice.message.content = text
    usage   = MagicMock()
    usage.prompt_tokens     = 100
    usage.completion_tokens = 50
    usage.total_tokens      = 150
    resp    = MagicMock()
    resp.choices = [choice]
    resp.usage   = usage
    return resp


def _make_status_error(status_code: int) -> APIStatusError:
    req  = httpx.Request("POST", "http://mock/v1/chat/completions")
    resp = httpx.Response(status_code, request=req)
    return APIStatusError(
        message=f"HTTP {status_code}",
        response=resp,
        body={"error": {"message": f"HTTP {status_code}"}},
    )


def _make_connection_error() -> APIConnectionError:
    req = httpx.Request("POST", "http://mock/v1/chat/completions")
    return APIConnectionError(request=req)


def _make_timeout_error() -> APITimeoutError:
    req = httpx.Request("POST", "http://mock/v1/chat/completions")
    return APITimeoutError(request=req)


MESSAGES = [{"role": "user", "content": "test"}]


class TestModelFallback(unittest.TestCase):

    def _patch_create(self, side_effects: list):
        """patch client.chat.completions.create，按顺序返回/抛出 side_effects。"""
        mock = MagicMock(side_effect=side_effects)
        return patch.object(parser_module.client.chat.completions, "create", mock)

    # ── 场景 1：主模型 404 → 回退 Kimi 成功 ──────────────────────────
    def test_fallback_on_404(self):
        print(f"\n{SEP}")
        print("场景 1: 主模型 404 → 回退到 Kimi-K2.6 成功")
        effects = [
            _make_status_error(404),   # Qwen 404
            _make_resp(),              # Kimi 成功
        ]
        with self._patch_create(effects) as mock_create:
            result = _llm_call(MESSAGES)
        self.assertIsNotNone(result)
        self.assertEqual(mock_create.call_count, 2)
        models_tried = [c.kwargs["model"] for c in mock_create.call_args_list]
        self.assertEqual(models_tried[0], "Qwen3.5-397B-A17B")
        self.assertEqual(models_tried[1], "Kimi-K2.6")
        print(f"  ✓ 调用链: {' → '.join(models_tried)} (成功)")

    # ── 场景 2：主模型超时 → 回退 Kimi 成功 ─────────────────────────
    def test_fallback_on_timeout(self):
        print(f"\n{SEP}")
        print("场景 2: 主模型超时 → 回退到 Kimi-K2.6 成功")
        effects = [
            _make_timeout_error(),  # Qwen 超时
            _make_resp(),           # Kimi 成功
        ]
        with self._patch_create(effects) as mock_create:
            result = _llm_call(MESSAGES)
        self.assertIsNotNone(result)
        self.assertEqual(mock_create.call_count, 2)
        print(f"  ✓ 超时后回退成功，共调用 {mock_create.call_count} 次")

    # ── 场景 3：主模型 503 → Kimi 500 → DeepSeek 成功 ───────────────
    def test_fallback_chain_to_second(self):
        print(f"\n{SEP}")
        print("场景 3: 主模型 503 → Kimi 500 → DeepSeek-V4-Pro 成功")
        effects = [
            _make_status_error(503),  # Qwen 503
            _make_status_error(500),  # Kimi 500
            _make_resp(),             # DeepSeek 成功
        ]
        with self._patch_create(effects) as mock_create:
            result = _llm_call(MESSAGES)
        self.assertIsNotNone(result)
        self.assertEqual(mock_create.call_count, 3)
        models_tried = [c.kwargs["model"] for c in mock_create.call_args_list]
        self.assertEqual(models_tried, ["Qwen3.5-397B-A17B", "Kimi-K2.6", "DeepSeek-V4-Pro"])
        print(f"  ✓ 完整回退链: {' → '.join(models_tried)} (成功)")

    # ── 场景 4：所有模型均失败 → 抛出最后一个错误 ───────────────────
    def test_all_models_fail(self):
        print(f"\n{SEP}")
        print("场景 4: 全部模型失败 → 抛出最后错误")
        effects = [
            _make_status_error(503),  # Qwen 503
            _make_timeout_error(),    # Kimi 超时
            _make_status_error(502),  # DeepSeek 502
        ]
        with self._patch_create(effects) as mock_create:
            with self.assertRaises(Exception) as ctx:
                _llm_call(MESSAGES)
        self.assertEqual(mock_create.call_count, 3)
        print(f"  ✓ 全部失败后正确抛出: {type(ctx.exception).__name__}")

    # ── 场景 5：连接错误 → 立即抛出，不回退 ────────────────────────
    def test_no_fallback_on_connection_error(self):
        print(f"\n{SEP}")
        print("场景 5: 连接错误 → 立即抛出（不回退）")
        effects = [_make_connection_error()]
        with self._patch_create(effects) as mock_create:
            with self.assertRaises(APIConnectionError):
                _llm_call(MESSAGES)
        self.assertEqual(mock_create.call_count, 1)
        print(f"  ✓ APIConnectionError 立即抛出，共调用 {mock_create.call_count} 次（未回退）")

    # ── 场景 6：401 鉴权失败 → 立即抛出，不回退 ────────────────────
    def test_no_fallback_on_401(self):
        print(f"\n{SEP}")
        print("场景 6: 401 鉴权失败 → 立即抛出（不回退）")
        effects = [_make_status_error(401)]
        with self._patch_create(effects) as mock_create:
            with self.assertRaises(APIStatusError) as ctx:
                _llm_call(MESSAGES)
        self.assertEqual(mock_create.call_count, 1)
        self.assertEqual(ctx.exception.status_code, 401)
        print(f"  ✓ 401 立即抛出，共调用 {mock_create.call_count} 次（未回退）")

    # ── 场景 7：thinking block 错误 → 同模型去掉参数重试后成功 ──────
    def test_thinking_block_retry_then_success(self):
        print(f"\n{SEP}")
        print("场景 7: thinking block 错误 → 去掉 thinking 参数重试成功")
        effects = [
            Exception("thinking block Invalid signature"),  # 第一次带 thinking 参数失败
            _make_resp(),                                    # 去掉参数后成功
        ]
        with self._patch_create(effects) as mock_create:
            result = _llm_call(MESSAGES)
        self.assertIsNotNone(result)
        self.assertEqual(mock_create.call_count, 2)
        first_call_extra  = mock_create.call_args_list[0].kwargs.get("extra_body")
        second_call_extra = mock_create.call_args_list[1].kwargs.get("extra_body")
        self.assertNotEqual(first_call_extra, {})  # 第一次带 thinking 参数
        self.assertEqual(second_call_extra, {})    # 第二次去掉参数
        print(f"  ✓ thinking block 重试成功，共调用 {mock_create.call_count} 次（同模型）")

    # ── 场景 8：thinking block → 重试仍失败 → 回退到 Kimi ──────────
    def test_thinking_block_retry_then_fallback(self):
        print(f"\n{SEP}")
        print("场景 8: thinking block 重试仍失败 → 回退到 Kimi-K2.6")
        effects = [
            Exception("thinking block Invalid signature"),  # Qwen 第一次失败
            Exception("thinking block still broken"),       # Qwen 去掉参数后仍失败
            _make_resp(),                                    # Kimi 成功
        ]
        with self._patch_create(effects) as mock_create:
            result = _llm_call(MESSAGES)
        self.assertIsNotNone(result)
        self.assertEqual(mock_create.call_count, 3)
        models_tried = [c.kwargs["model"] for c in mock_create.call_args_list]
        self.assertEqual(models_tried[2], "Kimi-K2.6")
        print(f"  ✓ thinking block 重试后失败，回退到 Kimi-K2.6 成功")


if __name__ == "__main__":
    print(f"\nFALLBACK_MODELS 配置: {FALLBACK_MODELS}")
    print(f"回退链: Qwen3.5-397B-A17B → {' → '.join(FALLBACK_MODELS)}")
    loader  = unittest.TestLoader()
    suite   = loader.loadTestsFromTestCase(TestModelFallback)
    runner  = unittest.TextTestRunner(verbosity=0, stream=sys.stdout)
    result  = runner.run(suite)
    print(f"\n{SEP}")
    print(f"结果: {result.testsRun} 个测试  "
          f"通过={result.testsRun - len(result.failures) - len(result.errors)}  "
          f"失败={len(result.failures)}  错误={len(result.errors)}")
    sys.exit(0 if result.wasSuccessful() else 1)
