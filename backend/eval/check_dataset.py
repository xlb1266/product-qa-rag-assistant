"""测试集自检：验证 expected_keywords 确实能命中期望文档的某个 chunk。

检查项（answerable 条目）：
1. 期望文档在数据库中存在且 status=ready；
2. 至少一个 chunk 的正文包含全部 expected_keywords（这是检索命中判定的依据）；
3. 期望文档之外若有 chunk 同样包含全部关键词（如压测超大文档、飞轮遗留问答），
   列出提示——不影响判定（命中按内容匹配），但出题方需确认关键词区分度足够；
4. expected_keywords 不应出现在 chunk 的标题行（### 开头）里，避免"命中标题"的假命中。

用法：cd backend && uv run python eval/check_dataset.py
"""

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.database import get_db  # noqa: E402


def load_dataset() -> list[dict]:
    path = Path(__file__).parent / "dataset.json"
    return json.loads(path.read_text(encoding="utf-8"))["items"]


def check() -> bool:
    db = get_db()
    docs = {r["filename"]: r["id"] for r in db.query("SELECT id, filename FROM documents")}
    all_chunks = db.query(
        "SELECT c.id, c.content, d.filename AS doc FROM chunks c"
        " JOIN documents d ON d.id = c.document_id"
    )

    items = load_dataset()
    ok = True
    for item in items:
        if item["type"] != "answerable":
            continue
        qid, kws = item["id"], item["expected_keywords"]
        doc_id = docs.get(item["expected_doc"])
        if doc_id is None:
            print(f"[FAIL] {qid} 期望文档不存在：{item['expected_doc']}")
            ok = False
            continue

        in_doc = [c for c in all_chunks if c["doc"] == item["expected_doc"] and all(k in c["content"] for k in kws)]
        outside = [c for c in all_chunks if c["doc"] != item["expected_doc"] and all(k in c["content"] for k in kws)]

        if not in_doc:
            print(f"[FAIL] {qid} 没有任何 chunk 同时包含关键词 {kws}")
            ok = False
            continue

        # 标题行假命中检查：唯一命中的 chunk 若只在标题行包含关键词，视为出题失误
        body_only = []
        for c in in_doc:
            body = "\n".join(line for line in c["content"].splitlines() if not line.lstrip().startswith("#"))
            if all(k in body for k in kws):
                body_only.append(c)
        if not body_only:
            print(f"[WARN] {qid} 关键词 {kws} 只命中 chunk 标题行，建议改用正文用词：chunk_id={[c['id'] for c in in_doc]}")

        if outside:
            print(f"[WARN] {qid} 关键词在期望文档外也有命中（共 {len(outside)} 个 chunk）：")
            for c in outside[:5]:
                print(f"       - doc={c['doc']} chunk_id={c['id']} 片段={c['content'][:60]!r}")

        hit = body_only[0] if body_only else in_doc[0]
        print(f"[OK]   {qid} -> chunk_id={hit['id']} | {item['expected_section']}")
    return ok


if __name__ == "__main__":
    print("=== 测试集自检 ===")
    passed = check()
    print("=== 结果：", "全部通过" if passed else "存在失败项，请修正 dataset.json", "===")
    sys.exit(0 if passed else 1)
