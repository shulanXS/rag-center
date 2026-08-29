# Markdown 感知分块：让每个 Chunk 都知道自己在哪里

上一篇讲了 rag-center 如何用 Celery 把文档处理从请求链路里分离出去。Celery worker 完成文件解析之后，下一步是分块。DocumentParser 负责把不同格式的原始文件转为结构化文本，分块则负责把这份文本切成可检索的 chunk。这一步看似简单，实则是检索质量好坏的关键所在。

## 朴素切块的问题

最直接的分块方式是按字符数截断，每隔 N 个字符切一刀，相邻 chunk 之间保留一定 overlap。`rag-center/app/utils/text_splitter.py` 提供了这种朴素策略；但**生产索引链路统一使用** `MarkdownStructuredSplitter`，对 Markdown 结构做感知分块。

这种做法有其合理之处，简单可控，参数容易调。但对于结构化的技术文档，它会产生一个根本性的问题：**切刀不认识文档结构**。

以一篇有多个章节的产品手册为例。按字符数切，可能把"退款政策"这一节的开头和上一节"支付方式"的末尾拼在同一个 chunk 里——这个 chunk 在语义上混杂了两个主题，向量化之后的嵌入向量介于两者之间，对任何一个主题的检索都不是最优命中。

更严重的是表格。表格的每一行依赖表头才有意义，如果一张表格被从中间截断，后半部分的 chunk 里只有数据行，没有表头，完全失去了可解释性。

下面用同一段文档、`chunk_size=80` 的 `TextSplitter` 说明朴素切块实际会切成什么样：

**原始 Markdown：**

```markdown
## 支付方式

目前支持三种支付方式：微信、支付宝和银行卡。

## 退款政策

### 线上退款

提交申请后，3-5 个工作日到账。

| 退款方式 | 到账时间 |
|--------|--------|
| 原路退回 | 3-5 工作日 |
| 余额退款 | 即时到账 |
```

**朴素切块结果（按字符数硬切，不认标题和表格边界）：**

```
Chunk 1: "目前支持三种支付方式：微信、支付宝和银行卡。\n\n## 退款政策\n\n### 线上退款\n"
Chunk 2: "提交申请后，3-5 个工作日到账。\n\n| 退款方式 | 到账时间 |\n|--------|--------|\n"
Chunk 3: "| 原路退回 | 3-5 工作日 |\n| 余额退款 | 即时到账 |"
```

三个问题一目了然：Chunk 1 把「支付方式」正文和「退款政策」标题拼在一起，语义混杂；Chunk 2 在表格中间下刀；Chunk 3 只剩数据行、没有表头，单独检索时无法知道各列含义。朴素策略的实现也很直白——纯按字符窗口滑动，与文档结构无关：

```python
# rag-center/app/utils/text_splitter.py

class TextSplitter:
    def split(self, text: str) -> list[str]:
        # 按 chunk_size 滑动窗口切分，chunk_overlap 控制相邻块重叠
        step = max(self.chunk_size - self.chunk_overlap, 1)
        while start < len(normalized):
            chunks.append(normalized[start : start + self.chunk_size].strip())
            start += step
```

## Markdown 感知分块的核心思路

`rag-center/app/utils/markdown_splitter.py` 实现了 `MarkdownStructuredSplitter`，核心逻辑分两步走：

**第一步，按标题层级划分逻辑段落。** 仅在 `#` / `##` 级标题处切分 section（`SECTION_SPLIT_MIN_LEVEL=1`，`SECTION_SPLIT_MAX_LEVEL=2`），维护 `heading_path`（以 `/` 连接，如 `退款政策/线上退款`）。更深层的 `###` 标题保留在 section 正文中，不单独切段。

**第二步，对超长 section 按块类型二次细切。** 每个 section 先拆成 prose / code / table 三类块；prose 按段落聚合，超长再按字符 overlap 切；table 按 `table_max_rows_per_chunk` 分行组块，并附带 `【表头】` 摘要，保证每块可读。

