#!/usr/bin/env python3
"""比较两种同步解析链路在十份真实文本型 PDF 上的耗时。

脚本按真实页数分层选择样本，交替调用当前同步接口和 `profile-parse` 生产解析
函数。包含原始文件映射和完整解析结果的报告仅写入 `evaluation/private`；可提交的
Markdown 报告只保留匿名样本编号、页数、字符数、耗时和结构数量。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextvars
import hashlib
import json
import os
import re
import statistics
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESUME_ROOT = PROJECT_ROOT / "uploads" / "uploads"
DEFAULT_PROFILE_ROOT = PROJECT_ROOT.parent / "profile-parse"
DEFAULT_ENDPOINT = "http://127.0.0.1:21804/api/resume/sync/parse"
DEFAULT_PRIVATE_REPORT = (
    PROJECT_ROOT / "evaluation" / "private" / "real_resume_comparison.json"
)
DEFAULT_MARKDOWN_REPORT = PROJECT_ROOT / "docs" / "真实简历同步解析方式对比报告.md"
DEFAULT_LOG_ROOT = PROJECT_ROOT / "localmaterial" / "offline" / "data" / "logs"
PAGE_TARGETS = (1, 1, 2, 2, 3, 4, 5, 6, 7, 8)
_ELAPSED_RE = re.compile(r"\belapsedSeconds=(?P<value>\d+(?:\.\d+)?)")
_TOKEN_RE = re.compile(
    r"\bprompt=(?P<prompt>\d+)\s+completion=(?P<completion>\d+)\s+"
    r"total=(?P<total>\d+)"
)
_JSON_TIMING_RE = re.compile(
    r"\bjsonDecodeSeconds=(?P<decode>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s+"
    r"postProcessSeconds=(?P<post_process>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s+"
    r"conversionSeconds=(?P<conversion>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
)


def _read_env(path: Path) -> dict[str, str]:
    """读取基准所需的简单 `KEY=VALUE` 环境变量。

    Args:
        path (Path): 当前项目的 `.env` 文件路径。

    Returns:
        dict[str, str]: 去除引号后的环境变量映射。

    Raises:
        OSError: 环境文件不存在或无法读取时抛出。
    """
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _configure_profile_environment(values: dict[str, str]) -> str:
    """将当前项目模型配置映射到 `profile-parse` 环境变量。

    Args:
        values (dict[str, str]): 当前项目环境变量映射。

    Returns:
        str: 两种链路共同使用的同步模型名称。

    Raises:
        KeyError: 模型地址、密钥或模型名称未配置时抛出。

    Note:
        基准进程移除代理变量，避免本机 SOCKS 代理改变本地接口和模型服务路径。
    """
    model_name = values.get("SYNC_MODEL_NAME") or values["MODEL_NAME"]
    os.environ["LLM_BASE_URL"] = values["MODEL_SERVICE_URL"]
    os.environ["LLM_API_KEY"] = values["API_KEY"]
    os.environ["LLM_MODEL"] = model_name
    os.environ["LLM_TIMEOUT_SECONDS"] = values.get("API_TIMEOUT", "300")
    for proxy_name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        os.environ.pop(proxy_name, None)
    return model_name


def _sha256(path: Path) -> str:
    """计算真实简历文件摘要，保证重复测试使用相同输入。

    Args:
        path (Path): 待计算摘要的 PDF 路径。

    Returns:
        str: 64 位十六进制 SHA-256 摘要。
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inventory_text_pdfs(resume_root: Path) -> list[dict[str, Any]]:
    """统计目录中可由两种链路共同处理的文本型 PDF。

    Args:
        resume_root (Path): 真实简历目录。

    Returns:
        list[dict[str, Any]]: 包含路径、页数、有效字符数和大小的候选列表。

    Note:
        扫描件不进入本次对比，因为 `profile-parse` 没有视觉降级能力。单页至少
        需要 300 个有效字符，多页平均至少需要 120 个有效字符。
    """
    import pymupdf

    records: list[dict[str, Any]] = []
    for path in sorted(resume_root.glob("*.pdf")):
        try:
            with pymupdf.open(path) as document:
                pages = document.page_count
                text = "\n".join(
                    page.get_text("text", sort=True) for page in document
                )
        except Exception:
            continue
        characters = sum(not character.isspace() for character in text)
        if characters < max(300, pages * 120):
            continue
        records.append(
            {
                "path": path,
                "pages": pages,
                "characters": characters,
                "sizeBytes": path.stat().st_size,
            }
        )
    return records


