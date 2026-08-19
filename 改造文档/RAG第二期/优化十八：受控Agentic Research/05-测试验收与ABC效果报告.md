# 受控 Agentic Research 测试验收与 A/B/C 效果报告

主测试验证三件事：查询分解是否提高多要点覆盖、第二轮补证是否修复依赖型缺口、效果提升是否值得额外的模型调用和延迟。

评测只使用一套核心证据指标，不为 Research 重新发明大量指标。Agentic 专属指标只有 `Multi-hop Repair Rate` 和 `Unnecessary Supplement Rate` 两项。

所有报告必须由固定数据集和真实运行结果生成。模型不可用、标注未完成、索引发生变化或调用失败时记录实际原因，不生成示例数值冒充正式结果。

## 测试准备

准备：

- 可用的 PostgreSQL、PgVector、Elasticsearch 和 Redis
- 已完成索引构建的真实知识库
- 可用的 Embedding、Qwen3-Rerank 和 DeepSeek API
- FAST 模型 `deepseek-v4-flash`
- STRONG 模型 `deepseek-v4-pro`
- 16 个经过人工复核的固定 Case，每类 4 个
- 每个 Case 对应的必要要点和可接受证据 Chunk
- 能够访问 Research 的 Pro 租户
- 可选的 Langfuse 环境

环境变量：

```env
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=实际APIKey
LLM_MODEL=deepseek-v4-flash
LLM_STRONG_MODEL=deepseek-v4-pro
LLM_TIMEOUT_SECONDS=60

RERANK_BASE_URL=实际Qwen3-Rerank地址
RERANK_API_KEY=实际APIKey
RERANK_MODEL=qwen3-rerank

LANGFUSE_ENABLED=true
LANGFUSE_HOST=实际地址
LANGFUSE_PUBLIC_KEY=实际PublicKey
LANGFUSE_SECRET_KEY=实际SecretKey
```

Langfuse 只影响链路观测检查，不影响 A/B/C 核心指标计算。

评测目录：

```text
eval/opt18_research/
├── research_dataset.json
├── single_baseline.json
├── decomposition_parallel.json
├── agentic_research.json
├── research_comparison.json
└── research_comparison.md
```

文件含义：

| 文件 | 类型 | 内容 |
|------|------|------|
| `research_dataset.json` | 固定输入 | 问题、要点、证据标注和索引版本 |
| `single_baseline.json` | A 组原始结果 | 单次检索逐题明细 |
| `decomposition_parallel.json` | B 组原始结果 | 查询分解与并行检索逐题明细 |
| `agentic_research.json` | C 组原始结果 | 受控补证逐题、逐轮明细 |
| `research_comparison.json` | 正式报告 | 可机读汇总和所有未通过项 |
| `research_comparison.md` | 正式报告 | 面向人工审阅的效果结论 |

只有最后两个文件称为正式报告，不为三组分别生成 Markdown 报告。

## 一、自动化测试

运行：

```bash
pytest \
  tests/test_research_foundation.py \
  tests/test_research_planner.py \
  tests/test_research_parallel_retrieval.py \
  tests/test_research_candidate_fusion.py \
  tests/test_research_decider.py \
  tests/test_research_supplement.py \
  tests/test_research_finalizer.py \
  tests/test_research_state_machine.py \
  tests/test_research_api.py \
  tests/test_research_policy.py \
  tests/test_research_observability.py \
  -v

pytest tests -v
```

前端运行：

```bash
npm run test
npm run build
```

至少覆盖以下能力。

### 1. 模型与基础设施

- FAST 和 STRONG 模型映射正确
- Planner、Decider 和 Finalizer 均关闭思考模式
- JSON 输出、max_tokens、timeout 正确传递
- `log_payload=false` 时不记录问题和候选正文
- Research 子查询不调用 LLM Query Rewrite
- `retrieve_once` 与 `/rag/retrieve` 共用同一检索实现
- 抽取前后 `/rag/retrieve` 关键返回字段一致

### 2. Query Plan 与并行检索

- 简单问题只生成一个首轮查询
- 多要点问题最多生成三个首轮查询
- 每个查询绑定至少一个必要要点
- Planner 失败时使用原问题回退
- 首轮并发数不超过 3
- 单个任务失败不取消其他任务
- 候选按要点保留，不被单个高频要点占满
- 跨查询重复 Chunk 正确合并 provenance

