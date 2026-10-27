"""chunker 分块规则单测：标题路径、超长切分、序号连续性。"""

from app.core.chunker import MAX_CHUNK_CHARS, split_markdown


def test_heading_path_records_full_nesting():
    chunks = split_markdown("# 一级\n## 二级\n正文内容\n")
    # 每个标题都会开新块：上一级标题无正文时，标题行自成一整块
    assert [c["heading_path"] for c in chunks] == ["一级", "一级 > 二级"]
    assert "正文内容" in chunks[1]["content"]


def test_sibling_heading_pops_deeper_levels():
    text = "# A\n## B\nb 正文\n## C\nc 正文\n# D\nd 正文\n"
    paths = [c["heading_path"] for c in split_markdown(text)]
    assert paths == ["A", "A > B", "A > C", "D"]


def test_text_before_first_heading_keeps_empty_path():
    chunks = split_markdown("开头正文\n# A\na 正文\n")
    assert chunks[0]["heading_path"] == ""
    assert "开头正文" in chunks[0]["content"]


def test_seq_starts_at_one_and_is_contiguous():
    chunks = split_markdown("# A\na\n# B\nb\n# C\nc\n")
    assert [c["seq"] for c in chunks] == [1, 2, 3]


def test_heading_only_section_still_kept_as_chunk():
    chunks = split_markdown("# A\n\n# B\nb 正文\n")
    assert [c["content"] for c in chunks] == ["# A", "# B\nb 正文"]


def test_long_section_split_by_paragraph_packing():
    para = "字" * 300
    text = f"# 标题\n{para}\n\n{para}\n\n{para}\n"
    chunks = split_markdown(text)
    assert len(chunks) > 1
    assert all(len(c["content"]) <= MAX_CHUNK_CHARS for c in chunks)


def test_single_overlong_paragraph_hard_split():
    para = "长" * (MAX_CHUNK_CHARS + 250)
    chunks = split_markdown(para)
    assert len(chunks) == 2
    assert len(chunks[0]["content"]) == MAX_CHUNK_CHARS
    assert len(chunks[1]["content"]) == 250


def test_max_chars_argument_is_respected():
    chunks = split_markdown("字" * 350, max_chars=100)
    assert len(chunks) == 4
    assert all(len(c["content"]) <= 100 for c in chunks)