```python
# rag-center/app/utils/markdown_splitter.py

@dataclass(slots=True)
class SplitPiece:
    text: str
    metadata: dict = field(default_factory=dict)   # heading_path、chunk_type 等

class MarkdownStructuredSplitter:
    def split(self, text: str) -> list[SplitPiece]:
        sections = self._split_into_sections(normalized.split("\n"))
        pieces: list[SplitPiece] = []
        for section in sections:
            pieces.extend(self._chunk_section(section))   # 按 prose/code/table 分别处理
        return [piece for piece in pieces if piece.text.strip()]

    def _split_into_sections(self, lines: list[str]) -> list[_Section]:
        heading_stack: list[tuple[int, str]] = []
        # ...
        for line in lines:
            match = HEADING_RE.match(line)
            if match:
                level = len(match.group(1))
                # 弹出同级及更深层标题，维护正确的路径栈
                while heading_stack and heading_stack[-1][0] >= level:
                    heading_stack.pop()
                heading_stack.append((level, title))
                heading_path = "/".join(item[1] for item in heading_stack)
                # 仅在 h1/h2 处 flush section
                if SECTION_SPLIT_MIN_LEVEL <= level <= SECTION_SPLIT_MAX_LEVEL:
                    flush()
                    current_heading_line = line
                    current_heading_path = heading_path
```

两步结合，既保证了语义边界的清晰，又控制了每个 chunk 的长度不超过 embedding 模型的输入限制。

## heading_path：给每个 Chunk 定位

分块之后，每个 chunk 以 `SplitPiece` 返回，元数据写入 `metadata` 字典，随 chunk 一并入库：

```python
# rag-center/app/services/indexing_service.py（索引时组装 chunk）

chunks = [{
    "content": piece.text,
    "metadata": {
        "chunk_index": index,
        "source_type": document.source_type,
        **piece.metadata,   # heading_path、chunk_type、table_part 等
    },
    # ...
} for index, piece in enumerate(pieces)]
```

`heading_path` 示例：

```
退款政策/线上退款
```

`heading_path` 由 `_split_into_sections` 在遍历标题时逐层维护，遇到同级或更高层级的新标题时弹出栈顶，始终保持当前正确的层级链路。

这条元数据在检索时有两个用途：

- **辅助排序和过滤**：用户问"线上退款需要几天"，向量检索命中若干 chunk，可以用 heading_path 做后处理，优先展示路径里包含"退款"的 chunk，降低不相关章节混入结果的概率
- **生成引用来源**：LLM 回答时把 `heading_path` 拼接成「参考来源：退款政策 / 线上退款」，告诉用户这条信息来自哪里

## 表格的特殊处理

表格是 Markdown 文档里最容易被切坏的结构。`MarkdownStructuredSplitter` 将表格识别为独立块（`chunk_type: table`），**优先保持表头完整**：小表整表成一个 chunk；超长表按 `table_max_rows_per_chunk`（默认 10 行）分组，每组 chunk 都附带 `【表头】列名摘要` 和 GFM 表头行，避免数据行失去上下文。

代码块（`chunk_type: code`）同样单独识别，围栏内内容不会被段落切分逻辑打断。

仍以上文退款表格为例，感知分块后表格单独成块，并保留表头语义：

```
Chunk（table）:
  text: "【表头】退款方式 | 到账时间
         | 退款方式 | 到账时间 |
         |--------|--------|
         | 原路退回 | 3-5 工作日 |
         | 余额退款 | 即时到账 |"
  metadata.heading_path: "退款政策/线上退款"
  metadata.chunk_type: table
```

正文段落则按章节边界切分，各自带上 `heading_path`，不会与相邻章节或表格混在同一 chunk 里。

## 对检索质量的影响

chunk 的质量直接决定 embedding 向量的质量。语义混杂的 chunk，其向量是多个话题的"平均值"，对每个话题的检索都不精准。语义内聚的 chunk，其向量集中在一个语义空间，命中率更高。

实际测试中，Markdown 感知分块在包含多章节结构的技术文档上，检索召回率明显优于朴素切块。代价是实现复杂度更高——需要维护标题栈、块类型识别和表格分行策略。

分块质量影响的不只是检索，还会影响到重索引策略的设计。当分块参数需要调整时，如何让已入库的文档用上新参数？这是下一篇要讲的内容。
