"""定义 Swagger/OpenAPI 使用的公共响应数据契约。

运行时代码继续保持既有 JSON 字段和业务错误码。本模块只把已经存在的响应结构
显式建模，供 FastAPI 生成非空响应 Schema、客户端代码和契约测试。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from core.resume_contract import ResumeData


TaskStatus = Literal[0, 1, 2, 3, 4]
TagSource = Literal["rule", "llm", "hybrid"]


class BusinessErrorResponse(BaseModel):
    """表示使用业务错误码返回的通用失败响应。"""

    code: int = Field(description="非零业务错误码")
    msg: str = Field(description="可直接展示或记录的错误信息")
    data: Any | None = Field(default=None, description="错误附加数据，通常为空")


class HttpErrorResponse(BaseModel):
    """表示使用 HTTP 4xx 返回的简单错误响应。"""

    error: str = Field(description="错误原因")


class HttpDetailErrorResponse(BaseModel):
    """表示 FastAPI `HTTPException` 生成的标准错误响应。"""

    detail: str = Field(description="HTTP 错误详情")


class HealthResponse(BaseModel):
    """表示进程存活探针响应。"""

    status: Literal["healthy"]
    timestamp: str = Field(description="服务本地 ISO 8601 时间")


class ModelInfo(BaseModel):
    """表示 OpenAI 模型列表中的单个逻辑模型。"""

    id: str
    object: Literal["model"]
    created: int
    owned_by: str


class ModelListResponse(BaseModel):
    """表示 OpenAI SDK 兼容的模型列表响应。"""

    object: Literal["list"]
    data: list[ModelInfo]


class PositiveTagValue(BaseModel):
    """表示单个正向标签值及其来源。"""

    tagId: str
    tag: str
    source: TagSource


class PositiveTagDimension(BaseModel):
    """表示最终响应中的一个正向标签维度。"""

    dimensionId: str
    dimension: str
    displayName: str
    confidence: float | None = None
    value: list[PositiveTagValue] = Field(default_factory=list)


class PositiveTags(BaseModel):
    """表示固定顺序输出的正向标签集合。"""

    tagsArray: list[PositiveTagDimension] = Field(default_factory=list)


class NegativeTagValue(BaseModel):
    """表示单个风险标签、等级和命中证据。"""

    tagId: str
    tag: str
    severity: str
    matchedContent: str
    source: TagSource


class NegativeTagDimension(BaseModel):
    """表示最终响应中的一个风险标签维度。"""

    dimensionId: str
    dimension: str
    displayName: str
    value: list[NegativeTagValue] = Field(default_factory=list)


class NegativeTags(BaseModel):
    """表示风险命中统计和分维度明细。"""

    hitCount: int = 0
    deductionScore: int = 0
    tagsArray: list[NegativeTagDimension] = Field(default_factory=list)


class TaggedResumeResult(ResumeData):
    """表示解析与打标完成后的公共结果对象。"""

    taskId: str
    mappingVersion: str
    resumeParseFinishTime: str
    tagParseFinishTime: str
    positiveTags: PositiveTags
    negativeTags: NegativeTags


class ParsedResumeResult(BaseModel):
    """表示仅解析异步任务可直接传给仅打标接口的结果。"""

    filename: str
    extra: str | None = None
    resumeParseFinishTime: str
    resume: ResumeData


class TaskSubmissionData(BaseModel):
    """表示单任务提交成功后的初始状态。"""

    taskId: str
    status: TaskStatus
    estimatedSeconds: int


class TaskSubmissionResponse(BaseModel):
    """表示单任务提交接口的成功响应。"""

    code: Literal[0]
    msg: str
    data: TaskSubmissionData


class TaskStatusData(BaseModel):
    """表示轮询或同步批次中的单个任务状态。"""

    taskId: str | None
    status: TaskStatus
    statusDesc: str
    elapsedSeconds: float
    estimatedSeconds: int
    extra: str | None = None
    result: TaggedResumeResult | ParsedResumeResult | None = None
    errorMsg: str | None = None


class TaskStatusResponse(BaseModel):
    """表示异步任务轮询响应，包括任务不存在场景。"""

    code: int
    msg: str
    data: TaskStatusData | None = None


class AsyncBatchTask(BaseModel):
    """表示异步批量提交中的单份文件结果。"""

    filename: str
    extra: str | None = None
    code: int
    msg: str | None = None
    taskId: str | None = None
    status: TaskStatus | None = None
    statusDesc: str | None = None


class AsyncBatchSubmissionResponse(BaseModel):
    """表示异步批量提交统计及任务列表。"""

    code: int
    msg: str
    total: int
    submitted: int
    failed: int
    estimatedSeconds: int
    tasks: list[AsyncBatchTask]


class TaskListItem(BaseModel):
    """表示任务总览接口中的单项摘要。"""

    taskId: str
    status: TaskStatus
    statusDesc: str
    elapsedSeconds: float
    createdAt: str
    extra: str | None = None
    candidateName: str | None = None
    resumeParseTime: str | None = None
    tagParseTime: str | None = None
    completedAt: str | None = None
    result: TaggedResumeResult | ParsedResumeResult | None = None
    errorMsg: str | None = None


class TaskListData(BaseModel):
    """表示当前进程内任务数量和摘要列表。"""

    total: int
    tasks: list[TaskListItem]


class TaskListResponse(BaseModel):
    """表示任务总览接口的成功响应。"""

    code: Literal[0]
    msg: str
    data: TaskListData


class BatchTaskItemResponse(BaseModel):
    """表示同步解析与打标批次中的单项结果。"""

    code: int
    msg: str
    data: TaskStatusData


class SyncBatchResponse(BaseModel):
    """表示同步解析与打标或仅打标的批量响应。"""

    total: int
    success: int
    failed: int
    elapsed_s: float
    results: list[BatchTaskItemResponse]


class SyncParseItemData(BaseModel):
    """表示同步仅解析单项的成功或失败数据。"""

    filename: str
    extra: str | None = None
    resumeParseFinishTime: str | None = None
    resume: ResumeData | None = None
    errorMsg: str | None = None


class SyncParseItemResponse(BaseModel):
    """表示同步仅解析批次中的单项响应。"""

    code: int
    msg: str
    data: SyncParseItemData


class SyncParseBatchResponse(BaseModel):
    """表示同步仅解析的批量统计和结果。"""

    total: int
    success: int
    failed: int
    elapsed_s: float
    results: list[SyncParseItemResponse]


class ChatMessageResponse(BaseModel):
    """表示 OpenAI completion 返回的助手消息。"""

    role: Literal["assistant"]
    content: str


class ChatChoice(BaseModel):
    """表示 OpenAI completion 的单个候选结果。"""

    index: int
    message: ChatMessageResponse
    finish_reason: str


class ChatUsage(BaseModel):
    """表示兼容接口返回的 token 统计。"""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponse(BaseModel):
    """表示非流式 OpenAI Chat Completions 响应。"""

    id: str
    object: Literal["chat.completion"]
    created: int
    model: str
    choices: list[ChatChoice]
    usage: ChatUsage


class TextEchoResponse(BaseModel):
    """表示兼容入口收到普通文本而非文件条目时的回显响应。"""

    type: Literal["text"]
    text: str


class PositiveAlgorithmResponse(BaseModel):
    """表示内部正向标签算法的原始结果。"""

    positiveTags: list[dict[str, Any]]
    confidence: float


class SensitiveHit(BaseModel):
    """表示内部风险检测的一条命中。"""

    model_config = ConfigDict(extra="allow")

    tag: str
    severity: str
    matchedContent: str


class SensitiveHitGroup(BaseModel):
    """表示内部风险检测的分类命中集合。"""

    category: str
    hits: list[SensitiveHit]


class SensitiveAlgorithmResponse(BaseModel):
    """表示内部规则风险检测结果。"""

    hitTags: list[SensitiveHitGroup]
    hitCount: int
    deductionScore: int


class PositiveDimensionConfig(BaseModel):
    """表示运行时正向维度配置。"""

    dimension: str
    displayName: str
    enabled: bool
    allowedValues: list[str]


class NegativeRuleConfig(BaseModel):
    """表示字段随检测类型变化的负向规则配置。"""

    model_config = ConfigDict(extra="allow")


class NegativeDimensionConfig(BaseModel):
    """表示运行时负向维度配置。"""

    dimension: str
    displayName: str
    enabled: bool
    detectionType: str = Field(
        default="rule",
        description="检测类型；旧配置省略时按 rule 处理",
    )
    rules: dict[str, NegativeRuleConfig]


class TagConfigData(BaseModel):
    """表示完整运行时标签配置和读取时间。"""

    positiveDimensions: list[PositiveDimensionConfig]
    negativeDimensions: list[NegativeDimensionConfig]
    severityDeduction: dict[str, int]
    mappingVersion: str
    updatedAt: str


class TagConfigResponse(BaseModel):
    """表示标签配置查询或更新成功响应。"""

    code: Literal[0]
    msg: str
    data: TagConfigData


class TagMappingDimension(BaseModel):
    """表示稳定维度代码、展示名和四位维度 ID。"""

    id: str
    dimension: str
    displayName: str


class TagMappingValue(BaseModel):
    """表示一个标签值及其稳定 ID、别名和可选负向规则键。"""

    id: str
    dimensionId: str
    dimension: str
    value: str
    aliases: list[str]
    category: str | None = None
    ruleKey: str | None = None


class TagMappingData(BaseModel):
    """表示当前完整映射快照及其内容版本。"""

    mappingVersion: str
    dimensions: list[TagMappingDimension]
    mappings: list[TagMappingValue]


class TagMappingResponse(BaseModel):
    """表示标签 ID 映射查询成功响应。"""

    code: Literal[0]
    msg: str
    data: TagMappingData
