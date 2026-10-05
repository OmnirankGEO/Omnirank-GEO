# -*- coding: utf-8 -*-
"""服务期唯一 SSOT · 一致性锁(WO_SERVICE_PERIOD_SSOT_2026-08-06 §1.4 / §3)

锁分三层:
  A. 行为锁 —— services/service_period.py 的语义(不猜、fail-closed、带符号剩余)
  B. 一致性锁 —— **任何代码路径同时暴露两种服务期口径 → 转红**
       (`<日期> + service_days` / `COALESCE(service_days, 365)` / `service_months or 1`
        / 本模块之外的 relativedelta(months=…) 算服务期 / 前端自算"服务期至")
  C. 接线锁 —— 六个写入点走 SSOT、读点读 service_end_date、§1.5 到期不静默、迁移登记

🔴 每条"必须命中"都配了成对的"必须不命中"(反向对照)。
   单向断言证明不了判别力 —— 2026-07-31 preflight_selftest 的教训。
🔴 扫描前一律**剥掉注释与文档字符串**:本文件和交付说明都会逐字引用被禁的写法,
   不剥注释的话锁会抓到自己写的字(2026-08-05 连踩两次)。
"""

import io
import os
import re
import sys
from datetime import date

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from services.service_period import (  # noqa: E402
    MAX_SERVICE_MONTHS,
    ServicePeriodError,
    calendar_days_left,
    calendar_span_days,
    coerce_date,
    compliance_target_days,
    compute_service_end,
    is_within_calendar_period,
    normalize_service_months,
    portal_token_cap,
    read_calendar_period,
    resolve_activation_period,
    service_period_missing_hint,
)

SSOT_MODULE = os.path.join(REPO, "services", "service_period.py")


# ─────────────────────────── 工具:剥注释 ───────────────────────────

def _read(path):
    with io.open(path, encoding="utf-8") as fh:
        return fh.read()


def strip_py_comments(src: str) -> str:
    """去掉 `#` 注释与文档字符串,**保留普通字符串字面量**。

    🔴 这个分寸很关键,第一版写歪过:我把所有字符串都清空了 —— 于是 SQL 文本
       (`COALESCE(q.service_days, 365)` 就活在 SQL 字面量里)全被抹掉,
       禁写法扫描变成恒绿,同时接线锁又因为找不到 SQL 片段而恒红。
       现在只丢两样:`#` 注释、以及**独占一行开头**的三引号块(= 文档字符串)。
       交付说明/注释里逐字引用的反面写法照样被剥掉,而 SQL 里的真写法照样被抓到。
    """
    out = []
    i = 0
    n = len(src)
    line_start = 0
    only_ws_before = True   # 本行到目前为止是否只有空白
    quote = None            # 当前所处字符串的定界符
    drop_current = False    # 当前三引号块是否是文档字符串(要丢)
    while i < n:
        ch = src[i]
        if quote:
            if ch == "\\" and len(quote) == 1:
                out.append("" if drop_current else src[i:i + 2])
                i += 2
                continue
            if src.startswith(quote, i):
                out.append("" if drop_current else quote)
                i += len(quote)
                quote = None
                drop_current = False
                continue
            out.append("\n" if (drop_current and ch == "\n") else ("" if drop_current else ch))
            if ch == "\n":
                line_start = i + 1
                only_ws_before = True
            i += 1
            continue
        if src.startswith('"""', i) or src.startswith("'''", i):
            quote = src[i:i + 3]
            drop_current = only_ws_before   # 行首三引号 = 文档字符串
            out.append("" if drop_current else quote)
            i += 3
            only_ws_before = False
            continue
        if ch in ("'", '"'):
            quote = ch
            drop_current = False
            out.append(ch)
            i += 1
            only_ws_before = False
            continue
        if ch == "#":
            j = src.find("\n", i)
            i = n if j == -1 else j
            continue
        if ch == "\n":
            out.append("\n")
            line_start = i + 1
            only_ws_before = True
            i += 1
            continue
        if not ch.isspace():
            only_ws_before = False
        out.append(ch)
        i += 1
    return "".join(out)


def strip_ts_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(?m)^\s*//.*$", "", src)
    src = re.sub(r"(?m)//.*$", "", src)
    return src


def _walk(root, exts, skip_dirs=("node_modules", ".git", "dist", "build", "__pycache__")):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fn in filenames:
            if fn.endswith(exts):
                yield os.path.join(dirpath, fn)


