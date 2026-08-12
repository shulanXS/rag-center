# 01 - DocumentParser 插件与入库链路改造

优化十一之后，切块层已经就绪，但 upload 仍只收 JSON 文本。这一步把 **DocumentParser 插件** 和 **入库编排** 搭好：二进制文件能进来，在 Celery Worker 里解析成 Markdown 风格字符串写入 `documents.content`，然后 **完全走现有的** `index_existing_document` 切块索引流程。

改完以后，链路变成：

```
upload（JSON 或 multipart）
  → 建 Document（可能暂存原文件路径）
  → Celery index_document_task
       → prepare_document_content()   ← 本篇唯一挂钩
       → 现有：split → embed → pgvector + ES
```

检索、知识库、配额、profile **本篇一律不碰**。

## 要解决什么问题

现在 `POST /documents/upload` 只接受：

```json
{ "kb_id": "...", "title": "...", "content": "长字符串" }
```

`source_type` 在代码里写死 `"text"`，没有「原文件在哪、要不要 parse」的概念。要支持 PDF/Word，需要：

1. **能收文件**（multipart），同时 **保留 JSON** 给 `.md`/`.txt` 和 curl 脚本
2. **解析逻辑集中在一处**，不散落在 Service、Task、Route 里各写一点
3. **解析失败** 有明确文档状态（`FAILED` + `error_message`），而不是 silent 空 content

## 核心设计：一个入口、一个出口

### ParsedDocument 契约

所有 Parser 输出统一形状：

```python
@dataclass
class ParsedDocument:
    content: str           # Markdown 风格纯文本，给 splitter 用
    source_type: str       # text | markdown | docx | pdf
    metadata: dict         # page_count, parser_name, warnings...
```

### DocumentParser 插件

```
app/providers/parsers/
  base.py          # DocumentParser ABC
  registry.py      # get_document_parser(filename, mime_type)
  markdown.py      # .md / .txt 归一化（\r\n → \n，trim）
  noop.py          # 不支持的扩展名 → 明确报错
```

Word、PDF 具体实现在 **02** 注册进 registry；本篇先把 **接口、registry、入库编排、upload、DB 字段** 写好，02 只「填两种 Parser」。

### prepare_document_content（唯一编排函数）

建议放在 `app/services/document_ingestion.py`（或 `app/ingestion/prepare.py`）：

```python
async def prepare_document_content(
    *,
    content: str | None,
    file_path: str | None,
    filename: str | None,
) -> ParsedDocument:
    """
    - 若已有非空 content（JSON 文本 upload）→ pass-through，source_type=text/markdown
    - 若有 file_path → 读 bytes → registry 选 Parser → parse
    - 二者都没有或 parse 结果为空 → 抛 DOCUMENT_CONTENT_EMPTY 或 DOCUMENT_PARSE_FAILED
    """
```

**Celery 任务里只调这一函数**，不要在 `IndexingService` 里 if/else 文件类型。

### index_existing_document 怎么挂钩

在 `IndexingService.index_existing_document` 里，**在** `if not document.content.strip()` **之前**插入：

```
若 document.source_file_path 存在且 content 为空（或 reparse 标志）：
    parsed = await prepare_document_content(...)
    document.content = parsed.content
    document.source_type = parsed.source_type
    commit
然后走现有 _index_document_content(document)
```

JSON 文本 upload：`content` 已有值，不读文件、不进 Parser，最多做一次 markdown 归一化，行为与优化十二之前一致。

## upload 接口：一个 URL，两种 Content-Type

**不新增** `/upload-file`，仍用 `POST /api/v1/documents/upload`：

| Content-Type | 请求体 | 行为 |
|--------------|--------|------|
| `application/json` | `{ kb_id, title, content }` | 与现在完全一致 |
| `multipart/form-data` | `file` + `kb_id` + 可选 `title` | 存原文件，content 先空，Worker 里 parse |

路由层读 `Request.headers["content-type"]` 分支，或拆两个内部 handler，对外同一个 path。

multipart 时：

- `title` 默认取文件名（去扩展名）
- 校验扩展名 `.pdf` / `.docx`（02 实现 Parser 后再放开；本篇可先存盘 + 占位失败）
- 校验文件大小（如 `DOCUMENT_MAX_SIZE_MB`，默认 20）

## 数据层扩展

Alembic 新迁移，给 `documents` 表增加（命名与现有风格一致）：

| 字段 | 说明 |
|------|------|
| `source_file_path` | 原文件相对路径，nullable；JSON 文本 upload 为 null |
| `source_filename` | 原始文件名，便于调试台展示 |

已有 `source_type`：从写死 `"text"` 改为按来源赋值 `text` / `markdown` / `docx` / `pdf`。

**原文件存哪：** 本地目录即可，如 `{DOCUMENT_STORAGE_PATH}/{tenant_id}/{document_id}/{filename}`，配置项 `DOCUMENT_STORAGE_PATH=./data/uploads`。生产可换对象存储，Parser 只认 `file_path` 读 bytes。

## 配置项

`app/core/config.py` 增加：

```env
DOCUMENT_STORAGE_PATH=./data/uploads
DOCUMENT_MAX_SIZE_MB=20
```

依赖 Word/PDF 库放 optional extra（02 安装）：

```toml
[project.optional-dependencies]
parsers = ["mammoth>=1.6.0", "pymupdf>=1.24.0"]
```

## 错误处理