### 3. Decider 与补证

- core 和 supporting 角色严格校验
- supporting 不能把要点判定为 covered
- status 和 missing_aspects 由后端重算
- complete 时不执行第二轮
- 补证 query 只绑定缺失要点
- 重复 query 被拒绝
- 第二轮查询不超过 2 个
- 总检索轮数不超过 2
- 两轮后强制停止
- 无新证据、超时和降级停止原因正确

### 4. Finalizer 与引用

- 回答只使用最终合法候选
- supported 内容至少引用一条合法证据
- 缺少证据时不生成猜测答案
- 每个 `[E编号]` 都能映射 EvidenceItem
- EvidenceItem 能够回查租户范围内的原始 Chunk
- 文档冲突时不会自动选择权威结论
- Finalizer 失败时 answer 为 null，证据包仍返回

### 5. API、权限与观测

- Single 和 Research 使用不同接口
- Research 只允许具备套餐权限的租户使用
- 每轮保持 tenant、kb_ids 和 index_versions 不变
- 请求不能设置任意轮数、工具和外部搜索
- Research 根 Trace 和阶段 Span 完整
- 观测异常不影响主流程
- response、运行日志和 Trace 的 stop_reason 一致
- 日志和 Trace 不包含 API Key、Header 和完整候选正文

单元测试使用 Fake Provider 或 mock，不产生真实模型费用。

## 二、固定评测数据

生成数据集骨架：

```bash
python scripts/compare_agentic_research.py prepare \
  --output eval/opt18_research/research_dataset.json
```

数据集固定为 16 个 Case，每类 4 个：

| case_type | 目的 |
|-----------|------|
| `simple` | 验证简单问题不会被复杂化 |
| `independent_multi_aspect` | 验证查询分解和并行检索 |
| `dependent_multi_hop` | 验证第二轮定向补证 |
| `control` | 验证无答案、冲突、版本和权限边界 |

4 个 `dependent_multi_hop` 必须满足：单次原问题检索不能直接覆盖全部目标证据，首轮材料中存在明确补证线索。数据准备阶段至少确认 2 个 Case 能够形成有效的 B 组未完整样本，否则 `Multi-hop Repair Rate` 的分母过小，不能支撑正式结论。

每个 Case 至少包含：

```json
{
  "id": "member_points_refund",
  "case_type": "independent_multi_aspect",
  "question": "积分抵扣订单退款后，积分和现金分别怎么退？",
  "ground_truth": "积分按规则退回账户，现金按原支付渠道退回。",
  "kb_ids": ["实际知识库ID"],
  "index_versions": {"实际知识库ID": "v2"},
  "required_aspects": [
    {
      "aspect_id": "A1",
      "description": "积分如何退回",
      "acceptable_chunk_ids": ["实际积分证据ID"]
    },
    {
      "aspect_id": "A2",
      "description": "现金如何退回",
      "acceptable_chunk_ids": ["实际现金证据ID"]
    }
  ],
  "expected_documents": [
    "实际会员规则Document ID",
    "实际支付规则Document ID"
  ],
  "expected_status": "complete",
  "requires_multi_hop": false,
  "dependency": null,
  "review_status": "reviewed"
}
```

依赖型 Case 增加：

```json
{
  "requires_multi_hop": true,
  "dependency": {
    "discover_from_chunk_ids": ["首轮线索Chunk ID"],
    "target_chunk_ids": ["补证目标Chunk ID"]
  }
}
```

### 标注规则

- required aspect 是回答必须覆盖的最少要点
- acceptable Chunk 必须能够直接或必要地支撑对应要点
- 同一要点存在等价 Chunk 时全部标出
- 同一 Chunk 可以出现在多个 aspect 的 acceptable 列表中
- supporting 证据确有必要时也可以列入 acceptable 列表
- `expected_status` 根据知识库中是否存在足够证据人工确定
- 无答案 Case 的 acceptable 列表允许为空
- 冲突 Case 要同时标注冲突双方证据
- 标注时不能查看 A/B/C 输出
- 每个 Case 至少由一人标注、一人复核

## 三、数据集校验

运行：

```bash
python scripts/compare_agentic_research.py validate \
  --dataset eval/opt18_research/research_dataset.json
```

