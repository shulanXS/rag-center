# 02 - Word 与 PDF 解析及验收

01 已经把 Parser 插件、upload 双模式、Celery 挂钩和存盘链路搭好了。这一步 **只填两种 Parser 实现**，并接上 **调试台上传** 和 **验收脚本**——后面切块、检索、多库仍不改。

改完以后，业务方和教程演示都可以：**直接扔 `.docx` / 文字版 `.pdf` 进知识库**，中台在 Worker 里转成 Markdown 风格文本，再走优化十一的结构化切块。

## Word 怎么解析

推荐 **mammoth**：专门把 `.docx` 转成 HTML 或 **Markdown**，不依赖 Microsoft Office，纯 Python 可用。

```python
# app/providers/parsers/docx.py
import mammoth

async def parse(self, file_bytes: bytes, *, filename: str) -> ParsedDocument:
    result = mammoth.convert_to_markdown(io.BytesIO(file_bytes))
    warnings = [w.message for w in result.messages]
    return ParsedDocument(
        content=result.value.strip(),
        source_type="docx",
        metadata={"parser": "mammoth", "warnings": warnings},
    )
```

**预期效果：** Word 里用「标题 1/2」样式的段落会输出 `#` / `##`；表格输出为 `| |`，转换效果不完美，但多数制度文档够用。

**不做什么：** 老式 `.doc`（非 docx）、嵌入 OLE 对象、复杂文本框——遇到可 FAILED 并提示「请另存为 docx」。

## PDF 怎么解析（文字版）

推荐 **PyMuPDF（fitz）**：抽文字快，不装 OCR。

```python
# app/providers/parsers/pdf.py
import fitz

async def parse(self, file_bytes: bytes, *, filename: str) -> ParsedDocument:
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    parts = []
    for i, page in enumerate(doc):
        text = page.get_text("text").strip()
        if text:
            parts.append(f"## 第 {i + 1} 页\n\n{text}")
    content = "\n\n".join(parts)
    return ParsedDocument(
        content=content,
        source_type="pdf",
        metadata={"parser": "pymupdf", "page_count": len(doc)},
    )
```

**为什么按页加 `##`：** PDF 往往没有可靠的 `#` 标题；按页分节能让优化十一至少按「页」切节，避免整本书粘成一块。版式里有真实标题的 PDF，后续可迭代启发式，本期不追求。

**不做什么：**

- 纯图片扫描件 → 抽出来是空的或乱码，应 FAILED 并 msg 提示「疑似扫描件，本期不支持 OCR」
- 多栏学术论文、复杂表格 → 不保证顺序，验收用 **版式简单的制度 PDF**

## registry 注册

`app/providers/parsers/registry.py`：

```python
_PARSERS = [
    DocxParser(),
    PdfParser(),
    MarkdownParser(),  # .md / .txt
]
```

扩展名映射：`.docx` → DocxParser，`.pdf` → PdfParser，`.md`/`.txt` → MarkdownParser。

## 依赖安装

```bash
pip install -e ".[parsers]"
```

`pyproject.toml`：

```toml
parsers = ["mammoth>=1.6.0", "pymupdf>=1.24.0"]
```

## 调试台怎么改（只动上传页）

优化十二 **只改上传入口**，检索页 `/retrieve`、列表页树形结构 **不必为 Parser 专门改**（可选在文档行展示 `source_filename`）。

### `frontend/src/utils/file.ts`

- `isSupportedUploadFile`：增加 `.pdf`、`.docx`（保留 `.md`、`.txt`）
- 新增 `isBinaryUploadFile(file)`：pdf/docx 走 FormData，md/txt 仍可 readAsText + JSON

### `frontend/src/services/documentService.ts`

```ts
// 二进制
export async function uploadDocumentFile(formData: FormData) { ... }

// 或统一 uploadDocument(payload | FormData) 内部分支
```

multipart 字段：`file`、`kb_id`、可选 `title`；**不要**把 PDF base64 塞进 JSON。

### `frontend/src/hooks/useUploadDocuments.ts`

```
if (isBinaryUploadFile(file)) → uploadDocumentFile(FormData)
else → readFileAsText → uploadDocument(JSON)  // 与现在一致
```

### 可选 UI

- 选中 `.pdf`/`.docx` 时提示：「将在后台解析，稍大文件需等待索引完成」
- 列表页 Document 行 Badge：`docx` / `pdf` / `md`，需后端 tree 或 detail 接口返回 `source_type`

## reindex 与 reparse

Parser 升级或切块策略变更，两种重建方式：

| 方式 | 何时用 |
|------|--------|
| 默认 reindex | 从已有 `documents.content` 重新切块（优化十一 03 脚本） |
| `reparse=true` | 从 `source_file_path` **重新 parse** 再切块（Parser 逻辑升级后） |

