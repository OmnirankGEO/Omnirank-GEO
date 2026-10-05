"""[工单 2026-08-06 §2] HTTP header 值必须是 latin1 · 服务商降级向导 P0 回归锁。

生产事故:admin 打开「服务商降级向导」立刻抛
    Failed to execute 'setRequestHeader' on 'XMLHttpRequest':
    String contains non ISO-8859-1 code point.
根因 = `ProviderDowngradeWizard.tsx` 两处把中文塞进 `X-Governance-Reason` header。
这两处在 `useEffect(open) → loadReadiness()` 里,**打开即抛** → snapshot 永远为 null
→ 「建立耐久方案」永远灰着 → 向导完全不可用。

锁的三条职责(缺一条就能绕过):
  1. 面锁:全前端任何 `headers: { ... }` 字面量里不许出现 U+00FF 以上的字符
     —— 不是只盯这一个文件,否则下一个人在别处再塞一次。
  2. 反删锁:不许「把 header 删掉了事」—— 审计日志要能区分是哪个动作,
     两个 ASCII slug 必须在。
  3. 资金/语义边界锁:用户填写的「治理原因」走 **request body**
     (`reason: reason.trim()`),createPlan / confirm 两个写操作**不得**携带
     `X-Governance-Reason` header —— 把它「统一」成 header 才是真的改语义。
"""
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC_DIR = _ROOT / "frontend" / "src"
_WIZARD = _SRC_DIR / "pages" / "Admin" / "ProviderDowngradeWizard.tsx"


def _match_brace_block(text: str, open_idx: int) -> str:
    """从 `{` 开始做括号配对,返回含首尾大括号的块。"""
    depth = 0
    for j in range(open_idx, len(text)):
        ch = text[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_idx : j + 1]
    return text[open_idx:]


def _header_blocks():
    """产出 (相对路径, 行号, headers 块正文)。"""
    import re

    pattern = re.compile(r"headers\s*:\s*\{")
    for path in sorted(_SRC_DIR.rglob("*")):
        if path.suffix not in (".ts", ".tsx") or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for m in pattern.finditer(text):
            open_idx = m.end() - 1
            line = text[: m.start()].count("\n") + 1
            yield (path.relative_to(_ROOT).as_posix(), line, _match_brace_block(text, open_idx))


def test_no_non_latin1_in_any_request_header_literal():
    """面锁:全前端 header 字面量不得含非 latin1 字符。

    🔴 判别力自证:本测试在修复前的 tree 上必须红(能抓到 ProviderDowngradeWizard
    的 2 处中文);只跑修复后 = 恒真,整条作废。
    """
    offenders = []
    for rel, line, block in _header_blocks():
        bad = sorted({ch for ch in block if ord(ch) > 0xFF})
        if bad:
            offenders.append(f"{rel}:{line} 含非 latin1 字符 {bad!r}")
    assert not offenders, (
        "HTTP header 值只接受 ISO-8859-1;下列位置会让 XHR setRequestHeader 直接抛异常:\n"
        + "\n".join(offenders)
    )


def test_governance_reason_header_kept_as_ascii_slug():
    """反删锁:不许删 header 了事 —— 审计要能区分是哪个只读动作。"""
    src = _WIZARD.read_text(encoding="utf-8")
    for slug in ("admin-provider-downgrade-plan-read", "admin-provider-downgrade-readiness"):
        assert slug in src, f"{slug} 丢失:X-Governance-Reason 被删掉而不是改成 ASCII slug"


def test_user_typed_reason_still_travels_in_request_body():
    """语义边界锁:用户填的治理原因走 body,不得被「统一」进 header。"""
    src = _WIZARD.read_text(encoding="utf-8")
    assert src.count("reason: reason.trim()") == 2, (
        "createPlan / confirm 必须各自在 request body 里带用户填写的 reason"
    )
    # createPlan(POST .../downgrade-plans)与 confirm(POST .../confirm)两段里
    # 不得出现 X-Governance-Reason —— 只读请求才用它。
    create_idx = src.index("const createPlan")
    tail = src[create_idx:]
    assert "X-Governance-Reason" not in tail, (
        "写操作(createPlan/confirm)不得携带 X-Governance-Reason header:"
        "那会把用户填写的治理原因从 body 挪到 header,是真的改语义"
    )
