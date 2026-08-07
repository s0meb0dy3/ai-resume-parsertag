"""导出并校验 FastAPI 自动生成的 OpenAPI 接口契约。

路由声明和 Pydantic Schema 是接口定义的唯一来源。本脚本把运行时
``app.openapi()`` 固化为仓库文件，并在 ``--check`` 模式下检测接口代码与已提交
规范是否发生漂移，供本地检查和 CI 直接调用。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT_DIR / "docs" / "openapi.json"

# 直接执行 ``python scripts/export_openapi.py`` 时，模块搜索根目录默认为
# ``scripts/``；显式加入项目根目录，确保脚本和测试使用同一应用入口。
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app import app  # noqa: E402


def build_openapi_document() -> dict:
    """从当前 FastAPI 应用生成 OpenAPI 文档。

    Returns:
        dict: 包含路径、请求 Schema、响应定义和标签元数据的 OpenAPI 对象。

    Note:
        函数直接调用 ``app.openapi()``，不启动服务生命周期，也不访问模型服务。
    """
    return app.openapi()


def serialize_openapi_document(document: dict) -> str:
    """以稳定格式序列化 OpenAPI 文档。

    Args:
        document (dict): FastAPI 生成的 OpenAPI 对象。

    Returns:
        str: 键排序、UTF-8 中文直出的格式化 JSON，并保留文件末尾换行。
    """
    return json.dumps(
        document,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def export_openapi_document(output_path: Path) -> None:
    """把当前接口契约写入指定文件。

    Args:
        output_path (Path): OpenAPI JSON 输出路径。

    Raises:
        OSError: 目录创建或文件写入失败时抛出。
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        serialize_openapi_document(build_openapi_document()),
        encoding="utf-8",
    )


def check_openapi_document(output_path: Path) -> bool:
    """检查已提交规范是否与当前应用生成结果完全一致。

    Args:
        output_path (Path): 待检查的 OpenAPI JSON 文件。

    Returns:
        bool: 文件存在、可解析且内容与当前应用一致时返回 ``True``。
    """
    if not output_path.is_file():
        return False
    try:
        committed_document = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return committed_document == build_openapi_document()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """解析导出路径和校验模式参数。

    Args:
        argv (Sequence[str] | None): 可选命令行参数；为空时读取进程参数。

    Returns:
        argparse.Namespace: 包含 ``output`` 和 ``check`` 的参数对象。
    """
    parser = argparse.ArgumentParser(
        description="导出或校验 AI-Resume-ParserTag OpenAPI 文档",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"输出文件，默认 {DEFAULT_OUTPUT}",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只检查已提交文件是否与当前代码一致，不写文件",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """执行 OpenAPI 导出或一致性校验。

    Args:
        argv (Sequence[str] | None): 可选命令行参数，主要供自动化测试使用。

    Returns:
        int: 成功返回 0；校验发现文件缺失或接口漂移时返回 1。
    """
    args = parse_args(argv)
    output_path = args.output.resolve()
    if args.check:
        if check_openapi_document(output_path):
            print(f"OpenAPI 契约一致: {output_path}")
            return 0
        print(
            f"OpenAPI 契约已变化: {output_path}\n"
            "请运行 python scripts/export_openapi.py 更新后提交。"
        )
        return 1

    export_openapi_document(output_path)
    print(f"OpenAPI 契约已导出: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
