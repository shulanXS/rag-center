# 03 - 全量 reindex

01、02 改的是 **新上传文档** 的分块方式。已经索引过的知识库，chunk 仍是旧的字符切——**必须 reindex，否则线上检索还是旧块。**

本篇提供 **按知识库/文档批量重建索引** 的能力，让已有文档也能用上新的结构化切块。效果验证可继续用优化十的离线测评或调试台检索，**本篇不做 RAGAS 流程**。

## 为什么必须 reindex

```
改 splitter 代码
    ↓
仅影响新 upload 的文档
    ↓
旧文档的 chunks 仍在 pgvector / ES 里
    ↓
retrieve 仍召回旧块 → 体感「改了没效果」
```

切块策略升级后，**reindex 不是可选项**。

## reindex 策略

两种粒度，建议都支持：

| 粒度 | 场景 | 做法 |
|------|------|------|
| **单文档** | 某文档需强制重建、失败重试 | 已有 `POST /documents/{id}/reindex`（仅 FAILED）；扩展允许 **SUCCESS** 文档强制重建 |
| **整库** | 切块策略升级后一次性切换 | 新增脚本：对 kb 下所有 SUCCESS 文档 purge + 重新 index |

推荐新增脚本（不强制 Admin UI）：

```bash
python scripts/reindex_knowledge_base.py --kb-id <kb_id>
```

脚本直连 DB + Celery，无需 HTTP API Key。内部流程：

```
列出 kb 下 status=SUCCESS 的 documents
  → 逐个 purge_document_chunks（pgvector + ES）
  → status 置 PROCESSING
  → index_document_task.delay(document_id)
  → Worker 用新 splitter 索引
```

**注意：**

- 整库 reindex 期间，已 purge 的文档会 **短暂检索不到**，建议在低峰或文档量小时执行
- `PROCESSING` 中的文档不可重复 reindex
- 原文从 `documents.content` 读取，**不需要**重新上传文件

## 调试台（可选小改）

检索结果 Card 里，若 chunk metadata 含 `heading_path`，只读展示一行：

```
所属章节：平台交易规则 / 退款时效
块类型：table
```

方便目视确认 reindex 后 chunk 是否符合 01/02 规则。不做复杂 UI。

## 给 AI 的提示词

```
在 rag-center 项目完成「优化十一 - 03 全量 reindex」（依赖 01、02 已完成）。

【1. 批量 reindex 脚本】
- 新增 scripts/reindex_knowledge_base.py
- 参数：--kb-id 必填；可选 --tenant-id（未传则从 kb 反查）
- 对 kb 下所有 SUCCESS 文档：purge_document_chunks → status=PROCESSING → index_document_task.delay
- 日志输出进度；单文档失败不阻断其余（记录 failed document_id）
- 结束时打印 success / failed / skipped 统计

【2. 单文档强制 reindex（二选一）】
- 方案 A：脚本支持 --document-id 可多次传入
- 方案 B：扩展 POST /documents/{id}/reindex 允许 SUCCESS 状态（Quota 校验），与 FAILED 重试共用 purge 逻辑
- PROCESSING 中拒绝 reindex

【3. 文档】
- README 增加「切块策略变更后 reindex」小节与脚本用法

【4. 前端可选】
- RetrievePage 召回列表展示 heading_path / chunk_type（只读）

【边界】
- 不改 RAGAS / eval 脚本
- 不做 embedding 模型切换

【验收】
- 整库 reindex 后各文档 SUCCESS，chunk metadata 含 heading_path / chunk_type（表格含 table）
- 调试台 retrieve 可见新切块结果
```

## 做完怎么验

前置：PostgreSQL、Redis、Celery Worker、后端均已启动。

### 1. 对测试库整库 reindex

```bash
python scripts/reindex_knowledge_base.py --kb-id <ecommerce_kb_id>
```

确认 Celery Worker 日志中各文档 `index_existing_document_success`，无大量 FAILED。

### 2. 抽查 chunk metadata

任选一篇曾用旧切块索引的文档，reindex 后在调试台 retrieve，或查库 `chunks.metadata`：

- 含 `heading_path`（有 `#` / `##` 的 md；`###` 随所属 `##` 节）
- 表格文档含 `chunk_type: "table"` 且 content 带 `【表头】`
- 无仅含 `---` 的无意义 chunk

### 3. 检索目视

对「满减和优惠券能否叠加」「第一节内容」类 query 检索，Top chunk 应为完整小节或完整表格行，不应出现标题/表格拦腰切断的残片。

---

优化十一至此闭环：**01 按 # / ## 切块 → 02 表格 → 03 全量 reindex**。  
效果量化可另行用 **优化十** 离线测评；下一步进入 **优化十二 - 多格式文档解析**（01 插件与入库，02 Word/PDF 与验收）。多库检索见 **优化十三**。
