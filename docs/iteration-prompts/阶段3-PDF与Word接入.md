# 阶段 3 / 4：PDF 与 Word 文档接入（四阶段迭代的第三个会话）

> 本文件是一套自包含的会话提示词。开新会话后，把本文件全文粘贴给 AI 即可开始，AI 无需其他上下文。
> 与阶段 1/2 无硬依赖（不需要评测产出），但建议按顺序做，保证回答层已稳。

---

## 任务目标

把知识库数据入口从"只支持 Markdown"扩展到 **PDF + Word（.docx）**：解析 → 转成结构化 Markdown → 走现有入库管线。解决企业场景最常见的两种文档格式接入。

**已定范围（不要扩大）**：

- 做：`.pdf` + `.docx` 上传解析入库
- 不做：PPT / Excel / 图片 / 扫描件 OCR。在 `README.md` 的功能说明里加一句"后续规划"提及即可，不写代码

## 项目背景（必读）

- 项目：`D:\zhuomian\RAG-project\kongqixiaozhu`，基于知识库的 AI 客服问答系统（对外名"产品咨询智能客服"）
- 技术栈：Python 3.12 + FastAPI + FAISS + SQLite（后端，依赖管理用 uv）；React 18 + TypeScript + Vite（前端）
- 模型：DeepSeek（生成回答）、阿里百炼 text-embedding-v4（向量）、qwen3-rerank（重排）
- 启动：后端 `cd backend && uv run uvicorn app.main:app --reload --port 8000`；前端 `cd frontend && npm run dev`（http://localhost:5173）
- 密钥在 `backend/.env`（已配好）；测试账号 admin@app.com / 123456、user@app.com / 123456
- 注意：数据飞轮功能已于 2026-09-22 整体移除，代码里没有 feedback / review 相关内容，不要引用

## 当前数据入口现状（2026-09-22 核实）

`backend/app/api/documents.py`：

- `upload_document`（documents.py:37-54）：只放行 `.md` / `.markdown`（第 43 行硬编码），且强制 UTF-8 解码（48-50 行），其他格式直接 400
- `process_document_text`（documents.py:57 起）：**入库管线本体**——`split_markdown` 分块 → 百炼向量化 → chunks 表 + FAISS 入库 → BM25 索引失效。签名 `(filename, text, size)`，纯文本进出，**这是多格式接入的天然卡点：只要在调用它之前把二进制文件转成 Markdown 文本，管线其余部分零改动**
- 文档标题取首个一级标题，没有则用文件名（66-69 行）

分块逻辑 `backend/app/core/chunker.py`（阶段 4 才优化，本阶段别动它）：按 Markdown 标题层级切分，所以 **PDF/Word 解析产物质量直接决定检索质量，转 Markdown 时要尽力还原标题层级**。

前端：`frontend/src/pages/KnowledgeBasePage.tsx`（上传入口）、`frontend/src/api.ts`（documentsApi）。

## 实现要求

### 1. 解析模块（新建 `backend/app/core/parsers.py`）

- 依赖用 `uv add pypdf python-docx`（纯 Python、轻量；不要引入 unstructured 这种大件）
- 接口约定：
  ```python
  def parse_upload(filename: str, raw: bytes) -> str
  # 按扩展名分发；返回近似 Markdown 的文本；解析失败抛 ParseError（自定义异常，带用户可读的中文原因）
  ```
- **docx → Markdown 规则**：
  - 段落样式 Heading 1~4 → `#`~`####`（这是分块质量的命脉，必须做）
  - 表格 → Markdown 表格（`|` 分隔 + 表头分隔行）
  - 列表段落 → `-` 列表项
  - 其余段落原样输出，段落间空行
- **pdf → Markdown 规则**：
  - pypdf 逐页提取文本，页与页之间用 `\n\n` 分隔
  - PDF 本身没有可靠标题结构，每页文本前不加伪标题（加伪标题会污染 heading_path）；提取结果作为无标题正文走 chunker 的空标题分支即可
  - **扫描件检测**：提取出的文本去空白后长度 < 阈值（如 50 字符）→ 抛 ParseError("该 PDF 疑似扫描件，无法提取文本内容")，明确告诉用户而不是入库一堆空块
- 错误处理：加密 PDF、损坏文件、docx 解析异常都要转成 400 + 中文原因，不能 500

### 2. 上传接口改造（`documents.py`）

- 白名单扩为 `.md / .markdown / .pdf / .docx`
- 分流：Markdown 走原路径（UTF-8 校验保留）；PDF/Word 先 `parse_upload` 转文本，再走 `process_document_text`
- PDF/Word 的 `size` 存原始文件字节数（现在就这么传的，保持）
- **不要**给 documents 表加新列、不要改表结构（filename 存原始文件名已够溯源）

### 3. 前端改造

- `KnowledgeBasePage.tsx` 上传控件的 accept 与提示文案加 `.pdf` / `.docx`
- `api.ts` 如有文件类型校验同步放开
- 文档列表、分块详情页无需改（展示的是解析后内容，天然兼容）

### 4. 测试数据与全链路验证

- 用 Python 脚本生成测试样本放 `test-data/`：一个带标题/表格/列表的 docx、一个文本型 PDF（生成脚本可临时写，样本留下）
- 全链路手工验证：上传 → 列表 ready → 分块详情（确认表格/列表/标题路径合理）→ 在线咨询问一个只有该文档才答得了的问题 → 命中
- 边界验证：损坏文件、加密 PDF、扫描件 PDF（可拿一张图片改后缀近似模拟文本提取为空的场景）都应 400 且中文报错

## 坑清单

1. **curl 上传中文文件名乱码已修**（`_fix_filename`，documents.py:23-34），扩展白名单时别把这个逻辑改坏
2. **入库后必须 `invalidate_bm25_index()`**——`process_document_text` 里已有，但如果你新写了任何绕过它的入库路径就会忘
3. **uvicorn --reload 只监控 .py 变化**，改完前端要硬刷新浏览器；改数据库 schema 才需要重启（本阶段不改 schema）
4. **Word 的"标题"依赖文档样式**：测试样本必须用 Heading 样式生成（python-docx 的 `add_heading`），用普通加粗段落模拟的话解析不出层级，会误判实现失败
5. pypdf 提取中文 PDF 一般没问题，但表格会散成文本行——这是已知局限，不必在本阶段解决（chunking 阶段 4 统一考虑特殊内容）
6. 新依赖记得 `uv add` 而不是手改 pyproject.toml 后不锁版本

## 验收标准

1. 上传 .docx（含标题/表格/列表）：文档 ready，分块详情里表格完整、heading_path 层级正确
2. 上传文本型 PDF：入库成功、可被检索命中
3. 上传损坏/加密/扫描件：返回 400 + 明确中文原因，不产生半成品文档记录（或记录为 failed 可删除）
4. 原有 .md 上传路径行为完全不变（拿 test-data/空气小猪产品知识库.md 回归一遍）
5. 前端上传入口接受新格式、列表与分块详情正常展示

## 与后续阶段的衔接

- **阶段 4（chunking）** 会处理表格/列表等特殊内容的切分——本阶段 PDF/Word 转出的 Markdown 里已经包含表格和列表，正好是阶段 4 的真实输入，别在这里提前做切分优化

## 收尾动作（固定流程）

全部完成后：先自查一遍验收标准，然后**主动询问用户是否要手动验收**（给出建议的验收步骤清单，至少含：上传一个 Word 和一个 PDF、看分块详情、问一个针对新文档的问题），用户确认通过才算本阶段结束。
