"""验证真实简历双链路基准的抽样、聚合和匿名报告契约。"""

from pathlib import Path

from scripts.benchmark_real_resume_comparison import (
    PAGE_TARGETS,
    _aggregate_report,
    _render_markdown,
    _select_samples,
)


def _records(tmp_path: Path) -> list[dict]:
    """构造覆盖目标页数的无隐私测试文件。

    Args:
        tmp_path (Path): Pytest 提供的临时目录。

    Returns:
        list[dict]: 每个目标页数包含中等和高文本量候选的记录列表。
    """
    records: list[dict] = []
    for pages in sorted(set(PAGE_TARGETS)):
        character_levels = (pages * 300, pages * 500, pages * 900)
        for variant, characters in enumerate(character_levels, start=1):
            path = tmp_path / f"page-{pages}-variant-{variant}.pdf"
            path.write_bytes(f"sample-{pages}-{variant}".encode("ascii"))
            records.append(
                {
                    "path": path,
                    "pages": pages,
                    "characters": characters,
                    "sizeBytes": path.stat().st_size,
                }
            )
    return records


def _successful_result(
    wall_seconds: float,
    llm_seconds: float,
    prompt_tokens: int,
    completion_tokens: int,
) -> dict:
    """构造当前链路使用的成功测量。

    Args:
        wall_seconds (float): 端到端墙钟耗时。
        llm_seconds (float): 模型调用墙钟耗时。
        prompt_tokens (int): 输入 token 数。
        completion_tokens (int): 输出 token 数。

    Returns:
        dict: 可由聚合器处理的成功测量。
    """
    return {
        "status": "success",
        "wallSeconds": wall_seconds,
        "llmSeconds": llm_seconds,
        "promptTokens": prompt_tokens,
        "completionTokens": completion_tokens,
        "counts": {"educations": 1, "works": 2, "projects": 3},
    }


def _profile_result() -> dict:
    """构造仅含分支级token的首版参考链路测量。

    Returns:
        dict: 用于验证旧私有报告兼容聚合的成功测量。
    """
    return {
        "status": "success",
        "wallSeconds": 4.0,
        "llmSeconds": 3.5,
        "branches": {
            "candidate": {
                "promptTokens": 70,
                "completionTokens": 10,
                "wallSeconds": 1.0,
            },
            "work": {
                "promptTokens": 80,
                "completionTokens": 20,
                "wallSeconds": 3.5,
            },
        },
        "counts": {"educations": 1, "works": 2, "projects": 3},
    }


def test_select_samples_uses_expected_page_layers(tmp_path: Path) -> None:
    """抽样结果应稳定覆盖1至8页及两份高密度短简历。"""
    selected = _select_samples(_records(tmp_path))

    assert [item["pages"] for item in selected] == list(PAGE_TARGETS)
    assert len({item["sampleId"] for item in selected}) == 10
    assert selected[1]["characters"] > selected[0]["characters"]
    assert selected[3]["characters"] > selected[2]["characters"]


def test_aggregate_report_sums_profile_branch_tokens() -> None:
    """参考链路应按20个分支请求累计token，不能只统计并发墙钟。"""
    samples = []
    for index, pages in enumerate(PAGE_TARGETS, start=1):
        samples.append(
            {
                "sampleId": f"real-{index:02d}",
                "pages": pages,
                "characters": pages * 1000,
                "length": "short" if pages <= 2 else "medium" if pages <= 5 else "long",
                "current": _successful_result(5.0, 4.0, 100, 40),
                "profile": _profile_result(),
                "agreement": {
                    "candidateAgreement": 1.0,
                    "educationInstitutionJaccard": 1.0,
                    "workCompanyJaccard": 1.0,
                },
            }
        )

    summary = _aggregate_report(samples)

    assert summary["current"]["llmRequestCount"] == 10
    assert summary["profile"]["llmRequestCount"] == 20
    assert summary["profile"]["promptTokens"] == 1500
    assert summary["profile"]["completionTokens"] == 300
    assert summary["profile"]["branchWallSecondsSum"] == 45.0


def test_markdown_report_does_not_render_private_source_path() -> None:
    """匿名报告不得展示真实文件名、完整路径或解析正文。"""
    samples = []
    for index, pages in enumerate(PAGE_TARGETS, start=1):
        samples.append(
            {
                "sampleId": f"real-{index:02d}",
                "sourceFile": "/private/张三.pdf",
                "pages": pages,
                "characters": pages * 1000,
                "length": "short" if pages <= 2 else "medium" if pages <= 5 else "long",
                "current": _successful_result(5.0, 4.0, 100, 40),
                "profile": {
                    **_profile_result(),
                    "promptTokens": 150,
                    "completionTokens": 30,
                },
                "agreement": {
                    "candidateAgreement": 1.0,
                    "educationInstitutionJaccard": 1.0,
                    "workCompanyJaccard": 1.0,
                },
            }
        )
    report = {
        "completedAt": "2026-08-05T16:00:00+08:00",
        "samples": samples,
        "summary": _aggregate_report(samples),
    }

    markdown = _render_markdown(report)

    assert "张三" not in markdown
    assert "/private/" not in markdown
    assert "real-01" in markdown