# 服务期语义涉及的后端文件(全仓扫太慢也太吵,锁住真正相关的这些)
BACKEND_SCOPE = [
    "server.py",
    os.path.join("api", "selection_api.py"),
    os.path.join("api", "monitoring_api.py"),
    os.path.join("api", "m3_api.py"),
    os.path.join("api", "m3_material_confirm_api.py"),
    os.path.join("api", "m3_export_endpoints.py"),
    os.path.join("api", "scheduler.py"),
    os.path.join("db", "monitoring_db.py"),
    os.path.join("services", "service_period.py"),
]

FRONTEND_SCOPE = [
    os.path.join("frontend", "src", "pages", "Monitoring"),
    os.path.join("frontend", "src", "pages", "Portal"),
]


def backend_sources():
    for rel in BACKEND_SCOPE:
        p = os.path.join(REPO, rel)
        assert os.path.exists(p), f"锁的扫描范围写歪了,文件不存在: {rel}"
        yield rel, strip_py_comments(_read(p))


def frontend_sources():
    for rel in FRONTEND_SCOPE:
        root = os.path.join(REPO, rel)
        assert os.path.isdir(root), f"锁的扫描范围写歪了,目录不存在: {rel}"
        for p in _walk(root, (".ts", ".tsx")):
            yield os.path.relpath(p, REPO).replace("\\", "/"), strip_ts_comments(_read(p))


# ══════════════════════════ A. 行为锁 ══════════════════════════

