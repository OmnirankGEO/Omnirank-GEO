"""WO_209 c1 判据 · G1 门禁本体:嵌套假阳性 + 三态退出码 + 不许 dark。

工单原文(`C:\\AI-Test\\WO_209_G1_CONTRACT_GATE_FALSE_POSITIVE_AND_DARK_RV_2026-09-14.md`,
sha256 前 16 位 `66a08ef76f0d2ce1`)§1:
  1. 嵌套模型:对 `List[<BaseModel>]` 等字段,把观测到的元素键归到**元素模型**再比对;分母打印。
  2. 三种结局三种退出码:0 通过 / 1 有违规 / **3 没跑成**;第一行明说是哪种。
  3. 自证:打印「真跑了 N 条路由(N>0)」+ 时间戳落盘;dark 超 48h 出声。
判据 C1–C4 见各条 docstring。

🔴 这道闸 2026-09 之前的病不是"漏报",是**恒红**:唯一那条"违规"是它自己的
   假阳性。恒红的闸等于没有闸 —— 它训练所有人忽略它,下一次真违规也会被忽略。
"""
import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "scripts" / "response_model_contract_gate.py"
SUGGEST_PATH = "/api/diagnosis/suggest-questions"


# ════════════════════════════════════════════════════════════════
# C1 · 嵌套响应:元素键归到元素模型
# ════════════════════════════════════════════════════════════════
def test_the_nested_endpoint_reports_no_violation(gate, collected):
    """`suggest-questions` 这类嵌套响应 ⇒ **0 违规**(工单 §1 判据 C1 正面)。

    09-14 实测它报 3 条:`question` / `side` / `layer` 都被算到**外层**模型
    `SuggestQuestionsResponse` 头上。三条反证(工单事实 2):
      ① 这三个键属于 `candidates: List[SuggestedQuestion]` 的内层模型,逐个声明过;
      ② 外层没有 `extra="forbid"`,「漏键必 500」的前提不成立;
      ③ 生产近 7 天该端点 66 次全 200、零 5xx。
    真机制是:handler 里有个内层 helper `_cand()`,收集器 `generic_visit`
    一路钻进去,把它的返回 dict 当成端点自己的返回 —— **比错了对象**。
    """
    routes, _ = collected
    target = [r for r in routes if r["path"] == SUGGEST_PATH]
    assert target, "锚过期:找不到 %s(端点改名/下线了就改这条判据)" % SUGGEST_PATH
    outcome = gate.analyze_endpoint(target[0]["endpoint"], target[0]["model"])
    assert outcome["violations"] == [], (
        "嵌套响应又报假阳性了:%s" % outcome["violations"])


def test_the_inner_keys_are_actually_checked_against_the_inner_model(gate, collected):
    """🔴 反向对照:内层键**真的被比过**,不是"不比了所以不报"。

    少了这条,C1 可以靠「干脆不看 candidates」满足 —— 那是把误报换成瞎,
    而瞎比误报更难发现(误报至少还有人抱怨)。
    这里直接构造"元素模型少一个键"的字段树,断言那一层**会**出违规,
    且路径是 `candidates.<键>`(归到元素模型,不是外层)。
    """
    routes, _ = collected
    target = [r for r in routes if r["path"] == SUGGEST_PATH][0]
    full = gate.model_field_tree(target["model"])
    assert "candidates" in full and isinstance(full["candidates"], dict), (
        "锚过期:candidates 不再是嵌套模型字段 —— 先看代码")
    assert set(full["candidates"]) >= {"question", "side", "layer"}, full["candidates"]

    node = gate._func_ast(target["endpoint"])
    collector = gate._collect_from(node)
    crippled = dict(full)
    crippled["candidates"] = {k: v for k, v in full["candidates"].items()
                              if k != "side"}
    seen = []
    for d in collector.returned_dicts:
        v, _ok = gate._check_dict_against_tree(
            d, crippled, (), collector.assigned, collector)
        seen.extend(tuple(x["path"]) for x in v)
    assert ("candidates", "side") in seen, (
        "元素模型少了 `side`,门禁却没报 —— 说明它根本没跟进 candidates 这一跳;"
        "实得 %s" % (seen,))


