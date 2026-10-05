# -*- coding: utf-8 -*-
"""WO_246 · 密钥/口令不许有写死的兜底值。

三处(WO_245 扫描闸实测全仓真命中):
  · `server.py:api_brands_merge`        品牌合并口令 —— 源码里写死一串 ⇒ **谁都能过的门**
  · `services/intake_events._daily_salt` 匿名化盐 —— 盐公开 ⇒ **哈希可离线反推**
  · `api/content_api._approval_token_secret` 审批链接签名密钥 ⇒ **谁都能伪造有效链接**

🔴 三处的共同点:**它们都"工作得很正常"**。
   有兜底 ⇒ 不报错 ⇒ 没人发现鉴权/匿名化已经失效。
   与本轮一直在治的那类 fallback 同一个病,只是后果从「算错钱」换成「门没关」。

🔴 拆掉兜底把风险从「后门」挪到「上线当天不可用」。这是对的取舍,
   但它必须配一张**必配键清单**(`config/required_env_keys.txt`),
   否则下一次就是"修好了安全,换来一次事故"。
"""
from __future__ import annotations

import ast
import io
import os
import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

MANIFEST = REPO / "config" / "required_env_keys.txt"


def _manifest_keys():
    keys = []
    for raw in io.open(MANIFEST, encoding="utf-8").read().splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            keys.append(line)
    return keys


# ══════════════════════════════════════════════════════════════════
# 一、品牌合并口令
# ══════════════════════════════════════════════════════════════════

def _merge_handler():
    tree = ast.parse(io.open(REPO / "server.py", encoding="utf-8").read(), "server.py")
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in n.decorator_list:
                if (isinstance(d, ast.Call) and d.args
                        and isinstance(d.args[0], ast.Constant)
                        and str(d.args[0].value).endswith("/api/brands/merge")):
                    return n
    return None


def test_the_merge_password_has_no_literal_fallback():
    """🔴 `environ.get("ADMIN_MERGE_PASSWORD", <字面量>)` 这种形状必须没有了。"""
    fn = _merge_handler()
    assert fn is not None, "找不到品牌合并端点 —— 判据前提变了"
    for n in ast.walk(fn):
        if (isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "get"
                and len(n.args) == 2):
            key = getattr(n.args[0], "value", None)
            if isinstance(key, str) and "PASSWORD" in key.upper():
                dflt = n.args[1]
                assert isinstance(dflt, ast.Constant) and dflt.value == "", (
                    "口令取值仍带非空兜底:%s" % ast.unparse(n))


def test_the_merge_password_fails_closed_and_uses_constant_time_compare():
    """🔴 未配置 ⇒ 503 指定 code;比对用 `hmac.compare_digest`(与 :11599 同形)。"""
    src = ast.unparse(_merge_handler())
    assert "ADMIN_MERGE_PASSWORD_NOT_CONFIGURED" in src, "没有 fail-closed 的 503 code"
    assert "503" in src, "不是 503"
    assert "hmac.compare_digest" in src, "比对不是定时安全的"
    assert "ADMIN_DELETE_PASSWORD" not in src.replace("#", ""), (
        "还在读 ADMIN_DELETE_PASSWORD —— 它没有独立功能,本单应删掉这次读取")


def test_the_merge_password_key_is_registered():
    """🔴 拆了兜底就必须登记 —— 否则"修好安全"会换来"上线当天不可用"。"""
    assert "ADMIN_MERGE_PASSWORD" in _manifest_keys(), (
        "config/required_env_keys.txt 里没有 ADMIN_MERGE_PASSWORD")


# ══════════════════════════════════════════════════════════════════
# 二、匿名化盐 / 审批签名密钥:or-链末尾不许是字面量
# ══════════════════════════════════════════════════════════════════

SECRET_FNS = (
    ("services/intake_events.py", "_daily_salt"),
    # [开源 E3 · B3b · 2026-09-28] api/content_api._approval_token_secret 随审批链接端点(社媒计划任务审批)整条删除,不再列
)


def _fn_in(rel, name):
    tree = ast.parse(io.open(REPO / rel, encoding="utf-8").read(), rel)
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


@pytest.mark.parametrize("rel,name", SECRET_FNS)
def test_no_literal_at_the_end_of_the_or_chain(rel, name):
    """🔴 `getenv(A) or getenv(B) or "字面量"` —— 最后那个字面量就是公开的密钥。"""
    fn = _fn_in(rel, name)
    assert fn is not None, "找不到 %s::%s" % (rel, name)
    for n in ast.walk(fn):
        if isinstance(n, ast.BoolOp) and isinstance(n.op, ast.Or):
            last = n.values[-1]
            if isinstance(last, ast.Constant) and isinstance(last.value, str):
                assert last.value == "", (
                    "%s::%s 的 or-链末尾仍是非空字面量:%r" % (rel, name, last.value))