必须检查：

- Case 总数和每类数量
- ID 唯一且字段完整
- 所有 Case 为 `reviewed`
- kb_ids 属于评测租户
- 索引版本存在且状态可用
- required aspect 非空、不重复
- acceptable Chunk 真实存在
- acceptable Chunk 位于指定 kb 和 index version
- `requires_multi_hop=true` 时 dependency 完整
- discover Chunk 和 target Chunk 不相同
- control Case 的预期状态和标注一致
- 数据集 SHA-256 与正式运行记录一致

任一检查失败时禁止执行真实 A/B/C 批量调用。

## 四、A/B/C 对比口径

三组使用完全相同的：

```text
问题
租户
知识库
索引版本
profile
top_k
Embedding
Vector / BM25 / RRF
Qwen3-Rerank
模型版本
Prompt 版本
人工标注
```

正式对比期间不修改代码、Prompt、模型、数据集和活动索引。

### A：Single Retrieve

```text
原始问题
    ↓
POST /rag/retrieve
    ↓
一次检索 + 可选 EvidencePack
    ↓
禁止查询分解和补证
```

A 组固定：

```text
query_options.enabled = false
evidence_options.enabled = true
```

A 组代表当前单次检索入口的真实质量和成本。

### B：Decomposition + Parallel Retrieval

```text
Planner
    ↓
1～3 个首轮查询并行检索
    ↓
Decider
    ↓
强制禁止第二轮
    ↓
Finalizer
```

B 组通过评测脚本调用 Research 控制器的测试模式：

```text
allow_supplement = false
```

这些开关只能存在于离线评测入口，不能出现在公开 API。

### C：Controlled Agentic Research

```text
Planner
    ↓
1～3 个首轮查询并行检索
    ↓
Decider
    ↓
证据不足时执行一次补证
    ↓
Finalizer
```

C 组固定：

```text
allow_decomposition = true
allow_supplement = true
```

C 组调用真实 `/rag/research` 等价链路，所有服务端上限正常生效。

### 三组分别证明什么

```text
B 对 A
    查询分解与并行检索是否提高独立多要点覆盖

C 对 B
    第二轮补证是否修复首轮无法解决的依赖型缺口

C 对 A
    完整 Research 链路是否产生总体质量收益
```

A 组不生成最终回答时，不参与回答文本语义指标，只参与证据质量、引用回溯、延迟和调用成本对比。B 对 A 表示引入查询分解、并行检索和统一 Research 证据处理后的净变化；C 对 B 只增加第二轮补证，因此能够单独判断多跳补证收益。

## 五、正式运行

先执行小规模预检，只检查连接、权限和理论调用量：

```bash
python scripts/compare_agentic_research.py preflight \
  --dataset eval/opt18_research/research_dataset.json
```

preflight 输出：

```text
Case 数
A/B/C 预计请求数
预计 LLM 调用上限
模型名称
索引版本
缺失依赖
输出文件是否已存在
```

preflight 不调用真实 LLM。

正式运行每题一轮，不做 warm-up 和重复稳定性测试：

```bash
python scripts/compare_agentic_research.py run \
  --mode single \
  --dataset eval/opt18_research/research_dataset.json \
  --repeat 1 \
  --warmup 0 \
  --output eval/opt18_research/single_baseline.json

python scripts/compare_agentic_research.py run \
  --mode decomposition \
  --dataset eval/opt18_research/research_dataset.json \
  --repeat 1 \
  --warmup 0 \
  --output eval/opt18_research/decomposition_parallel.json

python scripts/compare_agentic_research.py run \
  --mode research \
  --dataset eval/opt18_research/research_dataset.json \
  --repeat 1 \
  --warmup 0 \
  --output eval/opt18_research/agentic_research.json
```

Case 之间默认串行执行，`--case-concurrency=1`；同一个 Research Case 内的子查询仍按服务端规则并行。运行器每完成一题立即保存 checkpoint，并支持：

### 依赖型小步门禁

不需要一开始就运行完整 A/B/C。先准备与正式数据隔离的四题依赖链数据集，只运行 C 组：