class TestSsotBehaviour:
    def test_months_missing_raises_not_defaults_to_one(self):
        """§1.2 缺失 → 报错进人工,不许猜。这条就是 `or 1` 的替代品。"""
        for bad in (None, "", 0, -3, 25, "abc"):
            with pytest.raises(ServicePeriodError):
                normalize_service_months(bad)

    def test_months_valid_range(self):
        assert normalize_service_months(1) == 1
        assert normalize_service_months("6") == 6
        assert normalize_service_months(MAX_SERVICE_MONTHS) == MAX_SERVICE_MONTHS

    def test_error_carries_user_message_and_exit(self):
        """提示铁律:面向用户的提示必须自带解决方案快捷路径。"""
        with pytest.raises(ServicePeriodError) as ei:
            normalize_service_months(None)
        err = ei.value
        assert err.user_message and "服务期" in err.user_message
        assert err.hint.get("actions"), "报错必须带出口(actions),否则只是个死胡同"

    def test_missing_hint_shape(self):
        hint = service_period_missing_hint(286)
        assert hint["code"] == "SERVICE_PERIOD_NOT_SET"
        assert hint["actions"][0]["quote_id"] == 286

    def test_compute_service_end_is_calendar_months(self):
        assert compute_service_end(date(2026, 5, 8), 1) == date(2026, 6, 8)
        assert compute_service_end(date(2026, 4, 28), 6) == date(2026, 10, 28)
        # 月末回绕:1/31 + 1 月 = 2/28(relativedelta 语义)
        assert compute_service_end(date(2026, 1, 31), 1) == date(2026, 2, 28)

    def test_resolve_activation_period_pair(self):
        start, end = resolve_activation_period(start_date="2026-05-08", months=1)
        assert (start, end) == (date(2026, 5, 8), date(2026, 6, 8))

    def test_resolve_defaults_start_to_today_but_never_months(self):
        start, end = resolve_activation_period(start_date=None, months=3, today=date(2026, 8, 6))
        assert start == date(2026, 8, 6) and end == date(2026, 11, 6)
        with pytest.raises(ServicePeriodError):
            resolve_activation_period(start_date="2026-08-06", months=None)

    def test_read_calendar_period_never_derives_from_days_or_months(self):
        """给一行只有配额/月数、没有 end 的 quote —— 必须返 None,不许反推。

        这是本次事故的核心判据:11 张 paid 报价就是靠"反推"造出了第二口钟。
        """
        row = {
            "service_start_date": date(2026, 5, 8),
            "service_end_date": None,
            "service_days": 365,
            "service_months": 1,
        }
        assert read_calendar_period(row) == (date(2026, 5, 8), None)

    def test_read_calendar_period_reads_the_pair(self):
        row = {"service_start_date": "2026-05-08", "service_end_date": "2026-07-23"}
        assert read_calendar_period(row) == (date(2026, 5, 8), date(2026, 7, 23))

    def test_calendar_days_left_is_signed(self):
        """v1.7 老板复审 P1:不 clamp,UI 要能说"已过期 N 天"。"""
        assert calendar_days_left(date(2026, 8, 20), today=date(2026, 8, 6)) == 14
        assert calendar_days_left(date(2026, 7, 23), today=date(2026, 8, 6)) == -14
        assert calendar_days_left(None) is None

    def test_within_period_fail_closed_when_missing(self):
        assert is_within_calendar_period(date(2026, 8, 6), today=date(2026, 8, 6)) is True
        assert is_within_calendar_period(date(2026, 8, 5), today=date(2026, 8, 6)) is False
        assert is_within_calendar_period(None) is False, "没服务期不能当作永久有效"

    def test_compliance_target_days_has_no_365_fallback(self):
        assert compliance_target_days({"service_days": 30}) == 30
        assert compliance_target_days({"service_days": None}) is None
        assert compliance_target_days({"service_days": 0}) is None
        assert compliance_target_days({}) is None

    def test_span_and_portal_cap_are_derived_not_stored(self):
        assert calendar_span_days("2026-05-08", "2026-06-08") == 31
        assert calendar_span_days("2026-05-08", None) is None
        assert portal_token_cap("2026-07-23", 7) == date(2026, 7, 30)
        assert portal_token_cap(None, 7) is None

    def test_coerce_date_accepts_db_shapes_and_refuses_garbage(self):
        from datetime import datetime as dt
        assert coerce_date(dt(2026, 5, 8, 16, 44)) == date(2026, 5, 8)
        assert coerce_date("2026-05-08 16:44:20") == date(2026, 5, 8)
        assert coerce_date("2026-05-08T00:00:00") == date(2026, 5, 8)
        assert coerce_date("not-a-date") is None
        assert coerce_date(None) is None

    def test_286_regression_two_clocks_cannot_disagree(self):
        """§3 用 #286 真实数据构造的回归:归一后倒计时与轮换闸必须同日。

        生产实测(2026-08-06):#286 service_start=2026-05-08 / service_months=1 /
        service_days=30 / service_end_date=2026-07-23。
        旧口径:倒计时 = start + service_days = 06-07;轮换闸 = service_end_date = 07-23 → 差 46 天。
        新口径:两边都只读 service_end_date → 必须同日。
        """
        row_286 = {
            "service_start_date": date(2026, 5, 8),
            "service_end_date": date(2026, 7, 23),
            "service_days": 30,
            "service_months": 1,
        }
        _start, end = read_calendar_period(row_286)
        countdown_end = end                       # 监测页/门户倒计时读的
        rotation_gate_end = end                   # 轮换资格闸读的
        assert countdown_end == rotation_gate_end == date(2026, 7, 23)

        # 反向对照:旧口径确实会给出不同的一天(证明这条断言不是恒真)
        from datetime import timedelta
        legacy_end = row_286["service_start_date"] + timedelta(days=row_286["service_days"])
        assert legacy_end != rotation_gate_end
        assert (rotation_gate_end - legacy_end).days == 46


# ══════════════════════════ B. 一致性锁 ══════════════════════════

# 「日期 + service_days」= 用达标天数配额造日历钟
# 🔴 刻意**不认函数名**:第一版写成 `timedelta\(days=...service_days`,
#    变异 M05 只要 `from datetime import timedelta as _td` 换个别名就绕过去了
#    (「同名冒牌守卫骗过按函数名的扫描」的同一形状)。现在只认 `days=<任何含 service_days 的表达式>`。
#    加 `(?<![_\w])` 是因为不加会把 `service_days = %s` / `days = body.get("service_days")`
#    这种**赋值**也算进来(第一次放宽时当场误报 2 处)。要抓的是"把它当 days= 实参传出去"。
_DATE_PLUS_DAYS_PY = re.compile(
    r"((?<![_\w])days\s*=[^)\n]*service_days)"
    r"|(service_days\s*\|\|\s*'\s*days\s*')"
    r"|(\+\s*\(\s*COALESCE\s*\([^)]*service_days)",
    re.I,
)
_COALESCE_365 = re.compile(r"COALESCE\s*\([^)]*service_days[^)]*,\s*365\s*\)", re.I)
_OR_365 = re.compile(r"service_days[\"'\]\)\s]*\)?\s*or\s+365", re.I)
_MONTHS_OR_1 = re.compile(r"service_months[\"'\]\)\s]*\)?\s*or\s+1\b", re.I)
_RELATIVEDELTA_MONTHS = re.compile(r"relativedelta\s*\(\s*months\s*=")