@pytest.mark.parametrize("rel,name", SECRET_FNS)
def test_it_raises_instead_of_using_a_default(rel, name):
    """🔴 取不到就抛,不静默用默认值。"""
    src = ast.unparse(_fn_in(rel, name))
    assert "raise" in src, "%s::%s 取不到密钥时没有抛 —— 它会继续用某个值" % (rel, name)


def test_the_salt_raise_cannot_escape_into_the_business_path():
    """🔴 抛可以,但**不许外溢到业务路径**。

    `_safe_emit` 的契约白纸黑字是「永不抛,静默失败」。
    ⚠️ 工单点名 `_daily_salt` 一行,而**能抛到业务路径的调用点有五处** ——
       `_safe_emit` 一处 + 四个公开 helper 各自**在 try 之外**直接调它。
       只改点名那一行,剩下四条照样把异常外溢。
       (「工单点的实例不是缺陷的类」,这次类的边界是**该函数的全部调用点**。)
    """
    rel = "services/intake_events.py"
    tree = ast.parse(io.open(REPO / rel, encoding="utf-8").read(), rel)
    bad = []
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        guarded = set()
        for t in [n for n in ast.walk(fn) if isinstance(n, ast.Try)]:
            for n in ast.walk(ast.Module(body=t.body, type_ignores=[])):
                guarded.add(id(n))
        for n in ast.walk(fn):
            if (isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_daily_salt"
                    and id(n) not in guarded):
                bad.append("%s:%d" % (fn.name, n.lineno))
    assert not bad, "这些地方在 try 之外直接调 _daily_salt,异常会外溢:%s" % (bad,)


# ══════════════════════════════════════════════════════════════════
# 三、必配键清单本身
# ══════════════════════════════════════════════════════════════════

#: 🔴 [2026-09-20 订正] 这里**曾经**有一张 `NOT_ENV_BACKED` 豁免表,写着
#:    「ORG_INVITE_DELIVERY / SMS_TEMPLATE 不是读 env」。**两条都是假的**:
#:      · `auth/sms_service.py` 读 `SMS_TEMPLATE_INVITE`
#:      · `services/organization_onboarding.py` 读 `ORGANIZATION_INVITE_DELIVERY_PROVIDER`
#:    成因:旧判据**按后缀反推键名**(`X_NOT_CONFIGURED` ⇒ 键 `X`)。这两处
#:    **码名 ≠ 键名**,反推不中;我没去读代码,而是在豁免表里写一句理由把它解释掉 ——
#:    而 `reason` 是自由文本,没有任何东西校验它。于是"名字对不上"这个技术事实
#:    被洗成了"它不归这条规则管"这个制度结论,两处真 fail-closed 点因此免检
#:    (Deploy preflight 第 9 关数出 4 处、清单只有 2 个,才把它顶出来)。
#:
#: 🔴 修法不是"补两个键",是**不再反推**:键名从**代码现场**取 ——
#:    该 `*_NOT_CONFIGURED` 所在函数(外加一跳:它调用的本模块函数)里
#:    真正读了哪些环境变量。猜不中就没有豁免的诱惑,**洗白口本身被拆掉**。

def _env_keys_in(node) -> set:
    """这个语法树节点里**真正读了哪些环境变量**。

    只认 `os.getenv(...)` / `os.environ.get(...)` / `os.environ[...]` ——
    不认任意 `.get("x")`,否则 `payload.get("brand_id")` 也会被当成环境变量。
    """
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and n.args:
            a0 = n.args[0]
            if not (isinstance(a0, ast.Constant) and isinstance(a0.value, str)):
                continue
            f = n.func
            src = ast.unparse(f)
            if src.endswith("getenv") or src.endswith("environ.get"):
                out.add(a0.value)
        if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) \
                and isinstance(n.slice.value, str) \
                and "environ" in ast.unparse(n.value):
            out.add(n.slice.value)
    return out