`POST /documents/{id}/reindex?reparse=true`：仅当 `source_file_path` 存在时有意义；JSON 纯文本 upload 忽略该参数。

全量脚本 `reindex_knowledge_base.py` 可选 `--reparse`，与单篇 reindex 语义一致。

## 给 AI 的提示词

把下面整段复制给 AI，一步完成开发：

```
在 rag-center 项目完成「优化十二 - 02 Word 与 PDF 解析及验收」（依赖 01 已完成）。

【背景】
01 已搭好 Parser 插件、upload 双模式、Celery 挂钩。本篇只实现 Word/PDF 两种 Parser + 调试台 FormData + 验收。
解析结果写入 documents.content 后，走优化十一切块 → 现有索引，retrieve 无感。

【1. Parser 实现】
- app/providers/parsers/docx.py：mammoth.convert_to_markdown，ParsedDocument source_type=docx
  （Word 标题/表格尽量变成 # 和 | |，供 MarkdownStructuredSplitter 识别）
- app/providers/parsers/pdf.py：fitz 按页 get_text，页首 ## 第 N 页，source_type=pdf
  （本期只做文字 PDF；无 OCR）
- registry 注册 .docx、.pdf；空 content 或 parse 异常 → DOCUMENT_PARSE_FAILED，文档 FAILED
- PDF 抽文本为空 → FAILED，msg 提示疑似扫描件

【2. 依赖】
- pyproject.toml [project.optional-dependencies] parsers：mammoth、pymupdf
- README 补充：pip install -e ".[parsers]"

【3. 前端上传页】
- file.ts：支持 .pdf/.docx；区分 binary vs text
  （pdf/docx 不能 readAsText，必须 FormData；md/txt 可继续 JSON 上传）
- documentService：multipart upload 同 path /api/v1/documents/upload
- useUploadDocuments：pdf/docx 走 FormData，md/txt 保持 JSON
- KnowledgeUploadPage：文件选择 accept 含 .pdf,.docx

【4. reparse（可选但推荐）】
- POST /documents/{id}/reindex?reparse=true：有 source_file_path 时从原文件重新 parse 再 index
  （Parser 升级后用；默认 reindex 仍只读已有 content）
- scripts/reindex_knowledge_base.py 增加 --reparse

【5. 验收脚本】
- scripts/verify_parser_12.py：
  - 准备 sample.docx、sample.pdf（可文档内说明用 data/ 下样例或临时生成）
  - multipart upload → 等 SUCCESS → retrieve 抽关键词
  - JSON .md 回归一条
  - 打印通过

【边界】
- 不改 MarkdownStructuredSplitter、RagService、多库逻辑
- 扫描 PDF 本期不支持，测了应 FAILED 而非 silent 空库
- HTML/Excel 等留待后续新 Parser 插件，不在本篇范围

【验收】
- 调试台上传 docx/pdf → SUCCESS → retrieve 有 chunk
- curl multipart 与前端一致
- verify_parser_12.py  exit 0
- 优化十一 JSON upload 回归仍通过
```

## 做完怎么验

前置：`pip install -e ".[parsers]"`，Celery Worker 已启动。

### 1. Word 上传 + 检索

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/documents/upload \
  -H "Authorization: Bearer rk_live_你的key" \
  -F "kb_id=<kb_id>" \
  -F "file=@./sample.docx"
```

轮询 `GET /documents/{id}` 至 SUCCESS。retrieve：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/rag/retrieve \
  -H "Authorization: Bearer rk_live_你的key" \
  -H "Content-Type: application/json" \
  -d '{"kb_id":"<kb_id>","user_id":"demo","query":"<docx里某关键词>","profile":"speed"}'
```

预期：有召回 chunk；metadata 里可有 `heading_path`（若 docx 标题转成了 `#`）。

### 2. PDF 上传 + 检索

同上，换 `sample.pdf`（**文字版**，非扫描）。预期 SUCCESS + 能 retrieve。

### 3. 扫描件 PDF（应失败）

上传纯图片 PDF。预期：`status=FAILED`，`error_message` 含解析失败或内容为空提示。

### 4. 调试台

上传页选择 `.docx` 和 `.md` 各一篇 → 列表页均 SUCCESS → 检索页能搜到。

### 5. 跑验收脚本

```bash
python scripts/verify_parser_12.py --api-key rk_live_你的key --kb-id <kb_id>
```

预期：终端打印通过。

### 6. JSON 回归

再跑 `scripts/verify_chunking_11.py` 或 01 中的 JSON curl，确认 md 文本路径未破坏。

---

优化十二至此闭环：**00 定边界 → 01 插件与入库 → 02 Word/PDF + 调试台 + 验收**。

下一步进入 **优化十三 - 多知识库联合检索**（与文档格式无关，retrieve 侧扩展）。
