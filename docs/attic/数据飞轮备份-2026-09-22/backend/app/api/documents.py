"""知识库文档管理接口：上传（分块 -> 向量化 -> 入库）、列表、详情、删除。

知识库属于管理类资源（影响所有用户的问答质量），全部接口仅管理员可用。
"""

import re

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.config import settings
from app.core.bm25 import invalidate_bm25_index
from app.core.chunker import split_markdown
from app.core.embeddings import EmbeddingError, get_embedding_client
from app.core.security import require_admin
from app.core.vector_store import get_vector_store
from app.db.database import get_db, now

router = APIRouter()

_FIRST_HEADING_RE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def _fix_filename(name: str) -> str:
    """修复 Windows 客户端（如 curl）用 GBK 编码上传导致的文件名乱码。

    乱码文件名形如 "¿ÕÆø..."（UTF-8 被按 latin-1 误解码），
    正常 UTF-8 文件名（中文不在 latin-1 范围）会编码失败而原样返回。"""
    try:
        fixed = name.encode("latin-1").decode("gbk")
        if fixed.isprintable():
            return fixed
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    return name


@router.post("/documents/upload")
async def upload_document(
    file: UploadFile = File(...),
    user: dict = Depends(require_admin()),
) -> dict:
    filename = _fix_filename(file.filename or "untitled.md")
    if not filename.lower().endswith((".md", ".markdown")):
        raise HTTPException(400, "仅支持上传 Markdown（.md / .markdown）文件")

    raw = await file.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise HTTPException(400, "文件编码不是 UTF-8，请转换后重新上传")
    if not text.strip():
        raise HTTPException(400, "文件内容为空")

    return await process_document_text(filename, text, size=len(raw))


async def process_document_text(filename: str, text: str, size: int | None = None) -> dict:
    """知识库入库管线（上传接口与数据飞轮反哺共用）：

    分块 -> 向量化 -> chunks/FAISS 入库 -> BM25 索引失效。
    校验失败抛 HTTPException；成功返回文档详情。
    """
    chunks = split_markdown(text)
    if not chunks:
        raise HTTPException(400, "未能从文件中解析出有效内容块")

    # 文档标题取首个一级标题，没有则用文件名
    m = _FIRST_HEADING_RE.search(text)
    title = m.group(1).strip() if m else filename.rsplit(".", 1)[0]
    if size is None:
        size = len(text.encode("utf-8"))

    db = get_db()
    doc_id = db.execute(
        "INSERT INTO documents (filename, title, size, status, created_at)"
        " VALUES (?, ?, ?, 'processing', ?)",
        (filename, title, size, now()),
    ).lastrowid

    # 1) 向量化：失败则文档标记 failed，保留记录便于排查，可删除后重传
    try:
        vectors = await get_embedding_client().embed(
            [c["content"] for c in chunks]
        )
    except EmbeddingError as e:
        db.execute(
            "UPDATE documents SET status='failed', error=? WHERE id=?",
            (str(e), doc_id),
        )
        raise HTTPException(502, f"向量化失败：{e}")

    if len(vectors) != len(chunks):
        db.execute(
            "UPDATE documents SET status='failed', error=? WHERE id=?",
            ("向量化返回数量与分块数不一致", doc_id),
        )
        raise HTTPException(502, "向量化返回数量与分块数不一致")
    if len(vectors[0]) != settings.embedding_dim:
        msg = (
            f"向量维度 {len(vectors[0])} 与索引维度 {settings.embedding_dim} 不一致，"
            "请检查 embedding 模型配置"
        )
        db.execute(
            "UPDATE documents SET status='failed', error=? WHERE id=?",
            (msg, doc_id),
        )
        raise HTTPException(502, msg)

    # 2) 入库：chunks 自增 id 即 FAISS 向量 ID
    store = get_vector_store()
    faiss_ids: list[int] = []
    for chunk, _ in zip(chunks, vectors):
        cur = db.execute(
            "INSERT INTO chunks (document_id, seq, heading_path, content, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (doc_id, chunk["seq"], chunk["heading_path"], chunk["content"], now()),
        )
        faiss_ids.append(cur.lastrowid)  # type: ignore[arg-type]
    store.add(faiss_ids, vectors)

    db.execute(
        "UPDATE documents SET status='ready', chunk_count=? WHERE id=?",
        (len(chunks), doc_id),
    )
    invalidate_bm25_index()  # 新分块已入库，BM25 索引下次检索时重建
    return _get_document_or_404(doc_id)


@router.get("/documents")
def list_documents(user: dict = Depends(require_admin())) -> list[dict]:
    rows = get_db().query("SELECT * FROM documents ORDER BY id DESC")
    return [dict(r) for r in rows]


@router.get("/documents/{doc_id}")
def get_document(doc_id: int, user: dict = Depends(require_admin())) -> dict:
    doc = _get_document_or_404(doc_id)
    chunks = get_db().query(
        "SELECT id, document_id, seq, heading_path, content"
        " FROM chunks WHERE document_id=? ORDER BY seq",
        (doc_id,),
    )
    return {"document": doc, "chunks": [dict(c) for c in chunks]}


@router.delete("/documents/{doc_id}")
def delete_document(doc_id: int, user: dict = Depends(require_admin())) -> dict:
    db = get_db()
    _get_document_or_404(doc_id)

    # 先删 FAISS 向量，再删 SQLite 记录
    faiss_ids = [
        r["id"] for r in db.query("SELECT id FROM chunks WHERE document_id=?", (doc_id,))
    ]
    get_vector_store().remove(faiss_ids)
    db.execute("DELETE FROM chunks WHERE document_id=?", (doc_id,))
    db.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    invalidate_bm25_index()  # 分块已删除，BM25 索引下次检索时重建
    return {"ok": True, "removed_vectors": len(faiss_ids)}


def _get_document_or_404(doc_id: int) -> dict:
    rows = get_db().query("SELECT * FROM documents WHERE id=?", (doc_id,))
    if not rows:
        raise HTTPException(404, f"文档不存在（id={doc_id}）")
    return dict(rows[0])
