"""
tag 包公开接口。

其他模块通过此文件 import，不直接依赖包内私有函数：
  from tag import run_tagging, load_tag_cfg
"""
from .tagger import (
    run_tagging,
    extract_positive_tags_llm,
    extract_positive_tags_rule,
    detect_sensitive_tags,
    _tagger_load_cfg as load_tag_cfg,   # 公开别名，供外部读取运行时配置
)

__all__ = [
    "run_tagging",
    "extract_positive_tags_llm",
    "extract_positive_tags_rule",
    "detect_sensitive_tags",
    "load_tag_cfg",
]
