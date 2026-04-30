# PDF 简历解析技术方案文档

> 内网模型服务：`http://10.2.5.7:13001/v1`  
> 模型：`Qwen3.5-397B-A17B`  
> 生成日期：2026-04-10

---

## 文档目录

| 文件 | 方案 | 核心链路 |
|------|------|---------|
| [solution_A_native_multimodal.md](solution_A_native_multimodal.md) | 改善A：原生多模态 | PDF → 图像 → Qwen3.5视觉 → JSON |
| [solution_B_binary_stream.md](solution_B_binary_stream.md) | 改善B：二进制流 | PDF → bytes内存流 → 多模态LLM → JSON |
| [solution_C_direct_pdf.md](solution_C_direct_pdf.md) | 改善C：直传PDF | PDF → base64 → LLM原生解析 → JSON |
| [solution_D_batch_processing.md](solution_D_batch_processing.md) | 改善D：分批处理 | PDF → 分页渲染 → 并发批次推理 → 合并 → JSON |
| [solution_E_vlm_comparison.md](solution_E_vlm_comparison.md) | 闭源VLM对比 | GPT-4o / Claude / Gemini vs 内网方案 |

---

## 快速选型

```
页数 ≤ 2 页   →  方案A（整图一次推理，最简单）
页数 > 2 页   →  方案D（分批并发，不受上下文限制）
无GPU环境     →  方案C（直传PDF，纯API调用）
数字化PDF     →  方案B（文本提取快速通道，<0.5s）
```

## 统一模型配置

```python
MODEL_SERVICE_URL = "http://10.2.5.7:13001/v1"
API_KEY           = "ECkDBnvniXH4xbVi2d5476CcB75941339e392e89Ab48Db97"
MODEL_NAME        = "Qwen3.5-397B-A17B"
```
