# 01 - Markdown 按标题层级切块

00 说明了为什么要做结构化切块。这一步先从 **最常见、也最好验证** 的部分入手：**按 Markdown 一级、二级标题（`#` / `##`）切分**，让 chunk 尽量落在同一小节内，并在 metadata 里记录「这段属于哪一节」。

00 不动索引代码；本篇只改 **分块逻辑 + chunk metadata**，检索 API 和前端暂时不用动。

## 现在的问题

`app/utils/text_splitter.py` 里的 `TextSplitter` 只做字符滑动窗口：

```python
# 现状（简化）
while start < len(normalized):
    end = start + self.chunk_size
    chunks.append(normalized[start:end])
    start += step
```

对下面这种文档：

```markdown
# 平台交易规则

## 退款时效

用户可在订单完成后 7 天内申请退款。

## 特殊商品

以下商品不支持无理由退款：……
```

可能在 `## 退` 和 `款时效` 之间切一刀，或者把「退款时效」段落和「特殊商品」段落混进同一个 chunk。

## 目标行为

1. **按 `#` / `##` 分节**：遇到一级或二级标题才 flush；`###` 及以下作为节内正文，不单独成节
2. **单节过长时再二次切**：某一节超过 `chunk_size` 时，在该节 **内部** 按段落或字符切，但 **不跨 `#` / `##` 边界**
3. **metadata 带上路径**：维护标题栈生成路径，例如 `heading_path: "平台交易规则/退款时效"`
4. **过滤水平分割线**：独立一行的 `---` 跳过，不进入 chunk（避免无意义短块）

> **和旧字符切 / 初版实现的区别：** 原先或初版结构化切块实现中，`#`～`######` **每一级标题都会拆节**，三级标题也会单独成块，容易切太碎。本期收敛为 **只在 `#`、`##` 两级拆节**。

切完后的 chunk 示例：

| content（摘要） | metadata |
|-----------------|----------|
| `## 退款时效\n\n用户可在…` | `heading_path: "平台交易规则/退款时效"`, `chunk_type: "section"` |
| `## 特殊商品\n\n以下商品…` | `heading_path: "平台交易规则/特殊商品"`, `chunk_type: "section"` |

## 实现思路

新增 `MarkdownStructuredSplitter`，也可以重构现有 `TextSplitter`，建议放在 `app/utils/markdown_splitter.py`：

```
输入：document.content（Markdown 字符串）
  ↓
按行扫描：跳过独立 --- 行；识别 # ~ ###### 维护标题栈（heading_path）
  ↓
仅 level 1（#）或 level 2（##）时 flush 分节；### 及以下写入当前节正文
  ↓
累积段落文本到「当前节」缓冲区
  ↓
遇到新 # / ## or 文档结束 → flush 当前节
  ↓
若单节 len <= chunk_size → 一个 chunk
若单节 len > chunk_size  → 节内按段落/字符二次切，metadata 继承同一 heading_path
  ↓
输出：list[{ text, metadata }]
```

**边界约定（本篇范围）：**

| 元素 | 本篇处理 |
|------|----------|
| `#` 一级标题 | ✅ 作为分节边界（通常为文档标题节） |
| `##` 二级标题 | ✅ 作为分节边界（章节） |
| `###` 及以下 | ✅ 作为节内正文，不 flush |
| 普通段落、列表 | ✅ 随当前 `#` / `##` 节一起 |
| 代码块 ` ``` ` | ✅ 尽量不从中切开（整块保留在同一节内；过长则 02 前允许节内字符切） |
| 表格 `\| \|` | 本篇先 **随节走**；02 专门优化 |
| 水平分割线 `---` | ✅ 跳过（不进入 chunk） |

索引入口 `IndexingService._index_document_content` 改为：

```python
# 概念
pieces = markdown_splitter.split(document.content)
for piece in pieces:
    metadata = {
        "chunk_index": index,
        "source_type": document.source_type,  # 仍为 text
        "heading_path": piece.metadata.get("heading_path"),
        "chunk_type": piece.metadata.get("chunk_type", "section"),
    }
```

## 和现有配置的关系

| 配置 | 行为 |
|------|------|
| `CHUNK_SIZE` | 单节 **内部** 二次切的上限 |
| `CHUNK_OVERLAP` | 节内二次切时的重叠；**不跨 `#` / `##` 边界** |
| 无标题的文档 | 退化为整篇当作一节，行为接近现在，但可按段落切 |

