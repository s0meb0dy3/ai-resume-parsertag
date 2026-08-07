# 真实简历黄金评测集

本目录只提交评测契约、脱敏示例和评测代码。真实 PDF/DOCX、真实姓名、电话、
邮箱及人工标注统一保存在 `evaluation/private/` 或外部受控目录，不进入 Git。

## 目录约定

```text
evaluation/
├── golden_manifest.example.json
├── golden_evaluator.py
└── private/                       # Git 忽略
    ├── golden_manifest.json       # 真实人工标注
    ├── resumes/                   # 按匿名 ID 重命名的原文件
    │   └── resume-001.pdf
    └── predictions/               # 服务生成的完整结果
        └── resume-001.json
```

清单使用匿名样本 ID、无 PII 文件名和 SHA-256 绑定源文件。实际人工标注允许包含
解析验收必需的姓名等字段，但运行评测时必须显式传入 `--allow-pii`。清单禁止保存
原始简历全文、Python 抽取文本或技能证据文本。

## 建集步骤

1. 从业务分布中分层抽样 PDF 文本件、PDF 扫描件、普通 DOCX、图片型 DOCX，
   覆盖应届、社招、长简历、中英文简历和不同行业。
2. 将文件重命名为 `resume-NNN.pdf/docx`，计算 SHA-256，原文件放入受控目录。
3. 两名标注人员独立填写 candidate、教育、工作、项目、正向标签和负向标签，
   对不一致项复核；禁止直接把当前模型输出当作真值。
4. 每次评测固定服务版本、模型名、Prompt 版本和参考数据版本，生成以匿名 ID
   命名的预测文件。
5. 先校验清单与源文件，再计算指标；阈值未通过时命令返回非零状态。

初始本地私有集可用于验证评测链路，但只有文本型 PDF 时不能代表发布覆盖完整。
正式发布集至少应包含扫描 PDF、普通 DOCX、图片型 DOCX 和英文简历，并按应届、
社招、长简历及行业分层。非确定性模型应在相同版本下重复运行，发布判断采用最差
一次指标，不得只保留通过的一次结果。

## 命令

```bash
export GOLDEN_RESUME_ROOT="$PWD/evaluation/private/resumes"

python scripts/evaluate_golden_set.py \
  --manifest evaluation/private/golden_manifest.json \
  --resume-root "$GOLDEN_RESUME_ROOT" \
  --validate-only \
  --allow-pii

python scripts/evaluate_golden_set.py \
  --manifest evaluation/private/golden_manifest.json \
  --resume-root "$GOLDEN_RESUME_ROOT" \
  --predictions evaluation/private/predictions \
  --report evaluation/private/report.json \
  --allow-pii
```

聚合指标包括解析字段准确率、正向标签 micro/macro Precision、Recall、F1，负向
标签 Precision、Recall、F1，并在 `diagnostics` 中输出每份简历的缺失项和多余项。