class TestNoSecondClockBackend:
    def test_no_date_plus_service_days(self):
        hits = [(rel, m.group(0)) for rel, src in backend_sources()
                for m in _DATE_PLUS_DAYS_PY.finditer(src)]
        assert not hits, f"把履约达标天数配额加到日期上 = 第二口钟: {hits}"

    def test_no_coalesce_365(self):
        """§3 验收判据:`COALESCE 365` 在服务期语义里全站 0 命中。"""
        hits = [(rel, m.group(0)) for rel, src in backend_sources()
                for m in _COALESCE_365.finditer(src)]
        assert not hits, f"service_days 的 365 兜底应已随 migration_028 全部拔除: {hits}"

    def test_no_or_365(self):
        hits = [(rel, m.group(0)) for rel, src in backend_sources()
                for m in _OR_365.finditer(src)]
        assert not hits, f"Python 侧 365 兜底残留: {hits}"

    def test_no_service_months_or_1(self):
        """§3 验收判据:`or 1` 在服务期语义里全站 0 命中。"""
        hits = [(rel, m.group(0)) for rel, src in backend_sources()
                for m in _MONTHS_OR_1.finditer(src)]
        assert not hits, f"月数兜底 or 1 残留(缺失必须报错进人工): {hits}"

    def test_months_to_end_conversion_only_in_ssot_module(self):
        """全站只允许 services/service_period.py 一处 months → end 换算。"""
        offenders = [rel for rel, src in backend_sources()
                     if _RELATIVEDELTA_MONTHS.search(src)
                     and rel.replace("\\", "/") != "services/service_period.py"]
        assert not offenders, f"服务期换算只能有一处,别处出现 = 又长出一口钟: {offenders}"

    def test_reverse_control_patterns_actually_bite(self):
        """反向对照 —— 上面四条断言全是"必须不命中",必须证明它们能命中。

        没有这条,把正则写成永远匹配不到的样子也能全绿(恒真判据)。
        """
        assert _DATE_PLUS_DAYS_PY.search("end = start + timedelta(days=int(quote_service_days))")
        # 换个别名也必须抓到(变异 M05 就是这么绕过第一版的)
        assert _DATE_PLUS_DAYS_PY.search("end = start + _td_mut(days=int(quote_service_days))")
        assert _DATE_PLUS_DAYS_PY.search(
            "(q.service_start_date + (COALESCE(q.service_days,365) || ' days')::interval)")
        assert _COALESCE_365.search("), 0) < COALESCE(q.service_days, 365)")
        assert _OR_365.search('days = row.get("service_days") or 365')
        assert _MONTHS_OR_1.search('months = quote["service_months"] or 1')
        assert _RELATIVEDELTA_MONTHS.search("end = start + relativedelta(months=months)")
        # 好写法必须不命中(否则锁会把正确代码也判红)
        good = "end = compute_service_end(start, months)\nleft = calendar_days_left(row['service_end_date'])"
        assert not _DATE_PLUS_DAYS_PY.search(good)
        assert not _COALESCE_365.search(good)
        assert not _MONTHS_OR_1.search(good)
        assert not _RELATIVEDELTA_MONTHS.search(good)

    def test_comment_stripper_has_discriminating_power(self):
        """剥注释这一步本身也要证明有效 —— 两个方向都要证。

        方向①(必须剥掉):注释 / 文档字符串里逐字引用的反面写法,不该把锁弄红。
        方向②(必须留下):SQL 字面量里的真写法,必须还抓得到 ——
                        第一版把所有字符串都清空了,这条当场把它抓了出来。
        """
        doc_only = (
            'x = 1  # end = start + timedelta(days=service_days)\n'
            '"""\nCOALESCE(service_days, 365)\n"""\n'
            'y = COALESCE_MARKER\n'
        )
        stripped = strip_py_comments(doc_only)
        assert not _COALESCE_365.search(stripped)
        assert not _DATE_PLUS_DAYS_PY.search(stripped)
        assert "COALESCE_MARKER" in stripped, "剥注释不能把真代码也剥掉"

        live = 'cur.execute("""\n  AND x < COALESCE(q.service_days, 365)\n""")\n'
        assert _COALESCE_365.search(strip_py_comments(live)), (
            "SQL 字面量被剥掉了 → 禁写法扫描恒绿,等于没锁"
        )

    def test_ssot_module_itself_is_the_only_exception(self):
        """SSOT 模块允许出现 relativedelta,但**不允许**出现 365/or 1 兜底。"""
        src = strip_py_comments(_read(SSOT_MODULE))
        assert _RELATIVEDELTA_MONTHS.search(src), "换算入口反而不见了?"
        assert not _COALESCE_365.search(src)
        assert not _OR_365.search(src)
        assert not _MONTHS_OR_1.search(src)