```bash
python scripts/compare_agentic_research.py prepare-smoke \
  --output eval/opt18_research/research_dataset_smoke_v3.json

python scripts/compare_agentic_research.py run \
  --mode research \
  --dataset eval/opt18_research/research_dataset_smoke_v3.json \
  --case-type dependent_multi_hop \
  --case-concurrency 1 \
  --output eval/opt18_research/smoke_v3_research.json

python scripts/compare_agentic_research.py smoke-check \
  --dataset eval/opt18_research/research_dataset_smoke_v3.json \
  --research eval/opt18_research/smoke_v3_research.json \
  --json-output eval/opt18_research/smoke_v3_gate.json \
  --markdown-output eval/opt18_research/smoke_v3_gate.md
```

C 组至少要有两题满足“首轮证据不完整、第二轮新增命中目标 Chunk、最终证据完整”，并且没有超轮次、超调用或降级，才继续运行同四题的 B 组。C 组不通过时停止，不运行 B 组和完整 A/B/C，避免为无效链路继续消耗模型调用。

C 组通过后再运行 B 组，并计算真实修复率：

```bash
python scripts/compare_agentic_research.py run \
  --mode decomposition \
  --dataset eval/opt18_research/research_dataset_smoke_v3.json \
  --case-type dependent_multi_hop \
  --case-concurrency 1 \
  --output eval/opt18_research/smoke_v3_decomposition.json

python scripts/compare_agentic_research.py smoke-check \
  --dataset eval/opt18_research/research_dataset_smoke_v3.json \
  --decomposition eval/opt18_research/smoke_v3_decomposition.json \
  --research eval/opt18_research/smoke_v3_research.json \
  --json-output eval/opt18_research/smoke_v3_bc_gate.json \
  --markdown-output eval/opt18_research/smoke_v3_bc_gate.md
```

B/C 门禁要求 B 组未完整样本不少于两题，C 组修复率不低于 50%，且 C 组要点覆盖率不能低于 B 组。只有门禁通过，才有必要运行独立多要点 A/B 和最终完整 A/B/C。

```bash
# 中断后跳过已完成题目继续运行
python scripts/compare_agentic_research.py run \
  --mode research \
  --dataset eval/opt18_research/research_dataset.json \
  --resume \
  --case-concurrency 1 \
  --output eval/opt18_research/agentic_research.json
```

运行器也支持 `--max-cases`，但只按 Case 类型验证时应优先使用 `--case-type`，避免抽到其他类型后误判多跳效果。

单题达到 90 秒预算后记录 timeout 并继续下一题，不允许一个 Case 无限阻塞整批任务。不要默认并行运行多个 Case，以免放大 LLM、Embedding 和 Rerank 的瞬时请求量。

每组运行前重新校验数据集哈希、代码 commit、Prompt 哈希、模型和索引版本。三组任一项不一致时，compare 命令必须拒绝生成正式结论。

## 六、逐题原始结果

每个原始结果文件至少保存：

```text
run_id
run_started_at
dataset_sha256
code_commit
mode
Provider / model
Planner / Decider / Finalizer Prompt SHA-256
kb_ids / index_versions
case_id / case_type
query plan
每轮 retrieval tasks
每轮候选和新增 Chunk ID
最终 EvidenceItem Chunk ID
groups / status / missing_aspects
answer 和引用编号
stop_reason
round_count
retrieval_task_count
llm_call_count
各阶段 latency_ms
input / output / total tokens
success / degraded / error
```

逐题结果必须足以重新计算所有汇总指标。

## 七、核心指标口径

主报告只使用五个通用证据指标和两个 Agentic 专属指标。

### 1. Required Aspect Coverage

```text
获得合法 core 证据的 required aspect 数
÷
required aspect 总数
```

如果 EvidenceGroup 的 core EvidenceItem 命中该要点任意 `acceptable_chunk_id`，该 required aspect 记为覆盖。

### 2. Complete Case Rate

```text
全部 required aspects 都被覆盖的 Case 数
÷
总 Case 数
```

同时按 case_type 分层计算，主结论重点查看：

```text
independent_multi_aspect
dependent_multi_hop
```

### 3. Evidence Precision

```text
命中任意 required aspect 可接受证据集合的 EvidenceItem 数
÷
EvidenceItem 总数
```

主题相关但不能支撑任何必要要点的 EvidenceItem 计为无效。

### 4. Status Accuracy

先根据真实覆盖情况计算：