| 场景 | 行为 |
|------|------|
| 不支持扩展名 | upload 阶段拒绝，PARAM_ERROR |
| 文件超大 | upload 阶段拒绝 |
| parse 抛错 / 结果为空 | 文档 `FAILED`，`error_message` 含解析失败原因 |
| parse 成功但切块失败 | 沿用现有索引失败逻辑 |

可在 `exceptions.py` 新增 `DOCUMENT_PARSE_FAILED`，建议码 20023，或复用 `DOCUMENT_INDEX_FAILED` 并在 msg 里写清「解析阶段失败」——实现时二选一，文档与代码一致即可。

## 给 AI 的提示词

把下面整段复制给 AI，一步完成开发：

```
在 rag-center 项目完成「优化十二 - 01 DocumentParser 插件与入库链路改造」（00 已定边界；02 再实现 Word/PDF Parser）。

【背景】
本篇只搭「解析层」基础设施：二进制/文本 → 统一 Markdown 风格字符串 → 写入 documents.content。
切块（MarkdownStructuredSplitter）、Embedding、retrieve 一律不改。02 才注册 mammoth/PyMuPDF。

【1. Parser 插件层】
- app/providers/parsers/base.py：DocumentParser ABC
  - supports(filename, mime_type) -> bool
  - async parse(file_bytes, *, filename) -> ParsedDocument
- ParsedDocument：content: str, source_type: str, metadata: dict
  （content 是 splitter 的唯一输入，不是落盘 .md 文件）
- app/providers/parsers/markdown.py：处理 .md/.txt，归一化换行，source_type markdown 或 text
- app/providers/parsers/noop.py：不支持格式抛 AppError
- app/providers/parsers/registry.py：get_document_parser(filename) 按扩展名选择；02 再注册 docx/pdf
  （registry 模式与 Embedding/LLM Provider 一致，方便以后加 HTML 等 Parser）

【2. 入库编排】
- app/services/document_ingestion.py：prepare_document_content(content, file_path, filename) -> ParsedDocument
  （全项目唯一解析入口，禁止在 Route/Task 里散落 if pdf/docx）
- 有 content 且非空：pass-through（可选 markdown 归一化）
- 有 file_path：读 bytes → get_document_parser → parse
- IndexingService.index_existing_document：在切块前调用 prepare（当 source_file_path 存在且 content 为空，或 reparse 标志）
- parse 成功后写 document.content、source_type，db commit，再 _index_document_content

【3. 数据库】
- 新 Alembic 迁移：documents 增加 source_file_path (nullable string), source_filename (nullable string)
  （保留原文件路径，便于 reparse；JSON 纯文本 upload 两字段为 null）
- create_document_record：JSON upload 时 source_file_path=null，source_type=text

【4. upload API】
- POST /api/v1/documents/upload 保留 JSON 行为不变（curl、现有前端 md 路径零破坏）
- 同 path 支持 multipart/form-data：file + kb_id + 可选 title
- multipart：校验大小与扩展名（.pdf/.docx/.md/.txt）；存文件到 DOCUMENT_STORAGE_PATH；Document content="" 或占位，source_file_path 填入；index_document_task.delay
- 配额仍走 QuotaService.check_upload_document

【5. 配置】
- DOCUMENT_STORAGE_PATH、DOCUMENT_MAX_SIZE_MB
- pyproject.toml optional-dependencies parsers 占位（mammoth、pymupdf 版本范围），02 再用

【6. 错误码】
- 新增 DOCUMENT_PARSE_FAILED 或明确用现有码 + msg；不支持格式 upload 时 PARAM_ERROR

【边界】
- 不改 MarkdownStructuredSplitter、RagService、retrieve
- 02 之前 multipart 的 .pdf/.docx 可注册 noop 或临时 NotImplemented，JSON .md 必须仍与改前一致
- Celery Worker 必须运行才能完成文件类索引（解析在 Worker 里做，避免 API 阻塞大 PDF）

【验收】
- JSON upload .md 内容与优化十二前一致（回归）
- multipart upload .txt 文件 → Worker parse → SUCCESS → retrieve 能召回
- 不支持扩展名 .zip → 参数错误
- 故意损坏 pdf（02 前可 skip）→ FAILED + error_message
```

## 做完怎么验

前置：PostgreSQL、Redis、Celery Worker 已启动；`alembic upgrade head` 已跑新迁移。

### 1. JSON 文本 upload 回归（必须与原行为一致）

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/documents/upload \
  -H "Authorization: Bearer rk_live_你的key" \
  -H "Content-Type: application/json" \
  -d '{
    "kb_id": "<kb_id>",
    "title": "回归测试",
    "content": "## 测试节\n\n用于验证 JSON upload 未破坏。"
  }'
```

预期：`code=0`，`status=PROCESSING` → 轮询后 SUCCESS；retrieve 能命中「测试节」。

### 2. multipart 上传 .txt 文件

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/documents/upload \
  -H "Authorization: Bearer rk_live_你的key" \
  -F "kb_id=<kb_id>" \
  -F "title=txt文件" \
  -F "file=@./某文件.txt"
```

预期：SUCCESS；`GET /documents/{id}` 可见 `source_filename`；chunk 正常。

### 3. 不支持扩展名

```bash
curl -s -X POST ... -F "file=@./test.zip" -F "kb_id=<kb_id>"
```

预期：非 0，`msg` 提示不支持格式或参数错误。

### 4. Worker 未启动

upload 后文档一直 PROCESSING——与优化八行为一致，README 已有说明。

---

01 完成后进入 **02 - Word 与 PDF 解析及验收**：注册 DocxParser、PdfParser，调试台 FormData，验收脚本闭环。