# 🔴 同样不认写法细节:第一版要求 `getDate() + <标识符>service_days` 紧挨着,
#    变异 M24 写成 `getDate() + (data?.service_days ?? 0)` 就绕过去了。
#    现在只要"同一条语句里既 setDate/getDate 又出现 service_days/serviceDays"就算命中。
_TS_DATE_PLUS_DAYS = re.compile(
    r"set[DM]\w*\s*\([^;]*get[DM]\w*\s*\([^;]*(service_days|serviceDays)"
    r"|get[DM]\w*\s*\(\s*\)\s*\+[^;]*(service_days|serviceDays)"
)
_TS_365_FALLBACK = re.compile(r"service_days\s*\|\|\s*365|serviceDays\s*\|\|\s*365")


class TestNoSecondClockFrontend:
    def test_frontend_never_derives_service_end(self):
        hits = [(rel, m.group(0)) for rel, src in frontend_sources()
                for m in _TS_DATE_PLUS_DAYS.finditer(src)]
        assert not hits, f"前端自算服务期至 = 第四口钟: {hits}"

    def test_frontend_no_365_fallback(self):
        hits = [(rel, m.group(0)) for rel, src in frontend_sources()
                for m in _TS_365_FALLBACK.finditer(src)]
        assert not hits, f"前端 365 兜底残留: {hits}"

    def test_frontend_reverse_control(self):
        # 必须命中(前两行是被删掉的真代码,第三行是变异 M24 用来绕过第一版正则的写法)
        assert _TS_DATE_PLUS_DAYS.search("end.setDate(end.getDate() + data.service_days);")
        assert _TS_DATE_PLUS_DAYS.search("d.setDate(d.getDate() + serviceDays);")
        assert _TS_DATE_PLUS_DAYS.search("_e.setDate(_e.getDate() + (data?.service_days ?? 0));")
        assert _TS_365_FALLBACK.search("serviceDays={kw.service_days || 365}")
        # 必须不命中(改好之后的样子)
        assert not _TS_365_FALLBACK.search("serviceDays={kw.service_days ?? null}")
        assert not _TS_DATE_PLUS_DAYS.search("const end = data.contract_end_date;")

    def test_ts_comment_stripper_bites_both_ways(self):
        assert not _TS_365_FALLBACK.search(
            strip_ts_comments("// 旧版是 serviceDays={kw.service_days || 365}\nconst a = 1;")
        )
        assert _TS_365_FALLBACK.search(
            strip_ts_comments("const x = kw.service_days || 365; // 说明\n")
        )

    def test_frontend_reads_contract_end_date_from_backend(self):
        idx = _read(os.path.join(REPO, "frontend/src/pages/Monitoring/index.tsx"))
        assert "contract_end_date" in idx, "监测页必须从后端拿服务期至"
        assert "setCurrentServiceEnd" in idx


# ══════════════════════════ C. 接线锁 ══════════════════════════