```text
complete  全部 required aspects 覆盖
partial   只覆盖部分 required aspects
missing   没有 required aspect 覆盖
```

再与 API 返回的 `evidence_pack.status` 对比。

### 5. Citation Traceability

```text
能够在当前 tenant、kb 和 index version 回查原始 Chunk 的引用数
÷
全部引用数
```

B、C 回答中的每个 `[E编号]` 还必须映射到同一响应中的 EvidenceItem。

### 6. Multi-hop Repair Rate

只计算 `dependent_multi_hop` 中 B 组未完整的 Case：

```text
C 组通过第二轮从非 complete 修复为 complete 的 Case 数
÷
B 组非 complete 且存在合法 target evidence 的 Case 数
```

如果分母为 0，报告标记 `not_evaluable`，不能写成 100%。

### 7. Unnecessary Supplement Rate

```text
C 组首轮已经 complete 却仍执行第二轮的 Case 数
÷
C 组首轮已经 complete 的 Case 数
```

如果分母为 0，同样标记 `not_evaluable`。

## 八、成本与性能只作为约束

报告记录：

```text
P50 / P95 latency
平均检索轮数
平均 retrieval task 数
平均 LLM 调用数
input / output / total tokens
理论调用成本
success rate / degraded rate
```

这些数据不是新的质量指标，不与核心质量指标混在同一排名中。

理论成本只有在报告冻结以下信息时才计算：

```text
模型价格来源
价格版本日期
输入/输出单价
缓存 Token 口径
币种
```

无法确定价格时只报告 Token，不填虚构金额。

`new_evidence_count`、重复查询数和停止原因分布属于逐题诊断字段，不进入核心验收指标表。

## 九、主效果表

正式 Markdown 报告的第一张表固定为：

| 指标 | A Single | B Decomposition | C Research | 主要对比 |
|------|---------:|----------------:|-----------:|----------|
| Required Aspect Coverage | 实测 | 实测 | 实测 | B-A、C-B |
| Complete Case Rate | 实测 | 实测 | 实测 | B-A、C-B |
| Evidence Precision | 实测 | 实测 | 实测 | 质量护栏 |
| Status Accuracy | 实测 | 实测 | 实测 | 决策可靠性 |
| Citation Traceability | 实测 | 实测 | 实测 | 硬性门槛 |
| Multi-hop Repair Rate | — | — | 实测 | C 修复能力 |
| Unnecessary Supplement Rate | — | — | 实测 | C 控制能力 |

第二张表只展示性能与成本：

| 观察项 | A Single | B Decomposition | C Research |
|--------|---------:|----------------:|-----------:|
| P50 / P95 延迟 | 实测 | 实测 | 实测 |
| 平均检索轮数 | 实测 | 实测 | 实测 |
| 平均检索任务数 | 实测 | 实测 | 实测 |
| 平均 LLM 调用数 | 实测 | 实测 | 实测 |
| 平均 Token | 实测 | 实测 | 实测 |
| 成功率 / 降级率 | 实测 | 实测 | 实测 |

不要把 nDCG、MRR、Recall、Faithfulness、Answer Relevance、New Evidence Rate 等全部塞入主表。

## 十、分层结果

报告必须分别展示四类 Case，不能只给总体平均数。

### simple

重点检查：

- Planner 是否只生成一个查询
- 是否在首轮停止
- C 的质量不能明显低于 A
- 是否产生不必要补证

### independent_multi_aspect

重点检查：

- B 的 Aspect Coverage 是否高于 A
- 每个要点是否获得独立候选
- 某个高频要点是否挤掉其他要点

### dependent_multi_hop

重点检查：

- 首轮是否发现 dependency 线索
- Decider 是否围绕缺失要点生成补证查询
- 第二轮是否命中 target Chunk
- C 是否把 B 的 partial/missing 修复为 complete

### control

重点检查：

- 无答案时是否保持 missing/partial
- 冲突证据是否分别引用并说明不一致
- 是否访问错误索引版本
- 是否跨租户或跨未授权知识库
- 是否生成没有证据支持的确定结论

## 十一、Agent 控制验收

逐题检查：

