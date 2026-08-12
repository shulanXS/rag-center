# 全量 reindex 提示词

点击此处可复制本篇提示词

01 和 02 改的是新上传文档的分块方式，已经索引过的文档 chunk 仍是旧的字符切，线上检索还是旧块。reindex 是切块策略升级后的收尾动作——不跑它，之前的工作等于只对新文档生效。

## 使用场景

这份提示词用于在 Markdown 切块和表格切块完成后，对已有知识库批量重建索引，让旧文档也用上新的结构化切法。同时在调试台检索结果里展示 `heading_path` 和 `chunk_type`，方便目视确认 reindex 效果。

**完成后**：一条命令对指定知识库下所有已成功索引的文档跑 reindex；调试台召回的 chunk 卡片上可以看到所属章节和块类型，不需要手动查库确认。

### 一、为什么必须 reindex

切块逻辑的改动只影响新上传的文档。已有文档的 chunk 仍存在 pgvector 和 ES 里，内容是旧的字符切结果。不 reindex，retrieve 召回的还是旧块，改了切法也感受不到效果。

```
改 splitter 代码
    ↓
仅影响新 upload 的文档
    ↓
旧文档 chunks 仍在 pgvector / ES 里
    ↓
retrieve 仍召回旧块 → 体感「改了没效果」
```

### 二、批量 reindex 脚本

新增 `scripts/reindex_knowledge_base.py`，参数：

| 参数 | 必填 | 说明 |
|------|------|------|
| `--kb-id` | 是 | 目标知识库 ID |
| `--tenant-id` | 否 | 未传时从知识库记录反查 |
| `--document-id` | 否 | 可多次传入，只 reindex 指定文档 |

脚本直连 DB 和 Celery，不需要 HTTP API Key。内部流程：

```
查出 kb 下 status=SUCCESS 的文档（或 --document-id 指定的文档）
  ↓
逐个 purge_document_chunks（清除 pgvector + ES 里的旧 chunk）
  ↓
文档 status 置 PROCESSING
  ↓
index_document_task.delay(document_id)
  ↓
Worker 用新 MarkdownStructuredSplitter 重新索引
```

执行过程中打印每篇文档的进度；单篇失败时记录 document_id，不阻断其余文档继续执行。结束时打印 success、failed、skipped 三项统计。

**注意事项**：

- 已 purge 的文档在 Worker 索引完成前短暂检索不到，建议在低峰期或文档量小时执行
- `PROCESSING` 中的文档拒绝重复 reindex，脚本计入 skipped
- 原文从 `documents.content` 字段读取，不需要重新上传文件

### 三、单文档强制 reindex

旧接口 `POST /documents/{id}/reindex` 只允许 `FAILED` 状态的文档重试。扩展为允许 `SUCCESS` 状态也可强制重建，与 FAILED 重试共用同一套 purge → PROCESSING → Celery task 逻辑，`PROCESSING` 中的文档仍拒绝重入。

### 四、调试台展示 chunk 元信息

`RetrievePage` 召回列表中，每条 chunk 卡片若 metadata 含 `heading_path`，在 content 折叠区上方增加一行只读展示：

```
所属章节：平台交易规则 / 退款时效    块类型：table
```

两个字段都有值才显示，不做复杂 UI，不影响其他召回信息的展示。

### 五、文档

`README` 增加「切块策略升级后 reindex」小节，说明脚本用法和执行时机建议。

### 六、边界说明

- 不改 RAGAS 和 eval 脚本，效果量化走优化十的离线测评
- 不做 embedding 模型切换
- 不做 Admin 后台的 reindex 页面，脚本即入口

### 七、验收标准

- 对含 `#` / `##` 的 `.md` 知识库跑脚本，执行后各文档状态恢复 `SUCCESS`
- 抽查 `chunks.metadata` 含 `heading_path` 和 `chunk_type`，表格文档含 `table` 类型
- 调试台检索结果里可看到「所属章节」和「块类型」展示
- 单文档强制 reindex：`SUCCESS` 状态的文档调接口后重新索引，新 chunk 替换旧 chunk
- `PROCESSING` 中的文档调脚本或接口均被拒绝，统计进 skipped
- 脚本单篇失败不中断，结束时打印三项统计数字
