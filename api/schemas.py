"""
API 请求 Pydantic 模型。

所有路由的 Body 模型统一在此定义，避免直接接收 dict，提供：
  - 自动请求校验 + 422 错误提示
  - Swagger UI 完整文档
  - 强类型支持
"""
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ── 共享基础 ─────────────────────────────────────────────────────


class _JobContext(BaseModel):
    """岗位上下文，可选，影响打标结果。"""
    jobTitle:          str = Field("", description="岗位名称")
    requiredSkills:    str = Field("", description="技能要求，逗号分隔")
    jobDescription:    str = Field("", description="岗位 JD")
    jobSensitiveWords: str = Field("", description="风险关键词，逗号分隔")

    def to_job_info(self) -> dict:
        return {k: getattr(self, k) or ""
                for k in ("jobTitle", "requiredSkills", "jobDescription", "jobSensitiveWords")}


class FileMeta(BaseModel):
    """URL 方式简历的文件元信息。"""
    name:  str           = Field("resume.pdf", description="文件名，用于日志和结果标识")
    path:  str           = Field("",           description="可访问的 HTTP/HTTPS 下载链接")
    extra: Optional[str] = Field(None,         description="调用方业务标识，结果查询时原样返回")


class ResumeUrlItem(BaseModel):
    """URL 方式简历条目（type 必须为 url）。"""
    type:  Literal["url"]
    file:  FileMeta      = Field(default_factory=FileMeta)
    extra: Optional[str] = Field(None, description="顶层 extra，优先级低于 file.extra")

    @property
    def effective_extra(self) -> Optional[str]:
        """file.extra 优先，其次顶层 extra。"""
        return self.file.extra or self.extra or None

    @property
    def filename(self) -> str:
        return self.file.name or "resume.pdf"


class ParsedResumeItem(BaseModel):
    """已解析简历条目，作为仅打标接口的输入。"""
    filename:              str            = Field("resume.pdf", description="文件名")
    extra:                 Optional[str]  = Field(None, description="调用方业务标识")
    resumeParseFinishTime: str            = Field("",   description="解析完成时间")
    resume:                Dict[str, Any] = Field(...,  description="解析后的结构化简历 JSON")


# ── 异步接口请求模型 ─────────────────────────────────────────────


class AsyncBatchRequest(_JobContext):
    """
    POST /api/resume/async/batch 和 /api/resume/async/batch/parse 请求体。
    单次最多 20 份，仅支持 type=url。
    """
    resumes: List[ResumeUrlItem] = Field(..., min_length=1, max_length=20,
                                         description="简历列表，每项必须含 type=url 和 file.path")


class AsyncTagBatchRequest(_JobContext):
    """
    POST /api/resume/async/batch/tag 请求体。
    单次最多 20 份，resume 字段必须是已解析的结构化 JSON。
    """
    resumes: List[ParsedResumeItem] = Field(..., min_length=1, max_length=20,
                                            description="已解析简历列表，每项须含 resume 字段")


# ── 同步接口请求模型 ─────────────────────────────────────────────


class SyncBatchRequest(_JobContext):
    """
    POST /api/resume/sync/batch 和 /api/resume/sync/parse 请求体。
    resumes 支持 type=url/file/base64，item 结构灵活，由 get_pdf_source_from_content 解析。
    """
    resumes:         List[Any]     = Field(..., min_length=1, max_length=50,
                                           description="简历列表，支持 url / file / base64 三种方式")
    max_concurrency: Optional[int] = Field(None, ge=1, le=30, description="并发上限（1~30）")


class SyncTagRequest(_JobContext):
    """
    POST /api/resume/sync/tag 请求体。
    单次最多 50 份，resume 字段必须是已解析的结构化 JSON。
    """
    resumes:         List[ParsedResumeItem] = Field(..., min_length=1, max_length=50,
                                                    description="已解析简历列表")
    max_concurrency: Optional[int]          = Field(None, ge=1, le=30, description="并发上限（1~30）")


# ── 打标配置接口请求模型 ─────────────────────────────────────────


class AddPositiveDimensionBody(BaseModel):
    dimension:     str       = Field(..., min_length=1, description="维度标识，全局唯一，建议英文")
    displayName:   str       = Field("",  description="展示名称，默认同 dimension")
    enabled:       bool      = Field(True, description="是否启用")
    allowedValues: List[str] = Field(default_factory=list, description="允许值枚举，空列表表示不限")


class UpdatePositiveDimensionBody(BaseModel):
    displayName:   Optional[str]       = Field(None, description="展示名称")
    enabled:       Optional[bool]      = Field(None, description="是否启用")
    allowedValues: Optional[List[str]] = Field(None, description="允许值枚举")


class AddNegativeDimensionBody(BaseModel):
    """新增负向维度。

    detectionType 控制检测方式：
      rule（默认）：关键词规则匹配，rules 中每条规则需提供 keywords + 可选 searchIn
      llm         ：大模型语义检测，rules 中每条规则需提供 description 描述检测标准
    """
    dimension:     str                      = Field(..., min_length=1, description="维度标识，全局唯一，建议英文")
    displayName:   str                      = Field("",  description="展示名称，默认同 dimension")
    enabled:       bool                     = Field(True, description="是否启用")
    detectionType: Literal["rule", "llm"]   = Field("rule",
                                                     description="检测方式：rule=关键词规则 / llm=大模型语义")
    rules:         Optional[Dict[str, Any]] = Field(default_factory=dict,
                                                    description="规则字典，key 为 ruleKey")


class UpdateNegativeDimensionBody(BaseModel):
    displayName: Optional[str]  = Field(None, description="展示名称")
    enabled:     Optional[bool] = Field(None, description="是否启用")


class AddNegativeRuleBody(BaseModel):
    """
    其他字段（keywords / pattern / matchFields 等）通过 extra="allow" 透传存入 rules。
    """
    model_config = ConfigDict(extra="allow")

    ruleKey:  str           = Field(..., min_length=1, description="规则唯一标识，建议英文")
    tagName:  Optional[str] = Field(None,     description="标签展示名称，默认同 ruleKey")
    enabled:  bool          = Field(True,     description="是否启用")
    severity: str           = Field("medium", pattern="^(low|medium|high)$",
                                    description="风险等级：low / medium / high")


class UpdateNegativeRuleBody(BaseModel):
    """
    允许更新任意规则字段（tagName / enabled / severity / keywords 等）。
    ruleKey 由 URL path 参数确定，body 中无需提供。
    """
    model_config = ConfigDict(extra="allow")


class UpdateSeverityBody(BaseModel):
    low:    Optional[int] = Field(None, ge=0, description="低风险扣分值")
    medium: Optional[int] = Field(None, ge=0, description="中风险扣分值")
    high:   Optional[int] = Field(None, ge=0, description="高风险扣分值")
