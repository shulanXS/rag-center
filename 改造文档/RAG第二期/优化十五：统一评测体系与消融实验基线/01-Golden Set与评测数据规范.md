# Golden Set 与评测数据规范

点击此处可复制本篇提示词

在当前 rag-center 项目中，建立一个版本化的统一 Golden Set，作为 Query Rewrite、同义词扩展、Hybrid Search、重排、Profile 和 Research 评测的共享问题来源。

首个版本使用电商知识库构建 20 道基础题。数据结构要保持简单，满足当前评测即可；同时保留题目分组和知识库路由字段，使数据集能够增加人事复杂题，而不需要另建一套测试入口。

修改前先检查 `eval/` 下已有数据集、历史报告和知识库实际索引情况。可以从已有电商题中挑选和改写，但每道题的标准答案必须根据当前知识库原文人工核对。不生成无法在真实文档中找到依据的问题和答案。

## 一、建设目标

- 只维护一个当前生效的主 Golden Set
- 首个版本为 `golden_basic_20`，包含 20 道电商基础题
- 每道题指定一个 `kb_alias`，默认只检索对应知识库
- 数据集根节点统一保存知识库 ID 和语料版本
- 每道题至少包含 ID、问题、标准答案、题目类型和知识库别名
- 允许根据特殊实验增加可选的标准文档和标准章节
- 数据集中不保存 API Key、Authorization Header 和其他密钥
- 增加数据集校验，拒绝重复 ID、空问题、空标准答案和未知知识库别名

## 二、数据集文件

新增：

```text
eval/datasets/golden_basic_20.json
```

数据集根节点使用以下结构：

```json
{
  "schema_version": "1.0",
  "name": "rag_center_golden",
  "version": "golden_basic_20",
  "description": "20道电商基础检索题",
  "knowledge_bases": {
    "ecommerce": {
      "kb_id": "实际电商知识库ID",
      "corpus_version": "ecommerce_corpus_base"
    }
  },
  "suites": {
    "basic": {
      "description": "20道电商基础题",
      "case_ids": ["case-001", "case-002"]
    },
    "all": {
      "description": "当前版本的全部题目",
      "case_ids": "all"
    }
  },
  "cases": []
}
```

字段含义：

| 字段 | 含义 |
|------|------|
| `schema_version` | 数据结构版本，只有字段结构改变时才升级 |
| `version` | 题目内容版本，增删题目或修改标准答案时升级 |
| `knowledge_bases` | 知识库别名、实际 ID 和语料版本 |
| `suites` | 按实验场景选择题目的分组 |
| `cases` | 全部评测题目 |

知识库 ID 只在根节点保存一次，不在每道题中重复。切换本地环境或重建知识库后，只需更新 `knowledge_bases` 映射。

## 三、题目数据结构

基础题使用简单结构：

```json
{
  "id": "refund_unshipped",
  "kb_alias": "ecommerce",
  "suite": "basic",
  "case_type": "direct_fact",
  "question": "还没发货可以申请退款吗？",
  "ground_truth": "未发货订单申请后2小时内原路退款。",
  "expected_documents": ["售后服务与退换货政策.md"],
  "expected_headings": ["未发货退款"],
  "tags": ["refund", "single_document"]
}
```

必填字段：

| 字段 | 要求 |
|------|------|
| `id` | 稳定、唯一，使用小写英文和下划线 |
| `kb_alias` | 必须存在于根节点 `knowledge_bases` |
| `suite` | 首个版本固定为 `basic` |
| `case_type` | `direct_fact`、`paraphrase` 或 `multi_document` |
| `question` | 用户可能真实提出的中文问题 |
| `ground_truth` | 根据知识库原文核对后的完整标准答案 |

可选字段：

| 字段 | 用途 |
|------|------|
| `expected_documents` | 跨文档评测和错误定位 |
| `expected_headings` | 判断标准证据所在章节 |
| `tags` | 按领域、问题类型和难度分析 |

不要将当前 Chunk ID 设为基础题的必填字段。重新切块后 Chunk ID 会变化，而文档名、章节路径和标准答案仍可以用于评分和人工复核。

## 四、20 道基础题的分布

题目总数固定为 20，按以下比例构建。

### 1. 直接事实题10道

用于验证基础检索不能回归。例如：

- 现货普通订单多久必须揽收
- 新商家前90天的结算周期
- 优惠券过期后是否补发
- 未发货订单的退款时效

问题可以口语化，但标准答案只包含文档中可以证实的事实。

### 2. 口语改写和同义表达题5道

用于验证 Query Rewrite 和同义词扩展。例如：

```text
文档用词：价格保护
用户问法：买贵了能补差价吗？

文档用词：揽收超时
用户问法：商家一直不把货交给快递怎么办？
```

不要在问题中直接复制文档标题和关键词。

### 3. 跨章节或跨文档题5道

用于验证 Hybrid Search 和重排。例如：

- 组合支付部分退款后，现金、优惠券和积分分别怎么退
- 白金会员购买清仓商品后降价，是否享受30天价保
- 直播大促订单的发货时效和超时赔付规则

这类问题的 `ground_truth` 必须包含所有必要要点，不能只保存其中一个结论。

## 五、题目选择原则

为了避免数据集过于简单，选题时遵守以下规则：

- 电商知识库每篇主要文档至少被一道题覆盖
- 不连续构建多道只替换数字的重复题
- 同一规则不同问法只保留最有代表性的一道
- 至少一半问题不直接出现文档标题原词
- 跨文档题的各个要点必须分布在真实的不同文档或章节中
- 包含数字、时间、否定条件和例外规则的题目不少于 5 道
- 不故意设计知识库中没有答案的问题

