# 阶段 3：PDF 与 Word 文档接入

请在当前 RAG 客服项目中扩展知识库的数据入口。目前上传接口（`app/api/documents.py`）只放行 `.md` / `.markdown` 且强制 UTF-8 解码，企业场景最常见的 PDF 和 Word 文档进不来。本次把入口扩展为支持 **PDF + Word（.docx）**（已定范围，不扩大）：解析 → 转成结构化 Markdown → 复用现有入库管线 `process_document_text`（分块 → 向量化 → FAISS/BM25 入库），管线其余部分零改动。PPT / Excel / 图片 / 扫描件 OCR 明确不做，仅在 README 功能说明里留一句"后续规划"，不写代码。

解析层要求（新建 `backend/app/core/parsers.py`）：

- 依赖用 `uv add pypdf python-docx`，不引入 unstructured 这类重组件。
- 统一入口 `parse_upload(filename, raw: bytes) -> str`：按扩展名分发，返回近似 Markdown 的文本；解析失败抛自定义 ParseError，携带用户可读的中文原因。
- **Word**：段落样式 Heading 1~4 必须映射为 `#`~`####`（这是分块质量与 heading_path 的命脉）；表格转 Markdown 表格（`|` 分隔 + 表头分隔行）；列表段落转 `-` 列表项；段落间空行分隔。
- **PDF**：pypdf 逐页提取文本，页间用 `\n\n` 分隔；**不加伪标题**（会污染 heading_path），无标题正文走 chunker 空标题分支即可；提取文本去空白后不足 50 字符判定为扫描件，抛错"该 PDF 疑似扫描件，无法提取文本内容"，不得入库一堆空块。
- 加密 PDF、损坏文件、docx 解析异常全部转成 400 + 中文原因，不允许 500 裸奔。

上传接口与前端要求：

- 白名单扩为 `.md / .markdown / .pdf / .docx`；Markdown 走原路径（UTF-8 校验保留），PDF/Word 先 `parse_upload` 转文本再走 `process_document_text`；size 存原始文件字节数。
- 不改 documents 表结构（filename 存原始文件名已够溯源）。
- 前端上传控件 accept 与提示文案加 `.pdf` / `.docx`；文档列表与分块详情页无需改（展示的是解析后内容，天然兼容）。

验证要求：

- 用脚本生成测试样本放 `test-data/`：一个含标题/表格/列表的 docx（**必须用 Heading 样式生成**，普通加粗段落解析不出层级，会误判实现失败）、一个文本型 PDF。
- 全链路验证：上传 → 列表 ready → 分块详情（表格/列表/标题路径合理）→ 在线咨询问一个只有该文档才答得了的问题 → 检索命中。
- 边界验证：损坏文件、加密 PDF、扫描件（可拿一张图片改后缀近似模拟"文本提取为空"）都返回 400 且中文报错，不产生半成品文档记录（或记录为 failed 可删除）。

已知约束：

- `documents.py` 的 `_fix_filename`（curl 上传 GBK 文件名乱码修复）不能改坏。
- 任何新写的入库路径必须保证 BM25 索引失效（`invalidate_bm25_index`），否则检索不到新内容。
- pypdf 提取中文 PDF 文本正常，但表格会散成文本行——已知局限，留到阶段 4 统一处理特殊内容，本阶段不做。

Git 提交要求：解析层、上传接口改造、前端改造、测试样本各自独立提交，提交信息用中文、说清楚改了什么和为什么；全部完成后 `git status` 工作区必须干净。

全部完成后先自查上述要求是否逐条满足，再主动询问用户是否手动验收（附建议验收步骤：上传 Word 和 PDF 各一个、看分块详情、问一个针对新文档的问题、拿 `.md` 回归原上传路径），确认通过才算本阶段结束。