- 首轮 query 数 `<= 3`
- 补证 query 数 `<= 2`
- round_count `<= 2`
- llm_call_count `<= 3`
- complete 时 round_count 必须为 1
- 第二轮 query 只绑定 missing aspect
- 第二轮 query 不得与 visited query 重复
- 第二轮不能改变 tenant、kb 和 index version
- stop_reason 与实际执行路径一致
- 超时后没有新的 LLM 或检索任务

任何一题突破硬上限都视为工程验收失败，不用平均值掩盖。

## 十二、故障注入

至少模拟：

```text
Planner 超时
Planner 返回非法 JSON
一个首轮查询失败
所有首轮查询失败
Decider 429
Decider 返回越界 candidate index
所有补证 query 重复
一个补证查询失败
第二轮没有新增 Chunk
Finalizer 超时
Finalizer 返回无效引用
Langfuse 不可用
Redis 限流触发
```

验收预期：

- Planner 失败时回退为原问题单检索
- 部分检索失败时保留成功结果
- Decider 失败时不执行不可信补证
- 第二轮失败时保留首轮证据
- Finalizer 失败时 answer=null，证据包仍可用
- Langfuse 失败不影响主响应
- 限流和权限错误使用标准错误码
- 所有异常不泄露正文、Key 和 Header

## 十三、API 手工测试

### 1. Single

```bash
curl -X POST 'http://localhost:8000/api/v1/rag/retrieve' \
  -H 'Authorization: Bearer 实际APIKey' \
  -H 'Content-Type: application/json' \
  -d '{
    "kb_ids": ["实际知识库ID"],
    "user_id": "acceptance-user",
    "query": "未发货订单退款多久到账？",
    "profile": "quality",
    "evidence_options": {"enabled": true, "max_items": 8},
    "query_options": {"enabled": false, "strategy": "noop"}
  }'
```

### 2. Research 首轮完成

```bash
curl -X POST 'http://localhost:8000/api/v1/rag/research' \
  -H 'Authorization: Bearer 实际APIKey' \
  -H 'Content-Type: application/json' \
  -d '{
    "kb_ids": ["实际知识库ID"],
    "user_id": "acceptance-user",
    "query": "积分抵扣订单退款后，积分和现金分别怎么退？",
    "profile": "quality",
    "top_k": 5
  }'
```

检查：

```text
plan.initial_queries 为 1～3 条
round_count = 1
stop_reason = evidence_complete
answer 引用全部可回溯
```

### 3. Research 触发补证

选择数据集中确定需要第二轮的 Case，检查：

```text
round_count = 2
rounds[1].purpose = supplement
补证 query 绑定首轮 missing aspect
new_chunk_count > 0，或明确记录 no_new_evidence
llm_call_count <= 3
```

## 十四、调试台验收

### Single 模式

- 默认选中
- 现有参数和结果展示正常
- 不出现 Research 时间线

### Research 模式

- 套餐允许时可以选择
- Query Rewrite 和证据编排开关隐藏
- 不提供轮数、模型次数和工具配置
- 正确展示 aspects 和 initial queries
- 正确展示首轮并行任务
- 只在实际补证时展示第二轮
- answer 的 `[E编号]` 可以定位证据卡片
- 显示 status、missing_aspects 和 stop_reason
- 显示轮数、任务数、LLM 次数、Token 和耗时
- 降级时仍能查看已获得证据

## 十五、Langfuse 验收

选择一条一轮完成和一条两轮完成的请求。

一轮 Trace 至少包含：

```text
planner
retrieval_round_1
evidence_decider
finalizer
```

两轮 Trace 至少包含：

```text
planner
retrieval_round_1
evidence_decider
retrieval_round_2
finalizer
```

检查：

- trace_id 与 API 响应一致
- 每个任务的 query_id、round 和耗时正确
- Token 汇总等于阶段用量之和
- stop_reason 与 API 响应一致
- Trace 不含 API Key 和 Authorization Header
- Span 不保存完整候选正文
- 提交反馈后能够关联到同一 Trace

## 十六、生成正式报告

运行：

```bash
python scripts/compare_agentic_research.py compare \
  --dataset eval/opt18_research/research_dataset.json \
  --single eval/opt18_research/single_baseline.json \
  --decomposition eval/opt18_research/decomposition_parallel.json \
  --research eval/opt18_research/agentic_research.json \
  --json-output eval/opt18_research/research_comparison.json \
  --markdown-output eval/opt18_research/research_comparison.md
```