def _fail_closed_sites():
    """全仓每一个 `*_NOT_CONFIGURED` 出现点,连同**代码现场**能取到的 env 键。

    Returns: ``{code: {"where": "rel::fn", "keys": {...}}}``
    """
    sites = {}
    for p in sorted(REPO.rglob("*.py")):
        rel = p.relative_to(REPO).as_posix()
        if rel.startswith(("tests/", "scripts/")) or "node_modules" in rel:
            continue
        try:
            src = p.read_text(encoding="utf-8")
        except Exception:
            continue
        if "_NOT_CONFIGURED" not in src:
            continue
        try:
            tree = ast.parse(src, rel)
        except Exception:
            continue
        fns = {f.name: f for f in ast.walk(tree)
               if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for fname, fn in fns.items():
            codes = {n.value for n in ast.walk(fn)
                     if isinstance(n, ast.Constant) and isinstance(n.value, str)
                     and n.value.endswith("_NOT_CONFIGURED")}
            if not codes:
                continue
            keys = _env_keys_in(fn)
            # 一跳:本模块里被它调用的函数(键常常藏在 `_delivery_provider()` 这种 helper 里)
            for c in {getattr(call.func, "id", None) for call in ast.walk(fn)
                      if isinstance(call, ast.Call)}:
                if c in fns:
                    keys |= _env_keys_in(fns[c])
            for code in codes:
                sites.setdefault(code, {"where": "%s::%s" % (rel, fname), "keys": set()})
                sites[code]["keys"] |= keys
    return sites


def test_every_fail_closed_code_has_a_registered_key_from_its_own_code_site():
    """🔴 每个 `*_NOT_CONFIGURED`,在**它自己那段代码**里读到的键,至少有一个在清单里。

    这一格不看码名 —— 码名和键名不一致是常态(`SMS_TEMPLATE_NOT_CONFIGURED`
    对应的键叫 `SMS_TEMPLATE_INVITE`)。按码名反推正是上一版把两处真 fail-closed 点
    漏掉的原因,而漏掉之后它还被一句自由文本的"理由"合法化了。
    """
    sites = _fail_closed_sites()
    assert sites, "一个 fail-closed 点都没扫到 —— 扫描器坏了,不是都登记了"
    keys = set(_manifest_keys())
    bad = []
    for code, info in sorted(sites.items()):
        if not info["keys"]:
            bad.append((code, info["where"], "这段代码里根本没读 env —— 判据前提变了,去看它"))
        elif not (info["keys"] & keys):
            bad.append((code, info["where"], "读到的键一个都没登记:%s" % sorted(info["keys"])))
    assert not bad, "这些 fail-closed 点没有登记在册的键:%s" % (bad,)


def test_the_scan_still_sees_the_four_known_sites():
    """🔴 仪器自检:上面那格"没扫到就红"只挡得住扫描器全死,挡不住它**少扫**。

    这里钉住当下的真实份数与出处。新增 fail-closed 点时这一格会红 ——
    那是**要的**:它逼人来看一眼新点的键有没有登记,而不是让份数悄悄变。
    """
    sites = _fail_closed_sites()
    assert set(sites) == {
        "ADMIN_MERGE_PASSWORD_NOT_CONFIGURED",
        "MONITORING_CLEAR_DATA_PASSWORD_NOT_CONFIGURED",
        "ORG_INVITE_DELIVERY_NOT_CONFIGURED",
        "SMS_TEMPLATE_NOT_CONFIGURED",
    }, "fail-closed 点的集合变了:%s" % sorted(sites)


def test_the_two_mismatched_names_are_resolved_from_code_not_from_the_code_name():
    """🔴 反臂:专盯"码名 ≠ 键名"这两处 —— 它们正是上一版被豁免掉的。

    钉的是**取键的来源**:键必须来自代码现场。若哪天有人把取键方式改回按码名反推,
    这两条会红(反推出的 `SMS_TEMPLATE` / `ORG_INVITE_DELIVERY` 并不存在于清单)。
    """
    sites = _fail_closed_sites()
    assert "SMS_TEMPLATE_INVITE" in sites["SMS_TEMPLATE_NOT_CONFIGURED"]["keys"]
    assert "ORGANIZATION_INVITE_DELIVERY_PROVIDER" in \
        sites["ORG_INVITE_DELIVERY_NOT_CONFIGURED"]["keys"]
    for code in ("SMS_TEMPLATE_NOT_CONFIGURED", "ORG_INVITE_DELIVERY_NOT_CONFIGURED"):
        derived = code[: -len("_NOT_CONFIGURED")]
        assert derived not in _manifest_keys(), (
            "%r 竟然在清单里 —— 那这一格就分不出「按码名反推」和「按代码取」了" % derived)


def test_the_manifest_never_contains_a_value():
    """🔴 清单里只许有键名 —— 一旦有人写成 `KEY=值`,那就是把密钥提交进仓库。"""
    for key in _manifest_keys():
        assert "=" not in key, "清单里出现了带值的行:%r" % key
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", key), "不像键名:%r" % key
