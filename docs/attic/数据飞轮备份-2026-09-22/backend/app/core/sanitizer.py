"""文本脱敏：数据飞轮入库前去掉个人敏感信息。

覆盖：手机号、身份证号、银行卡号、邮箱。替换为 [已脱敏:类型] 占位符，
同时返回发现的敏感类型列表，随审核记录展示（审核人可知原文含敏感信息）。
"""

import re

_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_IDCARD_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
# 银行卡：连续 13-19 位数字，或 4 位一组、以空格分隔的 16/19 位
_BANKCARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?<! )(?<!-)(?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def sanitize_text(text: str) -> tuple[str, list[str]]:
    """返回 (脱敏后文本, 发现的敏感类型列表)。无敏感信息时原样返回。"""
    found: list[str] = []

    def _sub(pattern: re.Pattern, label: str, text: str) -> str:
        nonlocal found
        if pattern.search(text):
            found.append(label)
            return pattern.sub(f"[已脱敏:{label}]", text)
        return text

    # 顺序：先长模式（身份证/银行卡），避免手机号被银行卡部分吞掉
    text = _sub(_IDCARD_RE, "身份证号", text)
    text = _sub(_BANKCARD_RE, "银行卡号", text)
    text = _sub(_PHONE_RE, "手机号", text)
    text = _sub(_EMAIL_RE, "邮箱", text)
    # 去重保持顺序
    seen: set[str] = set()
    ordered = [t for t in found if not (t in seen or seen.add(t))]
    return text, ordered
