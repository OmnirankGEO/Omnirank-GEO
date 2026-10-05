"""品牌名入库校验与归一（P0-1 · 2026-07-26）。

生产实证：``brands.id=278`` 的 name 是

    "深圳驰鲸科技\\n\\n城市:深圳"

（含换行 + "城市:" 标签串）。品牌识别永远匹配不到这个字符串 →
诊断 456 / 468 双双 **0 分**，客户付费两次拿到废报告；同一家公司换成干净名字
（``brands.id=712`` "深圳市驰鲸科技有限公司"）后立刻拿到 20 分。

因此品牌名不是普通文本字段，它是**品牌识别的匹配键**。脏进去 = 整份报告作废。

--------------------------------------------------------------------------
边界归属（SSOT §11 四级裁决）
--------------------------------------------------------------------------
拒绝畸形品牌名属于 **H0「对象身份与数据完整性」**：品牌名是识别与计费对象的
身份键，带控制字符/标签串的名字会让识别恒不命中并产出不可用交付物。
但**拒绝方式必须给出口**（§13）：``BrandNameHygieneError`` 一律附带
``suggestion``（清洗后的建议名）+ ``repair_hint``，前端可一键采用，
不是丢一个错误码让人自己猜。

AI 自动填充与手工输入走**同一个** ``normalize_brand_name`` / ``validate_brand_name``，
不允许"AI 填的就不校验"。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

MAX_BRAND_NAME_LENGTH: Final[int] = 80
MIN_BRAND_NAME_LENGTH: Final[int] = 2

# 明确禁止的字段标签串：这些是"整段档案被误粘进名字字段"的指纹。
# 只要名字里出现「标签 + 分隔符」就判畸形（"城市:深圳" / "行业：餐饮" / "地区 : 广州"）。
_FIELD_LABEL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(城市|地区|地域|区域|行业|品类|业务|主营|电话|手机|邮箱|地址|官网|网址|联系人|微信|备注)"
    r"\s*[:：]"
)

# 控制字符（含换行、制表、回车、垂直制表、NUL 等）
_CONTROL_CHARS: Final[re.Pattern[str]] = re.compile(r"[\x00-\x1f\x7f  ]")

# 字符白名单：中日韩汉字 / 假名 / 拉丁字母 / 数字 / 空格 / 常见品牌标点
_ALLOWED_CHARS: Final[re.Pattern[str]] = re.compile(
    r"^[0-9A-Za-z一-鿿぀-ヿ０-９Ａ-Ｚａ-ｚ"
    r"\s\-_·．\.&＆'’/／()（）\[\]【】+＋]+$"
)

_WHITESPACE_RUN: Final[re.Pattern[str]] = re.compile(r"\s+")


class BrandNameHygieneError(ValueError):
    """畸形品牌名。附建议名与修复提示，调用方必须把出口带给用户（§13）。"""

    def __init__(self, code: str, message: str, *, suggestion: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.suggestion = suggestion or None

    @property
    def repair_hint(self) -> str:
        if self.suggestion:
            return f"建议改为「{self.suggestion}」，或手动填写规范的公司/品牌名。"
        return "请填写规范的公司/品牌名（不要粘贴整段资料，不要带换行或「城市:」这类标签）。"

    def to_detail(self) -> dict:
        """给 HTTPException(detail=...) 用的机器合同（§13 告警必须有出口）。"""
        return {
            "code": self.code,
            "message": self.message,
            "reason": "品牌名是 AI 品牌识别的匹配键，畸形名会让识别恒不命中、报告作废。",
            "impact": "本次录入被拒绝；不会产生诊断或扣费。",
            "repair_hint": self.repair_hint,
            "suggestion": self.suggestion,
            "actions": ["采用建议名", "手动修改品牌名"],
            "rule_version": "brand-name-hygiene-v1",
        }


def normalize_brand_name(value: object) -> str:
    """把品牌名归一到可入库形态（不判定合法性，只做无损清洗）。

    - NFKC 归一（全角数字/字母 → 半角，兼容重复编码）
    - 控制字符（换行/制表/回车）→ 空格
    - 连续空白折叠为一个空格
    - 去首尾空白与首尾标点噪音
    """
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKC", text)
    text = _CONTROL_CHARS.sub(" ", text)
    text = _WHITESPACE_RUN.sub(" ", text).strip()
    return text.strip(" ·-_,，、;；:：/／")


def suggest_brand_name(value: object) -> str:
    """从脏输入里抽出最可能的品牌名（用于给用户的建议，不自动入库）。

    brand 278 的实证形态是「真名 + 换行 + 标签串」，所以：
      1) 先按控制字符切段，取**第一段**非空文本（真名通常在最前）；
      2) 再切掉该段里从第一个字段标签开始的尾巴（"深圳驰鲸科技 城市:深圳"）；
      3) 归一 + 截断到长度上限。
    抽不出合法名就返回空串 —— 不编造。
    """
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKC", text)
    segments = [seg.strip() for seg in _CONTROL_CHARS.split(text) if seg.strip()]
    candidate = segments[0] if segments else ""
    label = _FIELD_LABEL_PATTERN.search(candidate)
    if label:
        candidate = candidate[: label.start()]
    candidate = normalize_brand_name(candidate)
    if len(candidate) > MAX_BRAND_NAME_LENGTH:
        candidate = candidate[:MAX_BRAND_NAME_LENGTH].strip()
    if len(candidate) < MIN_BRAND_NAME_LENGTH:
        return ""
    # 建议名本身必须过校验，否则不给建议（不把一个仍然畸形的名字推给用户）
    try:
        validate_brand_name(candidate, suggest=False)
    except BrandNameHygieneError:
        return ""
    return candidate


def validate_brand_name(value: object, *, suggest: bool = True) -> str:
    """校验并返回可入库的品牌名；畸形则抛 ``BrandNameHygieneError``。

    这是**手工输入与 AI 自动填充共用**的唯一入口。
    """
    raw = "" if value is None else str(value)

    def _suggestion() -> str | None:
        return suggest_brand_name(raw) if suggest else None

    if _CONTROL_CHARS.search(raw):
        raise BrandNameHygieneError(
            "BRAND_NAME_HAS_CONTROL_CHARS",
            "品牌名不能包含换行或制表符（看起来像把整段资料粘进了名字栏）。",
            suggestion=_suggestion(),
        )

    name = normalize_brand_name(raw)
    if not name:
        raise BrandNameHygieneError(
            "BRAND_NAME_EMPTY", "品牌名不能为空。", suggestion=None
        )
    if len(name) < MIN_BRAND_NAME_LENGTH:
        raise BrandNameHygieneError(
            "BRAND_NAME_TOO_SHORT",
            f"品牌名至少 {MIN_BRAND_NAME_LENGTH} 个字。",
            suggestion=None,
        )
    if len(name) > MAX_BRAND_NAME_LENGTH:
        raise BrandNameHygieneError(
            "BRAND_NAME_TOO_LONG",
            f"品牌名最多 {MAX_BRAND_NAME_LENGTH} 个字（当前 {len(name)} 个）。",
            suggestion=_suggestion(),
        )
    if _FIELD_LABEL_PATTERN.search(name):
        raise BrandNameHygieneError(
            "BRAND_NAME_HAS_FIELD_LABEL",
            "品牌名里不能带「城市:」「行业:」这类字段标签（这些信息请填到对应字段）。",
            suggestion=_suggestion(),
        )
    if not _ALLOWED_CHARS.match(name):
        raise BrandNameHygieneError(
            "BRAND_NAME_HAS_INVALID_CHARS",
            "品牌名含不支持的字符，请只使用中英文、数字和常见标点。",
            suggestion=_suggestion(),
        )
    return name


def is_malformed_brand_name(value: object) -> bool:
    """只判断是否畸形（给存量扫描/清洗脚本用）。"""
    try:
        validate_brand_name(value, suggest=False)
    except BrandNameHygieneError:
        return True
    return False


# ---------------------------------------------------------------------------
# 重复品牌检测用的归一键（P1-11）
# ---------------------------------------------------------------------------

# 中国公司名里的行政区/组织形式噪音：归一时剥掉，让
# 「深圳驰鲸科技」与「深圳市驰鲸科技有限公司」能对上。
_COMPANY_NOISE: Final[tuple[str, ...]] = (
    "有限责任公司", "股份有限公司", "有限公司", "集团有限公司", "集团", "公司",
    "分公司", "事务所", "工作室", "中心", "厂", "店",
    "科技有限", "网络科技", "信息技术",
)
_ADMIN_DIVISION_SUFFIX: Final[re.Pattern[str]] = re.compile(r"(省|市|区|县|自治区|自治州)")


def brand_dedupe_key(value: object) -> str:
    """品牌名归一键：用于「疑似重复品牌」检测（P1-11）。

    只用于**提示**，不用于自动合并归属。归一策略故意偏激进（宁可多提示），
    因为误提示的代价是用户点一下"仍然新建"，漏提示的代价是同一家公司被独立
    建档、独立计费、数据不互通（brand 278 vs 712 实证）。
    """
    name = normalize_brand_name(value)
    if not name:
        return ""
    name = name.lower()
    # 去空白与常见标点
    name = re.sub(r"[\s\-_·．\.&＆'’/／()（）\[\]【】+＋]", "", name)
    # 去组织形式噪音（长词优先，避免"有限公司"被"公司"抢先切断）
    for noise in sorted(_COMPANY_NOISE, key=len, reverse=True):
        name = name.replace(noise.lower(), "")
    # 去行政区划后缀字（"深圳市" → "深圳"）
    name = _ADMIN_DIVISION_SUFFIX.sub("", name)
    return name