报告至少包含：

```text
测试时间、代码 commit 和数据集 SHA-256
Case 总数、类型分布和复核状态
模型、Prompt SHA-256 和索引版本
A/B/C 执行定义
主效果表
性能与成本表
四类 Case 的分层结果
Multi-hop Repair 明细
Unnecessary Supplement 明细
所有 degraded、failed 和 not_evaluable 项
硬上限、权限和引用检查结果
逐题结果索引
测试限制
最终结论
```

JSON 中的每个汇总值必须能够由逐题原始结果重新计算。Markdown 不允许手工修改汇总数字。

## 十七、建议验收门槛

### 查询分解收益

- `independent_multi_aspect` 中，B 的 Required Aspect Coverage 高于 A
- `independent_multi_aspect` 中，B 至少比 A 多完成 1 个 Case
- `simple` 中，B、C 的 Complete Case Rate 相比 A 下降不超过 5 个百分点

### 多跳补证收益

- `dependent_multi_hop` 中，C 的 Complete Case Rate 高于 B
- C 至少比 B 多完成 1 个依赖型 Case
- Multi-hop Repair Rate 达到 50% 以上
- 如果没有可评估的 B 组未完整 Case，测试集需要补充，不能直接通过

### 质量护栏

- C 的 Required Aspect Coverage 不低于 B
- C 的 Evidence Precision 不低于 80%
- C 的 Evidence Precision 相比 B 下降不超过 5 个百分点
- C 的 Status Accuracy 不低于 80%
- Citation Traceability 达到 100%
- 无答案 Case 不生成无证据确定结论

### 控制与成本

- Unnecessary Supplement Rate 不高于 10%
- 所有 Case 的首轮 query 数不超过 3
- 所有 Case 的补证 query 数不超过 2
- 所有 Case 的 round_count 不超过 2
- 所有 Case 的 llm_call_count 不超过 3
- 成功请求总耗时不超过 90 秒服务端预算
- 任何超时请求都能够返回安全降级结果或标准超时错误

## 十八、可选语义检查

如果需要检查 B、C 最终回答的自然语言质量，可以单独运行语义评测：

```bash
python scripts/compare_agentic_research.py semantic \
  --dataset eval/opt18_research/research_dataset.json \
  --decomposition eval/opt18_research/decomposition_parallel.json \
  --research eval/opt18_research/agentic_research.json
```

可以使用 RAGAS 或固定 Judge Prompt 检查：

```text
回答是否忠于证据
回答是否覆盖参考结论
回答是否回应用户问题
```

语义结果放在正式报告的附录，不进入主效果表，也不替代 Chunk ID、状态、引用和 Agent 控制指标。语义 Judge 不可用时不影响核心验收结论。

## 十九、结论判定

报告只能给出以下三类结论之一：

### 通过

```text
B 对 A 证明查询分解有效
C 对 B 证明补证有效
质量护栏和 Agent 硬上限全部通过
```

### 有条件通过

```text
质量有提升，但成本、状态判断或部分控制门槛未通过
报告明确列出限制和启用范围
```

### 不通过

```text
B 没有提高多要点覆盖
或 C 没有修复依赖型缺口
或出现越权、伪造引用、无限循环、超预算等安全问题
```

不能只因为 C 的 Evidence Precision 较高就声称多跳有效，也不能只因为第二轮找到了新 Chunk 就声称回答完整。真正的补证收益必须表现为 B 的缺失 Case 在 C 中获得合法核心证据并变为完整。

## 二十、验收结果

全部满足以下条件才算完成：

- Research 相关新增测试通过
- 全量后端测试通过
- 前端测试和构建通过
- `/rag/retrieve` 回归行为不变
- 16 个固定 Case 已人工复核并冻结，每类固定 4 个
- A/B/C 使用相同代码、模型、Prompt、索引和数据集运行
- 三份逐题原始结果已真实生成
- JSON 和 Markdown 正式报告已由脚本生成
- 查询分解、多跳补证、质量护栏和控制门槛达到要求
- 所有失败、降级和 not_evaluable Case 已在报告中列出
- 每条引用都能回查授权范围内的原始 Chunk
- 报告和日志不包含 API Key、Authorization Header 和敏感信息