首个版本不需要为了增加文档数而加入无关文档。如果 Vector、Hybrid 和重排在 20 道题上几乎得到完全相同的结果，再增加与现有规则相似、容易混淆的真实文档，并升级 `corpus_version` 和 Golden Set 版本。

## 六、标准答案规则

`ground_truth` 是 RAGAS 评判 Context Recall 和 Context Precision 的核心依据，需要比普通问答中的简短答案更完整。

正确示例：

```text
现金部分按原支付比例和原渠道退回；
优惠券按促销活动规则决定是否退回；
已抵扣积分按等值规则退回，过期积分不退。
```

不完整示例：

```text
按原路退回。
```

编写要求：

- 覆盖问题必须回答的所有要点
- 保留适用条件、例外、时间和数字
- 使用陈述句，不写“参考某文档”这类无实际答案的内容
- 不增加原文中没有的推断、建议和常识
- 单道题保持简洁，不把整篇文档复制进标准答案

## 七、人事复杂题的数据结构

数据集增加人事复杂题时，版本升级为 `golden_basic_20_hr_10`，并在根节点增加：

```json
{
  "knowledge_bases": {
    "hr_travel": {
      "kb_id": "实际人事差旅知识库ID",
      "corpus_version": "hr_travel_corpus_base"
    }
  },
  "suites": {
    "complex": {
      "description": "10道人事复杂题",
      "case_ids": ["travel-complex-001"]
    },
    "all": {
      "description": "20道电商基础题和10道人事复杂题",
      "case_ids": "all"
    }
  }
}
```

人事题仍只使用 `kb_alias=hr_travel`，不同时检索电商知识库。复杂题可以增加：

```json
{
  "required_aspects": [
    "确定员工职级",
    "确定出差城市类别",
    "查找对应住宿上限",
    "说明特殊情况审批规则"
  ]
}
```

`required_aspects` 只用于复杂要点覆盖和 Research 评测，不要强制所有基础题填写。

## 八、多知识库题的边界

多知识库题只在一道问题确实需要同时从两个有业务关系的知识库取证时使用。

合适的知识库组合例如：

```text
人事差旅制度 + 财务报销制度
电商交易规则 + 客服售后规则
```

不为了测试联合检索而强行组合无关的电商和人事问题。多知识库题可以保存在同一 Golden Set 中，使用 `kb_aliases` 显式标记：

```json
{
  "id": "multi-kb-001",
  "suite": "multi_kb",
  "kb_aliases": ["hr_travel", "finance_expense"],
  "question": "实际需要跨库取证的问题",
  "ground_truth": "根据两个知识库原文核对的完整答案"
}
```

`golden_basic_20` 不强制包含多知识库题。

## 九、数据集版本管理

以下情况需要生成新版本：

- 增加或删除正式题目
- 修改会影响评分的 `ground_truth`
- 修正知识库文档中的业务规则
- 增加容易混淆的测试文档
- 将人事复杂题加入正式集

以下情况不需要修改 Golden Set 版本：

- 只修改了 Query Rewrite 实现
- 只修改了召回或 RRF 参数
- 只更换了重排模型
- 只修改了报告样式
- 修正不影响评分的文字说明

保留历史数据集文件和报告。报告必须记录它实际使用的数据集版本，不允许将 `golden_basic_20` 报告和 `golden_basic_20_hr_10` 报告直接比较后得出效果提升结论。

## 十、数据集校验

新增可独立运行的校验能力，统一入口支持：

```bash
python scripts/run_eval.py validate-dataset \
  --dataset eval/datasets/golden_basic_20.json
```

校验内容至少包括：

- `schema_version`、`name`、`version` 存在
- `knowledge_bases` 不为空
- 每个知识库都有合法 `kb_id` 和 `corpus_version`
- 每个 suite 引用的 case ID 都存在
- case ID 不重复
- `question` 和 `ground_truth` 非空
- `case_type` 在允许范围内
- `kb_alias` 能够解析到知识库
- `expected_documents`、`expected_headings`、`tags` 如果存在必须是字符串数组
- `basic` 套件恰好包含 20 道题
- 电商基础题的三类数量分别为 10、5、5

校验失败时返回非 0 退出码，逐条列出错误位置，不跳过错误题继续生成正式报告。

## 十一、测试代码

新增：

```text
tests/test_eval_dataset.py
```

至少覆盖：

- 合法的 `golden_basic_20` 校验通过
- 重复 case ID 被拒绝
- 空标准答案被拒绝
- 未知 `kb_alias` 被拒绝
- suite 引用不存在的 case 被拒绝
- `case_type` 非法值被拒绝
- 可选字段类型错误被拒绝
- `basic` 套件数量不符合要求时输出明确错误

测试使用临时数据，不连接真实知识库，不调用 Embedding、LLM 或 RAGAS。

## 十二、完成标准

- `eval/datasets/golden_basic_20.json` 包含 20 道经过原文核对的电商题
- 10 道直接事实题、5 道改写同义题、5 道跨章节或跨文档题分布正确
- 每道题能够通过 `kb_alias` 路由到实际知识库
- 所有 `ground_truth` 能够在知识库原文中找到依据
- 数据集校验命令执行成功
- 数据集单测全部通过
- 数据集不包含 API Key、Header 和不必要的敏感信息
- 已有零散数据集和历史报告保留，不作为新基线的正式结论
