# Markdown 切块与表格切块提示词

点击此处可复制本篇提示词

标题切块和表格切块都落在同一个类 `MarkdownStructuredSplitter` 里：前者建好骨架、识别标题边界；后者在同一次扫描中加入表格识别分支。放在一篇里给 AI，可以在同一个文件上连续完成，不需要第二次重新理解已有结构。

## 使用场景

这份提示词用于在当前 rag-center 工程基础上，完成结构化 Markdown 切块。

**完成后**：上传 `.md` 文档时，chunk 会沿 `#` / `##` 标题边界切分，不再在小节中间截断；表格会识别为独立类型，整表或带表头分组存储，不再因字符长度拦腰切断某行。每个 chunk 的 metadata 记录所属章节路径和块类型，便于后续检索时溯源。

### 一、背景：当前切块的问题

`app/utils/text_splitter.py` 的 `TextSplitter` 只做字符滑动窗口，不感知 Markdown 结构。对含多个 `##` 小节的文档，可能在标题行中间切断；对表格，字符切会把一行数据从中间劈开，召回的 chunk 缺少表头或行数据不完整，向量语义大打折扣。

### 二、新增 MarkdownStructuredSplitter

新建 `app/utils/markdown_splitter.py`，保留旧 `TextSplitter` 供对比和测试。

**标题分节逻辑**：

按行扫描文档，维护标题栈生成 `heading_path`，如 `平台交易规则/退款时效`。遇到 `#` 或 `##` 时 flush 当前节、开启新节；`###` 及以下不触发 flush，内容归入当前节正文，避免切太碎。独立一行的 `---` 跳过，不进入任何 chunk。代码块 ` ``` ` 尽量整块保留在同一节内，不从中截断。

无标题的文档：整篇视为一节，退化为按段落或字符切，行为接近旧切法。

**单节过长时的二次切**：某节正文超过 `settings.CHUNK_SIZE` 时，在节 **内部** 按段落优先、必要时字符切；`CHUNK_OVERLAP` 仅在节内生效，不跨 `#` / `##` 边界。

输入输出：

```
输入：document.content（Markdown 字符串）
  ↓
按行扫描：维护标题栈，识别 # / ## 边界
  ↓
flush 逻辑：遇 # / ## 或文档结束时输出当前节
  ↓
若单节 <= CHUNK_SIZE → 一个 chunk
若单节 > CHUNK_SIZE  → 节内段落/字符二次切，heading_path 继承
  ↓
输出：list[SplitPiece]，含 text 和 metadata
```

各元素的处理约定：

| 元素 | 处理方式 |
|------|---------|
| `#` 一级标题 | 分节边界，触发 flush |
| `##` 二级标题 | 分节边界，触发 flush |
| `###` 及以下 | 归入当前节正文，不单独拆节 |
| 普通段落、列表 | 随当前 `#` / `##` 节 |
| 代码块 ` ``` ` | 尽量整块保留，不从中截断 |
| 表格 `\| \|` | 见第三节专项处理 |
| 独立 `---` 行 | 跳过，不进入 chunk |

### 三、表格识别与专项切法

在同一个 `MarkdownStructuredSplitter` 的节内扫描阶段，识别连续 `| ... |` 行为 Markdown 表格并进入 table 模式，表头行判断：第一行 `|` 或分隔行 `|---|` 之后的行即为 header。

切法：

- **小表**：整张表不超过 `CHUNK_SIZE` → 一个 chunk，`chunk_type=table`
- **大表**：保留表头，按每组最多 `TABLE_MAX_ROWS_PER_CHUNK` 条数据行拆分，每组 chunk 都在正文顶部重复一行表头摘要，`table_part` 字段递增，确保每个 chunk 语义完整

表头重复格式示例：

```markdown
【表头】场景 | 是否可叠加 | 说明
| 满减 + 优惠券 | 否 | 同一订单只能选一种 |
```

表格与前后段落分开成块，不强行拼合。表格结束后，后续段落回到正常段落逻辑。

**配置项**，在 `settings` 或 `.env` 增加：

| 变量 | 含义 | 默认值 |
|------|------|--------|
| `TABLE_MAX_ROWS_PER_CHUNK` | 大表每组最多数据行数，不含表头 | 10 |

### 四、IndexingService 接入

`app/services/indexing_service.py` 中 `_index_document_content` 改用 `MarkdownStructuredSplitter`：

```python
pieces = markdown_splitter.split(document.content)
for piece in pieces:
    metadata = {
        "chunk_index": index,
        "source_type": document.source_type,
        "heading_path": piece.metadata.get("heading_path"),
        "chunk_type": piece.metadata.get("chunk_type", "section"),
        "table_part": piece.metadata.get("table_part"),   # 仅表格 chunk 有值
    }
```

pgvector 和 ES 的 `add_chunks` 已有 metadata 字段，确保透传即可，不需要改索引层。

### 五、单元测试

在 `tests/` 或 `app/utils/` 下新增 `markdown_splitter` 的单测，覆盖以下场景：

- `#` / `##` 分节正确，chunk 不在标题行中间截断
- `###` 合并进所属 `##` 节，不单独成块
- 独立 `---` 被过滤，不出现在任何 chunk 正文中
- 长节内二次切，同节各 chunk 的 `heading_path` 一致
- 无标题文档退化为接近旧行为
- 小表整表一个 chunk，`chunk_type=table`
- 大表分组，每组含完整表头，`table_part` 递增
- 混合文档：标题 + 段落 + 表格 + 段落，各类型正确分开

### 六、边界说明

- 不做全量 reindex 脚本，已索引文档在 03 里处理
- 不改 retrieve API 和前端
- `faithfulness`、纯 `.txt` 文档仍走旧 `TextSplitter` 或退化为整篇一节均可

### 七、验收标准

- 上传含 `##` 小节的 `.md`，chunk 不在标题行中间断开
- 数据库 `chunks.metadata` 含 `heading_path`，格式为 `一级标题/二级标题`
- `###` 三级标题内容在所属 `##` 节的 chunk 里，不单独成块
- 独立 `---` 行不出现在任何 chunk 正文中
- 上传含表格的 `.md`，无 chunk 在表格某行中间断开
- 表格 chunk 的 `chunk_type` 为 `table`，content 含完整表头和至少一行数据
- 大表各 chunk 的 `table_part` 递增，且都重复了表头行
- 现有纯文本 `.txt` 上传仍可成功索引