def test_the_collector_stops_at_nested_function_boundaries(gate):
    """结构臂:收集器**不下钻**嵌套函数,但要把它们记下来。

    这是 C1 的机制本身。钉它是因为:哪天有人"顺手"去掉 `visit_FunctionDef`,
    三条假阳性会原样回来,而 C1 那条正面判据同时也会红 ——
    但红的原因会被读成"端点改坏了"。这条把原因钉在机制上。
    """
    src = ast.parse(GATE.read_text(encoding="utf-8"))
    cls = [n for n in ast.walk(src)
           if isinstance(n, ast.ClassDef) and n.name == "_ReturnDictCollector"]
    assert cls, "锚过期:收集器改名了"
    names = {f.name for f in cls[0].body
             if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "visit_FunctionDef" in names, (
        "收集器又会钻进嵌套函数了 —— 内层 helper 的返回会被当成端点的返回")
    assert "visit_Lambda" in names, "lambda 体同理,也不该算本函数的返回"


# ════════════════════════════════════════════════════════════════
# C2 / C3 · 三种结局三种退出码(0 通过 / 1 违规 / 3 没跑成)
# ════════════════════════════════════════════════════════════════
def _verdict_line(out):
    """从门禁输出里取**机器可读的结论行**。

    🔴 不读「stdout 第一行」:门禁 `import server`,导入期会刷一屏
       `[OK] xxx 表已创建`,第一行根本不归门禁管。
       工单 §1.2 说的"第一行明说"要落成一个**带前缀的标记行**才可靠 ——
       否则 209-d1 那边解析出来的会是建表日志。
    """
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("g1_probe", GATE)
    mod = module_from_spec(spec)
    spec.loader.exec_module(mod)
    hits = [l for l in out.splitlines() if l.startswith(mod.VERDICT_PREFIX)]
    assert len(hits) == 1, "结论行不是恰好一条(实得 %d 条)—— 下游会解析到歧义" % len(hits)
    return hits[0]


def _run_gate(env_overrides, timeout=600):
    env = dict(os.environ)
    env.update(env_overrides)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    p = subprocess.run([sys.executable, str(GATE)], cwd=str(ROOT),
                       env=env, capture_output=True, timeout=timeout)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def test_a_db_it_cannot_reach_exits_three_and_says_it_did_not_run():
    """C2:库不可达 ⇒ **退出码 3**,且第一行写「没跑成:<原因>」。

    🔴 这条是整张工单的核心。2026-08-14 那次:主仓工作树里没有这个脚本,
       python 报 can't open file,rc≠0 → preflight 打出
       「有 handler 返回了模型未声明的键(上线即 500)」—— **门禁压根没跑**。
       假红和真红在退出码上一模一样,比漏检更坏:它让人学会忽略这个闸。
    🔴 走**真子进程**,断言的是真实退出码 —— preflight 读的就是这个数,
       在进程内调 `main()` 证不了退出码这件事。
    """
    rc, out = _run_gate({"DATABASE_URL":
                         "postgresql://geo_admin:x@127.0.0.1:59999/nope"})
    assert rc == 3, "库不可达却返 %d(1=会被读成有违规,0=会被读成通过)" % rc
    line = _verdict_line(out)
    assert "did-not-run" in line, "结论行没说这是「没跑成」:%r" % line


def test_zero_routes_exits_three_not_zero(gate, monkeypatch, capsys):
    """C3:路由收集 0 条 ⇒ **退出码 3**,不是 0;且两种"零"要说**不同的话**。

    0 条路由时门禁什么都没检查,把它算成"通过"就是把 dark 当合格 ——
    09-04 之后这道闸 dark 了数天,没有任何人看见。

    🔴 只钉退出码不够。两种"零"是运维要做的**两件事**:
       · 一条路由都没有 ⇒ server 没起来 / router 没注册(去看进程和导入);
       · 有路由但没有一条声明 response_model ⇒ 覆盖数 0(去看模型声明)。
       两者都返 3,所以只钉退出码时,**任一道闸被摘掉都不会红**(另一道兜住了)
       —— 注毒实测:把第一道改成 `if False` 全程绿。冗余的闸不是坏事,
       但"哪一道开的火"必须有人钉,否则摘掉一道之后错误提示会指错方向。
    """
    monkeypatch.setattr(gate, "collect_routes", lambda: ([], 0))
    assert gate.main([]) == 3
    no_route_msg = capsys.readouterr().out
    assert "收集到 0 条路由" in no_route_msg, (
        "一条路由都没有,提示却不是「收集到 0 条路由」—— 第一道闸被摘了,"
        "运维会被指去查模型声明,而真正的问题是 server 没起来。实得:%s"
        % no_route_msg.strip().splitlines()[:2])

    monkeypatch.setattr(gate, "collect_routes", lambda: ([], 120))
    assert gate.main([]) == 3, "有路由但覆盖数 0 —— 同样是瞎,不能算通过"
    no_model_msg = capsys.readouterr().out
    assert "没有一条声明 response_model" in no_model_msg, no_model_msg[:200]


def test_the_three_exit_codes_are_distinct_constants(gate):
    """三个结局必须是**三个**不同的码。

    少了它,「0 通过 / 1 违规 / 3 没跑成」可以退化成两态而判据不红。
    """
    assert (gate.EXIT_OK, gate.EXIT_VIOLATION, gate.EXIT_DID_NOT_RUN) == (0, 1, 3)


# ════════════════════════════════════════════════════════════════
# §1.3 · 自证:真跑了多少条 + 时间戳落盘 + dark 出声
# ════════════════════════════════════════════════════════════════
def test_a_successful_run_prints_the_denominator_and_stamps_the_clock(tmp_path, gate):
    """每次跑都要打印「真跑了 N 条路由」并把时间戳落盘。

    🔴 落的是「门禁**执行过**」,不是「仓库没问题」—— 所以有违规也要落。
       只在通过时落的话,一个长期有违规的仓库会被判成 dark,
       而真正 dark 的那段(闸根本没跑)反而看不出来。
    """
    rc, out = _run_gate({"G1_STATE_DIR": str(tmp_path)})
    line = _verdict_line(out)
    assert rc in (0, 1), "这次跑不该是「没跑成」:%r" % line
    assert ("ok" in line) or ("violation" in line), line
    m = re.search("真跑了 ([0-9]+) 条路由", out)
    assert m and int(m.group(1)) > 0, "没打印「真跑了 N 条路由」或 N=0:%r" % line
    stamp = tmp_path / "g1_last_ok.txt"
    assert stamp.exists() and stamp.read_text(encoding="utf-8").strip(), (
        "时间戳没落盘 —— dark 检测就没有依据")


def test_it_says_so_when_the_last_successful_run_is_too_old(tmp_path, gate):
    """dark 超过阈值要**出声**(工单 §1.3)。

    🔴 反向对照在下一条:刚跑过就不许报 dark,否则「永远报 dark」
       也能让这条绿,而那会让人把告警整体静音。
    """
    (tmp_path / "g1_last_ok.txt").write_text(
        "2026-09-01T00:00:00+00:00", encoding="utf-8")
    rc, out = _run_gate({"G1_STATE_DIR": str(tmp_path)})
    assert rc in (0, 1)
    assert "小时前,超过" in out, "上次成功运行是两周前,门禁却没出声:%s" % out[-300:]


def test_a_fresh_stamp_does_not_raise_the_dark_alarm(tmp_path, gate):
    """反向对照:刚跑过 ⇒ 不报 dark。"""
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    (tmp_path / "g1_last_ok.txt").write_text(now, encoding="utf-8")
    rc, out = _run_gate({"G1_STATE_DIR": str(tmp_path)})
    assert rc in (0, 1)
    assert "小时前,超过" not in out, "刚跑过却报 dark —— 这种告警会被整体静音"


# ════════════════════════════════════════════════════════════════
# C4 · 存量 3 条逐条分类(不许「其它」)
# ════════════════════════════════════════════════════════════════
#: 09-14 Deploy 在 0913c preflight 上实测到的 3 条违规,逐条定性。
#: 🔴 工单要求"真违规 / 门禁假阳性"二选一,**不许「其它」**:
#:    "其它"是把没读懂的那条藏起来的地方。
FROZEN_2026_09_14 = [
    ("POST /api/diagnosis/suggest-questions", "question", "门禁假阳性"),
    ("POST /api/diagnosis/suggest-questions", "side", "门禁假阳性"),
    ("POST /api/diagnosis/suggest-questions", "layer", "门禁假阳性"),
]


def test_every_legacy_violation_is_classified_and_none_survive(gate, collected):
    """C4:3 条存量违规逐条分类,且修完之后**一条不剩**。

    三条同一个根因:handler 内层 helper `_cand()` 的返回 dict 被当成端点自己的返回,
    拿去和外层模型比。所以三条都是**门禁假阳性**,真违规 0 条。
    定性依据(工单事实 2 的三条反证)写在 `test_the_nested_endpoint_reports_no_violation`。
    """
    assert {c for _, _, c in FROZEN_2026_09_14} == {"门禁假阳性"}, (
        "分类里出现了「真违规」或「其它」—— 真违规要修代码不是修门禁")

    routes, _ = collected
    live = []
    for r in routes:
        outcome = gate.analyze_endpoint(r["endpoint"], r["model"])
        for v in outcome["violations"]:
            live.append(("%s %s" % ("/".join(r["methods"]), r["path"]),
                         ".".join(v["path"])))
    assert live == [], (
        "门禁又报违规了。若确是真违规 ⇒ 修 handler/模型;"
        "若是新的假阳性 ⇒ 按 C1 的形状补跟进,别放宽判据。实得:%s" % live)


# ════════════════════════════════════════════════════════════════
# c1' · 门禁**自述「看没看过」**的三个维度
#
# 这三条都不是"漏报违规",是**谎报覆盖**:门禁声称看过了,其实看的是
# 过期快照 / 根本跟不动。谎报覆盖比漏报更坏 —— 漏报还留着 G2 兜底的指望,
# 谎报会让人以为这一条已经被静态层管住了。
# ════════════════════════════════════════════════════════════════
def _probe(gate, src, tree):
    """在合成 AST 上跑一次比对,返回 (违规路径, 可信)。

    🔴 用合成 AST 而不是找一个真端点:真端点的形状会随业务改动漂走,
       而这三条钉的是**机制**。锚在真代码上的话,哪天那个端点被重写,
       判据会以"找不到锚"转红,而那与机制对不对无关。
    """
    import ast as _ast
    import textwrap

    node = _ast.parse(textwrap.dedent(src)).body[0]
    c = gate._collect_from(node)
    paths, ok = [], True
    for d in c.returned_dicts:
        v, sub_ok = gate._check_dict_against_tree(d, tree, (), c.assigned, c)
        paths.extend(".".join(x["path"]) for x in v)
        ok = ok and sub_ok
    return paths, ok


@pytest.mark.parametrize("mutation,label", [
    ('    payload["sneaky"] = 2', "下标赋值"),
    ("    payload.update({'x': 1})", ".update()"),
    ("    payload.setdefault('x', 1)", ".setdefault()"),
    ("    payload.pop('a')", ".pop()"),
    ("    payload |= {'x': 1}", "|="),
])
def test_a_mutated_local_dict_is_not_treated_as_seen(gate, mutation, label):
    """🔴 本地 dict **赋值之后又被改写** ⇒ 展开出来的是过期快照,必须记不可信。

    c1 把 `{**本地 dict}` 改成"静态可知就展开"是对的,但漏了这一层:
    展开的是**赋值那一刻**的键集合。之后 `payload["sneaky"]=…` 塞进来的键
    门禁看不见,却因为展开成功而把这一条记成**可信**。
    改之前那形状是记不可信的 —— 所以这是 c1 **引入的退步**:
    从"我不知道"变成"我看过了",而后者没人会再去查。
    """
    src = chr(10).join(["def h():", "    payload = {'a': 1}", mutation,
                        "    return {**payload, 'b': 3}"])
    _paths, ok = _probe(gate, src, {"a": None, "b": None})
    assert ok is False, "%s 之后仍被记成可信 —— 展开的是过期快照" % label


def test_an_unmutated_local_dict_is_still_expanded(gate):
    """反向对照:没被改写的本地 dict **照常展开**。

    少了它,上一条可以靠「所有 `**` 一律记不可信」满足 —— 那就退回 c1 之前,
    `candidates` 这一跳又没人看了。
    """
    src = chr(10).join(["def h():", "    payload = {'a': 1}",
                        "    return {**payload, 'b': 3}"])
    _paths, ok = _probe(gate, src, {"a": None, "b": None})
    assert ok is True, "没被改写却不展开 —— 覆盖白白缩回去了"

    # 而且展开之后**真的在比**:模型少一个键就要报出来
    paths, _ok = _probe(gate, src, {"b": None})
    assert paths == ["a"], "展开了却没比:%s" % paths


def test_an_element_list_it_cannot_follow_is_recorded_as_not_seen(gate):
    """🔴 跟不动某字段里放了什么 ⇒ 记不可信,**不是**记"没问题"。

    `_element_dicts_for` 的 docstring 早就写着"跟不动返空,调用方记
    trustworthy=False" —— 而调用方**没做**。注释说了什么不算数,只有代码算数
    (本仓 `a-wrong-comment-outlives-a-wrong-assertion`)。
    """
    src = chr(10).join(["def h(items):", "    return {'candidates': items}"])
    _paths, ok = _probe(gate, src, {"candidates": {"question": None}})
    assert ok is False, "元素来自参数、根本跟不动,却记成了可信"


def test_an_element_list_it_can_follow_stays_trustworthy(gate):
    """反向对照:跟得动就不许记不可信。

    少了它,上一条可以靠「凡是嵌套模型字段一律记不可信」满足 ——
    那样盲区数会虚高,而虚高的盲区和真盲区一样会被整体忽略。
    """
    src = chr(10).join(["def h():", "    def _c(q):",
                        "        return {'question': q}",
                        "    return {'candidates': [_c(1), _c(2)]}"])
    _paths, ok = _probe(gate, src, {"candidates": {"question": None}})
    assert ok is True


def test_the_route_count_is_printed_on_the_violation_path_too(gate, monkeypatch, capsys):
    """🔴 「真跑了 N 条路由」**每次**都要打印,包括有违规那次(工单 §1.3)。

    c1 第一版只在通过路径打。而 §1.3 落盘那条判据容忍 rc∈(0,1) 却断言这句 ——
    真出现违规那天,判据会自己红,而红的原因跟那个违规毫无关系。
    「判据只在某一条分支上成立」是本仓的老病
    (`criteria-all-lived-on-the-fixed-side-of-the-seam`)。
    """
    monkeypatch.setattr(gate, "analyze_endpoint", lambda func, model: {
        "violations": [{"path": ("造出来的键",), "lineno": 1, "file": "x.py"}],
        "analyzed": True, "trustworthy": True, "reason": ""})
    rc = gate.main([])
    out = capsys.readouterr().out
    assert rc == 1, "造了违规却不是 1:%d" % rc
    assert "violation" in out, out[:200]
    assert re.search("真跑了 [0-9]+ 条路由", out), (
        "有违规那条路径没打印「真跑了 N 条路由」—— §1.3 要求每次都打:%s"
        % out.splitlines()[:3])


# ════════════════════════════════════════════════════════════════
# c1'' · 还剩两种「元素零比较,却记可信」的形状
#
# 共性:`_element_dicts_for` 走完一圈**一条元素都没收到**,却因为没触发
# 任何一条"跟不动"而返回空列表 —— 调用方于是当成"看过了,没问题"。
# 零比较 + 声称看过,正是 c1' 要消灭的那类谎报,只是换了个入口。
# ════════════════════════════════════════════════════════════════
def test_an_inner_function_that_returns_a_parameter_is_not_seen(gate):
    """内层函数直接把参数返回去(`def _c(x): return x`)⇒ 记不可信。

    元素到底是什么形状,取决于调用点传进去的是什么 —— 静态跟不到。
    原来只挡了 `returned_calls` / 看不懂的返回,这一种从缝里过去了。
    """
    src = chr(10).join(["def h(q):", "    def _c(x):", "        return x",
                        "    return {'candidates': [_c(q)]}"])
    _paths, ok = _probe(gate, src, {"candidates": {"question": None}})
    assert ok is False, "内层返参数却记成可信 —— 元素一条都没比过"


def test_an_inner_function_that_returns_a_call_result_is_not_seen(gate):
    """内层函数把名字绑到调用结果再返回(`d = build(x); return d`)⇒ 记不可信。

    与上一条同源:`returned_names` 里那个名字不在 `inner.assigned`(它绑的是
    调用结果不是 dict 字面量),于是既收不到元素、也没人说"跟不动"。
    """
    src = chr(10).join(["def h(q):", "    def _c(x):", "        d = build(x)",
                        "        return d",
                        "    return {'candidates': [_c(q)]}"])
    _paths, ok = _probe(gate, src, {"candidates": {"question": None}})
    assert ok is False, "内层返调用结果名却记成可信"


def test_a_local_dict_handed_to_a_call_is_not_seen(gate):
    """本地 dict **被当参数传出去**之后再 `{**payload}` ⇒ 记不可信。

    `enrich(payload)` 之后 payload 里有什么,静态看不见。被调方往里塞一个键,
    门禁照样声称"我展开看过了"。
    🔴 宁可多记一条盲区,也不要多一条谎报:
       盲区是**知道自己不知道**,谎报是**不知道自己不知道**。
    """
    src = chr(10).join(["def h():", "    payload = {'a': 1}",
                        "    enrich(payload)",
                        "    return {**payload, 'b': 2}"])
    _paths, ok = _probe(gate, src, {"a": None, "b": None})
    assert ok is False, "本地 dict 传出去过,展开的仍可能是过期快照"


def test_handing_an_unrelated_name_to_a_call_does_not_blind_the_dict(gate):
    """反向对照:传出去的是**别的**名字,不影响这个 dict 的展开。

    少了它,上一条可以靠「函数里只要有任何调用就全记不可信」满足 ——
    那样盲区会虚高,而虚高的盲区和真盲区一样会被整体忽略。
    """
    src = chr(10).join(["def h(other):", "    payload = {'a': 1}",
                        "    log(other)",
                        "    return {**payload, 'b': 2}"])
    _paths, ok = _probe(gate, src, {"a": None, "b": None})
    assert ok is True, "传的是别的名字,却把 payload 也记成不可信了"