## 给 AI 的提示词

把下面整段复制给 AI，一步完成开发：

```
在 rag-center 项目完成「优化十一 - 01 Markdown 按标题层级切块」。

背景：当前 app/utils/text_splitter.py 仅按 CHUNK_SIZE/CHUNK_OVERLAP 字符切；
IndexingService 对 .md 和 .txt 无区别。本篇实现 Markdown 标题感知分块。

【1. 新增 MarkdownStructuredSplitter】
- 新文件 app/utils/markdown_splitter.py（或重构 text_splitter，保留旧类供对比/测试）
- 输入：str；输出：list[SplitPiece]，含 text + metadata（heading_path, chunk_type）
- 识别 # ~ ###### 维护 heading 栈生成 heading_path（如 "文档标题/二级节"）
- **`#` 与 `##` 作为分节边界 flush**；`###` 及以下写入当前节正文
- 跳过独立一行的 `---`（水平分割线），不进入 chunk
- 单节超过 settings.chunk_size 时在节内二次切（段落优先，必要时字符切）
- overlap 仅在节内生效，不跨 `#` / `##`
- 无标题文档：整篇为一节，按段落/字符切
- 代码块 ``` 尽量不从中切开

【2. IndexingService 接入】
- app/services/indexing_service.py：_index_document_content 改用 MarkdownStructuredSplitter
- chunk metadata 写入：chunk_index, source_type, heading_path, chunk_type
- pgvector / ES add_chunks 已有 metadata 字段，确保透传

【3. 测试】
- tests/ 或 app/utils/ 下增加 markdown_splitter 单测：
  - `#` / `##` 分节不跨节；`###` 合并进所属 `##` 节
  - `---` 被过滤，不单独成 chunk
  - 长节二次切且 heading_path 一致
  - 无标题退化为近似旧行为

【边界】
- 不做表格专项（02）
- 不做 reindex 脚本（03）
- 不改 retrieve API / 前端

【验收】
- 上传含 ## 小节的 .md，chunk 不在标题行中间断开
- 数据库 chunks.metadata 含 heading_path
- 现有 .txt 纯文本上传仍可成功索引
```

## 做完怎么验

前置：PostgreSQL、Redis、Celery Worker、后端均已启动。

### 1. 准备测试文档

新建测试 Markdown（或通过 upload API 上传）：

```markdown
# 测试知识库

## 第一节

这是第一节的内容，用于验证 heading_path。

## 第二节

这是第二节的内容，应与第一节分开切块。
```

### 2. 上传并等待索引完成

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/documents/upload \
  -H "Authorization: Bearer rk_live_你的key" \
  -H "Content-Type: application/json" \
  -d '{
    "kb_id": "<kb_id>",
    "title": "heading_test.md",
    "content": "# 测试知识库\n\n## 第一节\n\n这是第一节的内容。\n\n## 第二节\n\n这是第二节的内容。"
  }'
```

轮询 `GET /documents/{document_id}` 直到 `status=1`（SUCCESS）。

### 3. 查 chunk metadata

在 PostgreSQL 中（或通过调试台 retrieve 看召回）确认：

- 至少 2 个 chunk（两节分开）
- `metadata.heading_path` 分别为 `测试知识库/第一节`、`测试知识库/第二节`
- 无 chunk 在 `## 第` 这种位置截断标题
- 无仅含 `---` 的无意义 chunk

### 4. 验证 `###` 与 `---`（可选）

含三级标题与分割线的文档应表现为：

```markdown
## 仓库作业

### 4.1 出库
说明 A。

---

### 4.2 入库
说明 B。
```

- `4.1`、`4.2` 同属一个 `## 仓库作业` 节，**不会**因 `###` 再拆节
- `---` 不出现在任何 chunk 正文中

### 5. 检索验证

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/rag/retrieve \
  -H "Authorization: Bearer rk_live_你的key" \
  -H "Content-Type: application/json" \
  -d '{
    "kb_id": "<kb_id>",
    "user_id": "test",
    "query": "第一节内容",
    "profile": "balanced"
  }'
```

预期：Top 结果 content 来自第一节，且不与第二节明显混杂。

---

全部通过后，进入 **02 - 表格专项切块**：针对 `data/ecommerce` 等文档里的 Markdown 表格，避免拦腰切断并注入表头上下文。