class TestWiring:
    @pytest.mark.parametrize("rel,needle", [
        ("server.py", "resolve_activation_period"),
        (os.path.join("api", "selection_api.py"), "resolve_activation_period"),
        (os.path.join("api", "monitoring_api.py"), "read_calendar_period"),
        (os.path.join("db", "monitoring_db.py"), "read_calendar_period"),
    ])
    def test_writers_and_readers_go_through_ssot(self, rel, needle):
        src = strip_py_comments(_read(os.path.join(REPO, rel)))
        assert needle in src, f"{rel} 没走 SSOT({needle} 不见了)"

    def test_activation_endpoints_require_explicit_months(self):
        """三个激活/确认端点的 service_months 必须是必填(Field(...)),不能有默认值。"""
        server = strip_py_comments(_read(os.path.join(REPO, "server.py")))
        sel = strip_py_comments(_read(os.path.join(REPO, "api", "selection_api.py")))
        for src, name in ((server, "server.py"), (sel, "selection_api.py")):
            for m in re.finditer(r"service_months\s*:\s*[^=\n]+=\s*([^\n]+)", src):
                decl = m.group(1)
                assert "Field(..." in decl or "Field(\n" in decl, (
                    f"{name} 里 service_months 又有默认值了: {m.group(0)!r}"
                )

    def test_service_config_no_longer_writes_start_date(self):
        """service-config 是"两钟错开"的写入侧成因:改起始日却不重算结束日。"""
        src = strip_py_comments(_read(os.path.join(REPO, "server.py")))
        block = src[src.find("def update_service_config"):]
        block = block[: block.find("\n@app.")] if "\n@app." in block else block
        assert "UPDATE quotes SET service_days = %s WHERE id = %s" in block
        assert "service_start_date = %s" not in block, "这个端点不许再写服务期起始日"
        assert "SERVICE_PERIOD_NOT_EDITABLE_HERE" in block, "传了起始日必须 400 + 指路"

    def test_rotation_gate_still_reads_service_end_date(self):
        """本包**不改**轮换闸口径(挂 OWNER_DECISION_BRIEF 第 3 项),锁住它别被顺手动了。

        🔴 断言必须钉在 `get_monitoring_enabled_clients` 函数体内。第一版全文件搜
           `q.service_end_date >= CURRENT_DATE`,而 backfill 那个函数里还有一处同样的字符串
           → 把闸条件整条删掉,锁照样绿(变异 M20 存活)。
        """
        src = strip_py_comments(_read(os.path.join(REPO, "db", "monitoring_db.py")))
        i = src.find("def get_monitoring_enabled_clients")
        assert i != -1, "轮换闸函数没了?"
        j = src.find("\ndef ", i + 1)
        body = src[i: j if j != -1 else len(src)]
        assert "q.service_end_date >= CURRENT_DATE" in body, (
            "轮换资格闸的服务期条件不在了 —— 本包不改它的口径"
        )
        # 反向对照:范围切得对(别的函数体里不该含这句"闸"上下文)
        assert "get_service_period_blocked_clients" not in body

    def test_expiry_is_not_silent(self):
        """§1.5:到期摘除必须有内部可见状态 + 提醒。"""
        mon = strip_py_comments(_read(os.path.join(REPO, "db", "monitoring_db.py")))
        sch = strip_py_comments(_read(os.path.join(REPO, "api", "scheduler.py")))
        srv = strip_py_comments(_read(os.path.join(REPO, "server.py")))
        assert "def get_service_period_blocked_clients" in mon
        assert "_notify_rotation_blocked_by_service_period" in sch
        assert "rotation_blocked:" in sch, "去重键要带到期日,才能只提醒一次又不永久沉默"
        assert "auto_monitoring_paused" in srv, "经营后台总览必须能看见"

    def test_renewal_bucket_includes_expiring_and_expired(self):
        src = strip_py_comments(_read(os.path.join(REPO, "api", "m3_api.py")))
        assert '"expiring"' in src and '"expired"' in src, (
            "续费桶必须收 expiring/expired —— 否则客户恰在最该续费那几天掉出面板"
        )

    def test_migration_028_registered_and_sets_not_null(self):
        manifest = _read(os.path.join(REPO, "db", "migration_manifest.py"))
        code = strip_py_comments(manifest)
        assert "db/migration_028_service_period_ssot_2026_08_06.sql" in code, (
            "迁移没登记 = prestart 永远不会跑它(它不 glob 目录)"
        )
        sql = _read(os.path.join(REPO, "db", "migration_028_service_period_ssot_2026_08_06.sql"))
        assert "ALTER TABLE quotes ALTER COLUMN service_days SET NOT NULL" in sql, (
            "没有这条 NOT NULL,删掉 365 兜底就不再安全"
        )
        # 反向对照:登记检查读的是代码不是注释
        assert "db/migration_028_service_period_ssot_2026_08_06.sql" in code.replace(" ", "")

    def test_no_rollback_028_registered(self):
        code = strip_py_comments(_read(os.path.join(REPO, "db", "migration_manifest.py")))
        assert "rollback_028" not in code