def _select_samples(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按固定页数目标选择十份文本密度有差异的真实简历。

    Args:
        records (list[dict[str, Any]]): 文本型 PDF 候选列表。

    Returns:
        list[dict[str, Any]]: 已附加匿名编号、长度分组和文件摘要的十份样本。

    Raises:
        ValueError: 任一目标页数没有足够候选文件时抛出。

    Note:
        1页和2页分别选择中位文本量及最大文本量，其余页数选择中位文本量，既
        覆盖常规样本，也覆盖单页和双页高密度简历。
    """
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[int(record["pages"])].append(record)
    for page_records in grouped.values():
        page_records.sort(key=lambda item: (item["characters"], str(item["path"])))

    selected: list[dict[str, Any]] = []
    duplicate_index: dict[int, int] = defaultdict(int)
    for sample_index, pages in enumerate(PAGE_TARGETS, start=1):
        candidates = grouped.get(pages, [])
        if not candidates:
            raise ValueError(f"缺少 {pages} 页文本型 PDF 样本")
        occurrence = duplicate_index[pages]
        duplicate_index[pages] += 1
        position = len(candidates) // 2 if occurrence == 0 else len(candidates) - 1
        source = dict(candidates[position])
        source.update(
            {
                "sampleId": f"real-{sample_index:02d}",
                "alias": f"real-bench-{sample_index:02d}-p{pages}.pdf",
                "length": "short" if pages <= 2 else "medium" if pages <= 5 else "long",
                "sha256": _sha256(source["path"]),
            }
        )
        selected.append(source)
    return selected


def _resume_counts(resume: dict[str, Any]) -> dict[str, Any]:
    """生成不包含候选人隐私的结构化结果摘要。

    Args:
        resume (dict[str, Any]): 任一解析链路输出的标准简历对象。

    Returns:
        dict[str, Any]: 基础字段存在性和三类列表数量。
    """
    candidate = resume.get("candidate") or {}
    educations = resume.get("educations") or []
    return {
        "namePresent": bool(candidate.get("name")),
        "contactPresent": bool(candidate.get("phone") or candidate.get("email")),
        "schoolPresent": any(
            isinstance(item, dict) and item.get("school") for item in educations
        ),
        "educations": len(educations),
        "works": len(resume.get("works") or []),
        "projects": len(resume.get("projects") or []),
    }


def _normalize_private_value(value: Any) -> str:
    """规范化仅用于私有结果一致性比较的标量。

    Args:
        value (Any): 候选人或机构字段值。

    Returns:
        str: 忽略大小写、全半角和空白差异的字符串。
    """
    import unicodedata

    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", str(value)).casefold()
    return "".join(normalized.split())


def _set_jaccard(left: set[str], right: set[str]) -> float:
    """计算两个机构名称集合的 Jaccard 一致率。

    Args:
        left (set[str]): 当前项目输出的规范化机构集合。
        right (set[str]): `profile-parse` 输出的规范化机构集合。

    Returns:
        float: 0到1之间的一致率；两个集合均为空时返回1。
    """
    if not left and not right:
        return 1.0
    return len(left & right) / len(left | right)


def _agreement(current: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    """比较两种输出的核心字段一致性，但不将一致率当作准确率。

    Args:
        current (dict[str, Any]): 当前项目解析结果。
        profile (dict[str, Any]): `profile-parse` 解析结果。

    Returns:
        dict[str, Any]: 基础字段精确一致性及学校、公司集合 Jaccard 一致率。

    Note:
        两个模型结果可能同时错误，真实准确率仍需要人工金标准。
    """
    current_candidate = current.get("candidate") or {}
    profile_candidate = profile.get("candidate") or {}
    candidate_fields = ("name", "phone", "email")
    candidate_matches = {
        field: _normalize_private_value(current_candidate.get(field))
        == _normalize_private_value(profile_candidate.get(field))
        for field in candidate_fields
    }

    def institution_set(resume: dict[str, Any], section: str, field: str) -> set[str]:
        """提取指定列表中的非空机构名称集合。

        Args:
            resume (dict[str, Any]): 标准简历对象。
            section (str): `educations` 或 `works` 列表字段。
            field (str): `school` 或 `company` 机构字段。

        Returns:
            set[str]: 规范化并去重后的非空机构名称。
        """
        return {
            normalized
            for item in resume.get(section) or []
            if isinstance(item, dict)
            and (normalized := _normalize_private_value(item.get(field)))
        }

    current_schools = institution_set(current, "educations", "school")
    profile_schools = institution_set(profile, "educations", "school")
    current_companies = institution_set(current, "works", "company")
    profile_companies = institution_set(profile, "works", "company")
    return {
        "candidate": candidate_matches,
        "candidateAgreement": round(
            sum(candidate_matches.values()) / len(candidate_matches), 4
        ),
        "educationInstitutionJaccard": round(
            _set_jaccard(current_schools, profile_schools), 4
        ),
        "workCompanyJaccard": round(
            _set_jaccard(current_companies, profile_companies), 4
        ),
    }


def _call_current_endpoint(
    endpoint: str,
    sample: dict[str, Any],
    timeout: int,
) -> dict[str, Any]:
    """通过 Base64 输入调用已部署的当前同步仅解析接口。

    Args:
        endpoint (str): `/api/resume/sync/parse` 完整 URL。
        sample (dict[str, Any]): 包含真实路径和匿名文件名的样本。
        timeout (int): 单份请求超时秒数。

    Returns:
        dict[str, Any]: 端到端耗时、公开结构摘要和私有完整结果。

    Raises:
        RuntimeError: HTTP响应或单项业务码表示解析失败时抛出。
    """
    content = base64.b64encode(sample["path"].read_bytes()).decode("ascii")
    payload = {
        "resumes": [
            {
                "type": "base64",
                "file": {
                    "name": sample["alias"],
                    "content": content,
                    "extra": sample["sampleId"],
                },
            }
        ],
        "max_concurrency": 1,
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    started = time.perf_counter()
    with opener.open(request, timeout=timeout) as response:
        body = json.loads(response.read().decode("utf-8"))
    wall_seconds = time.perf_counter() - started
    items = body.get("results") or []
    first = items[0] if items else {}
    if first.get("code") != 0:
        raise RuntimeError(first.get("msg") or "当前同步接口解析失败")
    resume = first["data"]["resume"]
    return {
        "status": "success",
        "wallSeconds": round(wall_seconds, 3),
        "apiSeconds": body.get("elapsed_s"),
        "counts": _resume_counts(resume),
        "resume": resume,
    }


class ProfileParseRunner:
    """为 `profile-parse` 生产函数补充单轮私有基准指标。

    Attributes:
        profile_root (Path): `profile-parse` 项目根目录。

    Note:
        包装器不改变请求参数和解析结果，只记录两路并发调用各自的耗时及 token。
    """

    def __init__(self, profile_root: Path) -> None:
        """导入参考项目并安装进程内指标包装器。

        Args:
            profile_root (Path): `profile-parse` 项目根目录。

        Raises:
            ModuleNotFoundError: 参考项目或其依赖无法导入时抛出。
        """
        self.profile_root = profile_root
        sys.path.insert(0, str(profile_root))
        from app import document, parser

        self.document = document
        self.parser = parser
        self._metrics_var: contextvars.ContextVar[dict[str, Any] | None] = (
            contextvars.ContextVar("profile_metrics", default=None)
        )
        self._branch_var: contextvars.ContextVar[str] = contextvars.ContextVar(
            "profile_branch", default="unknown"
        )
        self._original_section = parser._parse_section
        self._original_create = parser.client.chat.completions.create
        parser._parse_section = self._tracked_section
        parser.client.chat.completions.create = self._tracked_create

    async def _tracked_section(
        self,
        name: str,
        prompt: str,
        resume_text: str,
    ) -> dict:
        """记录 `profile-parse` 单个语义分支的墙钟耗时。

        Args:
            name (str): 基础教育或工作项目分支名称。
            prompt (str): 参考项目原始系统 Prompt。
            resume_text (str): Python 抽取的完整简历文本。

        Returns:
            dict: 参考项目原函数生成的分支 JSON 对象。
        """
        branch_token = self._branch_var.set(name)
        started = time.perf_counter()
        try:
            return await self._original_section(name, prompt, resume_text)
        finally:
            metrics = self._metrics_var.get()
            if metrics is not None:
                metrics.setdefault("branches", {}).setdefault(name, {})[
                    "wallSeconds"
                ] = round(time.perf_counter() - started, 3)
            self._branch_var.reset(branch_token)

    async def _tracked_create(self, *args, **kwargs):
        """保留原模型调用并采集对应分支的 OpenAI usage。

        Args:
            *args: 原 `chat.completions.create` 位置参数。
            **kwargs: 原 `chat.completions.create` 关键字参数。

        Returns:
            Any: OpenAI SDK 原始响应对象。
        """
        response = await self._original_create(*args, **kwargs)
        metrics = self._metrics_var.get()
        usage = getattr(response, "usage", None)
        if metrics is not None and usage is not None:
            branch_metrics = metrics.setdefault("branches", {}).setdefault(
                self._branch_var.get(), {}
            )
            branch_metrics.update(
                {
                    "promptTokens": usage.prompt_tokens,
                    "completionTokens": usage.completion_tokens,
                    "totalTokens": usage.total_tokens,
                }
            )
        return response

    async def parse(self, sample: dict[str, Any]) -> dict[str, Any]:
        """执行一份真实 PDF 的参考项目生产解析链路。

        Args:
            sample (dict[str, Any]): 包含真实路径和匿名文件名的样本。

        Returns:
            dict[str, Any]: 抽取、两路模型、总耗时、token和完整私有结果。
        """
        metrics: dict[str, Any] = {"branches": {}}
        metrics_token = self._metrics_var.set(metrics)
        try:
            content = sample["path"].read_bytes()
            total_started = time.perf_counter()
            extraction_started = time.perf_counter()
            text = await asyncio.to_thread(
                self.document.extract_document_text,
                content,
                ".pdf",
            )
            extraction_seconds = time.perf_counter() - extraction_started
            llm_started = time.perf_counter()
            model = await self.parser.parse_resume_text(text)
            llm_seconds = time.perf_counter() - llm_started
            total_seconds = time.perf_counter() - total_started
        finally:
            self._metrics_var.reset(metrics_token)
        resume = model.model_dump()
        branch_values = list(metrics["branches"].values())
        return {
            "status": "success",
            "extractedCharacters": len(text),
            "extractionSeconds": round(extraction_seconds, 3),
            "llmSeconds": round(llm_seconds, 3),
            "wallSeconds": round(total_seconds, 3),
            "llmWallShare": round(llm_seconds / total_seconds, 4),
            "branches": metrics["branches"],
            "promptTokens": sum(item.get("promptTokens", 0) for item in branch_values),
            "completionTokens": sum(
                item.get("completionTokens", 0) for item in branch_values
            ),
            "totalTokens": sum(item.get("totalTokens", 0) for item in branch_values),
            "counts": _resume_counts(resume),
            "resume": resume,
        }


def _load_current_log_metrics(
    log_root: Path,
    aliases: set[str],
) -> dict[str, dict[str, Any]]:
    """从容器挂载日志提取当前接口的模型和本地阶段指标。

    Args:
        log_root (Path): 当前容器调试日志根目录。
        aliases (set[str]): 本轮匿名文件名集合。

    Returns:
        dict[str, dict[str, Any]]: 匿名文件名到抽取、token、LLM和JSON耗时的映射。
    """
    metrics: dict[str, dict[str, Any]] = defaultdict(dict)
    paths = sorted(
        log_root.rglob("debug*.log"),
        key=lambda path: (path.stat().st_mtime_ns, str(path)),
    )
    for path in paths:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            alias = next((name for name in aliases if f"[{name}]" in line), None)
            if alias is None:
                continue
            elapsed_match = _ELAPSED_RE.search(line)
            if "[Python文本抽取完成]" in line and elapsed_match:
                metrics[alias]["extractionSeconds"] = float(
                    elapsed_match.group("value")
                )
            elif "[LLM单模型调用完成]" in line and elapsed_match:
                token_match = _TOKEN_RE.search(line)
                metrics[alias]["llmSeconds"] = float(elapsed_match.group("value"))
                if token_match:
                    metrics[alias].update(
                        {
                            "promptTokens": int(token_match.group("prompt")),
                            "completionTokens": int(token_match.group("completion")),
                            "totalTokens": int(token_match.group("total")),
                        }
                    )
            elif "[本地JSON解码与契约校验完成]" in line:
                json_match = _JSON_TIMING_RE.search(line)
                if json_match:
                    metrics[alias].update(
                        {
                            "jsonDecodeSeconds": float(json_match.group("decode")),
                            "postProcessSeconds": float(
                                json_match.group("post_process")
                            ),
                            "jsonConversionSeconds": float(
                                json_match.group("conversion")
                            ),
                        }
                    )
    return dict(metrics)


def _duration_summary(values: list[float]) -> dict[str, float]:
    """汇总成功样本耗时的均值、中位数和范围。

    Args:
        values (list[float]): 成功请求的秒数列表。

    Returns:
        dict[str, float]: mean、median、min和max；空列表返回空对象。
    """
    if not values:
        return {}
    return {
        "mean": round(statistics.mean(values), 3),
        "median": round(statistics.median(values), 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
    }


def _aggregate_report(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """构造两种链路的总体和长度分组统计。

    Args:
        samples (list[dict[str, Any]]): 已完成两种解析的私有样本结果。

    Returns:
        dict[str, Any]: 成功率、耗时、LLM占比、token和输出一致性汇总。
    """
    implementations = ("current", "profile")
    summary: dict[str, Any] = {}
    for implementation in implementations:
        successful = [
            sample[implementation]
            for sample in samples
            if sample[implementation].get("status") == "success"
        ]
        llm_values = [
            item["llmSeconds"] for item in successful if "llmSeconds" in item
        ]
        total_values = [item["wallSeconds"] for item in successful]

        def metric_total(item: dict[str, Any], key: str) -> int:
            """读取顶层token，兼容首版私有报告中的分支级token。

            Args:
                item (dict[str, Any]): 单种解析实现的成功结果。
                key (str): promptTokens、completionTokens或totalTokens。

            Returns:
                int: 顶层值或两路分支值之和。
            """
            if key in item:
                return int(item.get(key) or 0)
            return sum(
                int(branch.get(key) or 0)
                for branch in (item.get("branches") or {}).values()
            )

        prompt_tokens = sum(metric_total(item, "promptTokens") for item in successful)
        completion_tokens = sum(
            metric_total(item, "completionTokens") for item in successful
        )
        branch_wall_seconds = sum(
            sum(
                float(branch.get("wallSeconds") or 0)
                for branch in (item.get("branches") or {}).values()
            )
            if item.get("branches")
            else float(item.get("llmSeconds") or 0)
            for item in successful
        )
        summary[implementation] = {
            "successCount": len(successful),
            "failedCount": len(samples) - len(successful),
            "wallDuration": _duration_summary(total_values),
            "llmDuration": _duration_summary(llm_values),
            "llmWallShare": round(sum(llm_values) / sum(total_values), 4)
            if llm_values and total_values
            else None,
            "llmRequestCount": sum(
                len(item.get("branches") or {}) or 1 for item in successful
            ),
            "promptTokens": prompt_tokens,
            "completionTokens": completion_tokens,
            "totalTokens": prompt_tokens + completion_tokens,
            "branchWallSecondsSum": round(branch_wall_seconds, 3),
        }

    by_length: dict[str, Any] = {}
    for length in ("short", "medium", "long"):
        grouped = [sample for sample in samples if sample["length"] == length]
        by_length[length] = {}
        for implementation in implementations:
            values = [
                sample[implementation]["wallSeconds"]
                for sample in grouped
                if sample[implementation].get("status") == "success"
            ]
            by_length[length][implementation] = _duration_summary(values)

    agreements = [sample["agreement"] for sample in samples if sample.get("agreement")]
    summary["agreement"] = {
        "candidateMean": round(
            statistics.mean(item["candidateAgreement"] for item in agreements), 4
        )
        if agreements
        else None,
        "educationInstitutionJaccardMean": round(
            statistics.mean(
                item["educationInstitutionJaccard"] for item in agreements
            ),
            4,
        )
        if agreements
        else None,
        "workCompanyJaccardMean": round(
            statistics.mean(item["workCompanyJaccard"] for item in agreements), 4
        )
        if agreements
        else None,
    }
    summary["byLength"] = by_length
    return summary


def _format_seconds(value: Any) -> str:
    """将可空秒数格式化为三位小数。

    Args:
        value (Any): 秒数或空值。

    Returns:
        str: 带`s`后缀的展示文本；空值返回`-`。
    """
    return "-" if value is None else f"{float(value):.3f}s"


def _render_markdown(report: dict[str, Any]) -> str:
    """生成不包含原始文件名和解析正文的匿名 Markdown 报告。

    Args:
        report (dict[str, Any]): 完整私有基准报告。

    Returns:
        str: 可提交的匿名耗时与一致性报告。
    """
    summary = report["summary"]
    current = summary["current"]
    profile = summary["profile"]
    current_mean = current["wallDuration"]["mean"]
    profile_mean = profile["wallDuration"]["mean"]
    mean_reduction = (current_mean - profile_mean) / current_mean
    profile_token_increase = (
        profile["totalTokens"] - current["totalTokens"]
    ) / current["totalTokens"]
    lines = [
        "# 真实简历同步解析方式对比报告",
        "",
        f"测试时间：{report['completedAt']}",
        "",
        "## 测试口径",
        "",
        "1. 从本地真实简历目录按页数分层选择10份文本型PDF。",
        "2. 1页和2页各2份，3至8页各1份；逐份串行且交替调用两种链路。",
        "3. 两种链路使用同一个模型；扫描件未纳入，因为profile-parse不支持视觉降级。",
        "4. 原始文件名、完整解析结果和哈希仅保存在Git忽略的私有报告中。",
        "5. 本轮没有人工标注，输出一致率不是准确率，不能替代黄金集评测。",
        "",
        "## 总体结果",
        "",
        "| 方案 | 成功 | 平均耗时 | 中位耗时 | 最短 | 最长 | LLM墙钟占比 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        (
            f"| AI-Resume-ParserTag | {current['successCount']}/10 | "
            f"{_format_seconds(current['wallDuration'].get('mean'))} | "
            f"{_format_seconds(current['wallDuration'].get('median'))} | "
            f"{_format_seconds(current['wallDuration'].get('min'))} | "
            f"{_format_seconds(current['wallDuration'].get('max'))} | "
            f"{(current['llmWallShare'] or 0) * 100:.2f}% |"
        ),
        (
            f"| profile-parse | {profile['successCount']}/10 | "
            f"{_format_seconds(profile['wallDuration'].get('mean'))} | "
            f"{_format_seconds(profile['wallDuration'].get('median'))} | "
            f"{_format_seconds(profile['wallDuration'].get('min'))} | "
            f"{_format_seconds(profile['wallDuration'].get('max'))} | "
            f"{(profile['llmWallShare'] or 0) * 100:.2f}% |"
        ),
        "",
        f"单轮平均值中，profile-parse墙钟降低`{mean_reduction * 100:.2f}%`，"
        f"但两路合计token增加`{profile_token_increase * 100:.2f}%`。",
        "",
        "## LLM用量",
        "",
        "| 方案 | 模型请求数 | Prompt tokens | Completion tokens | 总tokens | 分支调用耗时之和 |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
        (
            f"| AI-Resume-ParserTag | {current['llmRequestCount']} | "
            f"{current['promptTokens']} | {current['completionTokens']} | "
            f"{current['totalTokens']} | "
            f"{_format_seconds(current['branchWallSecondsSum'])} |"
        ),
        (
            f"| profile-parse | {profile['llmRequestCount']} | "
            f"{profile['promptTokens']} | {profile['completionTokens']} | "
            f"{profile['totalTokens']} | "
            f"{_format_seconds(profile['branchWallSecondsSum'])} |"
        ),
        "",
        "profile-parse的分支调用耗时之和不是GPU计费时间，只用于说明并发墙钟隐藏了"
        "第二路请求；真实GPU资源消耗需由模型服务指标确认。",
        "",
        "## 逐份结果",
        "",
        "| 样本 | 页数 | 有效字符 | 长度 | 当前抽取 | 当前LLM | 当前总耗时 | "
        "profile抽取 | profile两路LLM墙钟 | profile总耗时 |",
        "| --- | ---: | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for sample in report["samples"]:
        current_item = sample["current"]
        profile_item = sample["profile"]
        lines.append(
            f"| {sample['sampleId']} | {sample['pages']} | {sample['characters']} | "
            f"{sample['length']} | "
            f"{_format_seconds(current_item.get('extractionSeconds'))} | "
            f"{_format_seconds(current_item.get('llmSeconds'))} | "
            f"{_format_seconds(current_item.get('wallSeconds'))} | "
            f"{_format_seconds(profile_item.get('extractionSeconds'))} | "
            f"{_format_seconds(profile_item.get('llmSeconds'))} | "
            f"{_format_seconds(profile_item.get('wallSeconds'))} |"
        )
    lines.extend(
        [
            "",
            "## 长度分组",
            "",
            "| 长度 | 样本数 | 当前平均 | profile平均 |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    length_counts = {length: 0 for length in ("short", "medium", "long")}
    for sample in report["samples"]:
        length_counts[sample["length"]] += 1
    for length in ("short", "medium", "long"):
        grouped = summary["byLength"][length]
        lines.append(
            f"| {length} | {length_counts[length]} | "
            f"{_format_seconds(grouped['current'].get('mean'))} | "
            f"{_format_seconds(grouped['profile'].get('mean'))} |"
        )
    agreement = summary["agreement"]
    lines.extend(
        [
            "",
            "## 结构数量对比",
            "",
            "| 样本 | 当前E/W/P | profile E/W/P | 基础字段一致率 | 学校一致率 | 公司一致率 |",
            "| --- | --- | --- | ---: | ---: | ---: |",
        ]
    )
    for sample in report["samples"]:
        current_counts = sample["current"].get("counts") or {}
        profile_counts = sample["profile"].get("counts") or {}
        item_agreement = sample.get("agreement") or {}
        lines.append(
            f"| {sample['sampleId']} | "
            f"{current_counts.get('educations', '-')}/"
            f"{current_counts.get('works', '-')}/"
            f"{current_counts.get('projects', '-')} | "
            f"{profile_counts.get('educations', '-')}/"
            f"{profile_counts.get('works', '-')}/"
            f"{profile_counts.get('projects', '-')} | "
            f"{float(item_agreement.get('candidateAgreement', 0)) * 100:.2f}% | "
            f"{float(item_agreement.get('educationInstitutionJaccard', 0)) * 100:.2f}% | "
            f"{float(item_agreement.get('workCompanyJaccard', 0)) * 100:.2f}% |"
        )
    lines.extend(
        [
            "",
            "## 输出一致性",
            "",
            f"- 姓名/电话/邮箱平均一致率：{agreement['candidateMean'] * 100:.2f}%",
            "- 教育机构集合平均Jaccard一致率："
            f"{agreement['educationInstitutionJaccardMean'] * 100:.2f}%",
            "- 工作公司集合平均Jaccard一致率："
            f"{agreement['workCompanyJaccardMean'] * 100:.2f}%",
            "- 一致率仅用于发现两种实现的结构差异，不表示任一结果正确。",
            "",
            "## 解释限制",
            "",
            "- 每份样本只运行一次，模型排队和生成长度会造成明显波动。",
            "- profile-parse的两路LLM耗时是并发墙钟时间，实际模型资源消耗应按两路相加。",
            "- 当前接口的墙钟包含Base64请求解析；profile-parse按其业务elapsed口径从文本抽取开始计时。",
            "- 发布决策应至少重复3轮，并对真实简历补充人工字段标注。",
            "",
        ]
    )
    return "\n".join(lines)


def rebuild_report(private_report: Path, markdown_report: Path) -> dict[str, Any]:
    """从已有私有测量重新计算汇总并生成匿名报告。

    Args:
        private_report (Path): 包含完整样本测量的Git忽略JSON路径。
        markdown_report (Path): 不包含PII的Markdown输出路径。

    Returns:
        dict[str, Any]: 已更新token、耗时和一致性汇总的私有报告。

    Raises:
        ValueError: 私有报告顶层或samples字段不是预期结构时抛出。
    """
    report = json.loads(private_report.read_text(encoding="utf-8"))
    if not isinstance(report, dict) or not isinstance(report.get("samples"), list):
        raise ValueError("私有报告必须包含samples数组")
    report["summary"] = _aggregate_report(report["samples"])
    private_report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    markdown_report.parent.mkdir(parents=True, exist_ok=True)
    markdown_report.write_text(_render_markdown(report), encoding="utf-8")
    return report


async def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    """执行十份真实简历的交替串行比较并写入私有和匿名报告。

    Args:
        args (argparse.Namespace): 路径、接口和超时命令行参数。

    Returns:
        dict[str, Any]: 包含完整私有结果及匿名汇总的基准报告。
    """
    environment = _read_env(PROJECT_ROOT / ".env")
    model_name = _configure_profile_environment(environment)
    records = _inventory_text_pdfs(args.resume_root)
    selected = _select_samples(records)
    profile_runner = ProfileParseRunner(args.profile_root)
    started_at = datetime.now().astimezone()
    samples: list[dict[str, Any]] = []

    for index, sample in enumerate(selected, start=1):
        result: dict[str, Any] = {
            "sampleId": sample["sampleId"],
            "alias": sample["alias"],
            "sourceFile": str(sample["path"]),
            "sha256": sample["sha256"],
            "pages": sample["pages"],
            "characters": sample["characters"],
            "sizeBytes": sample["sizeBytes"],
            "length": sample["length"],
        }

        async def run_current() -> None:
            """调用当前接口并将失败归一为单样本错误。"""
            try:
                result["current"] = await asyncio.to_thread(
                    _call_current_endpoint,
                    args.endpoint,
                    sample,
                    args.timeout,
                )
            except Exception as exc:
                result["current"] = {
                    "status": "failed",
                    "errorType": type(exc).__name__,
                    "error": str(exc),
                }

        async def run_profile() -> None:
            """调用参考链路并将失败归一为单样本错误。"""
            try:
                result["profile"] = await profile_runner.parse(sample)
            except Exception as exc:
                result["profile"] = {
                    "status": "failed",
                    "errorType": type(exc).__name__,
                    "error": str(exc),
                }

        if index % 2:
            await run_current()
            await run_profile()
        else:
            await run_profile()
            await run_current()
        if (
            result["current"].get("status") == "success"
            and result["profile"].get("status") == "success"
        ):
            result["agreement"] = _agreement(
                result["current"]["resume"],
                result["profile"]["resume"],
            )
        samples.append(result)
        print(
            f"[{index}/10] {sample['sampleId']} pages={sample['pages']} "
            f"current={result['current'].get('wallSeconds', 'failed')}s "
            f"profile={result['profile'].get('wallSeconds', 'failed')}s",
            flush=True,
        )

    current_metrics = _load_current_log_metrics(
        args.log_root,
        {sample["alias"] for sample in selected},
    )
    for sample in samples:
        if sample["current"].get("status") != "success":
            continue
        sample["current"].update(current_metrics.get(sample["alias"], {}))
        llm_seconds = sample["current"].get("llmSeconds")
        if llm_seconds is not None:
            sample["current"]["llmWallShare"] = round(
                llm_seconds / sample["current"]["wallSeconds"], 4
            )

    completed_at = datetime.now().astimezone()
    report = {
        "schemaVersion": "1.0",
        "startedAt": started_at.isoformat(timespec="seconds"),
        "completedAt": completed_at.isoformat(timespec="seconds"),
        "model": model_name,
        "selection": {
            "sourceDirectory": str(args.resume_root),
            "availableTextPdfCount": len(records),
            "pageTargets": list(PAGE_TARGETS),
            "alternatingOrder": True,
        },
        "samples": samples,
    }
    report["summary"] = _aggregate_report(samples)
    args.private_report.parent.mkdir(parents=True, exist_ok=True)
    args.private_report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    args.markdown_report.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_report.write_text(_render_markdown(report), encoding="utf-8")
    print(f"私有报告: {args.private_report}")
    print(f"匿名报告: {args.markdown_report}")
    return report


def _build_parser() -> argparse.ArgumentParser:
    """构造真实简历比较脚本的命令行参数。

    Returns:
        argparse.ArgumentParser: 已配置目录、接口、超时和报告路径的解析器。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume-root", type=Path, default=DEFAULT_RESUME_ROOT)
    parser.add_argument("--profile-root", type=Path, default=DEFAULT_PROFILE_ROOT)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument(
        "--private-report",
        type=Path,
        default=DEFAULT_PRIVATE_REPORT,
    )
    parser.add_argument(
        "--markdown-report",
        type=Path,
        default=DEFAULT_MARKDOWN_REPORT,
    )
    parser.add_argument("--log-root", type=Path, default=DEFAULT_LOG_ROOT)
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="不调用模型，仅从已有私有测量重新生成汇总和匿名报告",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """执行真实简历同步解析比较。

    Args:
        argv (Sequence[str] | None): 可选命令行参数；为空时读取进程参数。

    Returns:
        int: 测试和报告写入成功返回0；异常由调用环境返回非零状态。
    """
    args = _build_parser().parse_args(argv)
    if args.report_only:
        rebuild_report(args.private_report, args.markdown_report)
    else:
        asyncio.run(run_benchmark(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
