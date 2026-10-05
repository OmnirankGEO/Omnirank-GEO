# -*- coding: utf-8 -*-
"""WO_221-c1 注毒台。

🔴 毒的清单不是从判据倒推的(WO_220 的教训:**作者的毒来自作者的判据**)。
   这里是从**本单承诺的每一样**列出来的:
     承诺 A 官方线零处退役名        -> N1..N4 每处各一发
     承诺 B 百炼侧一个字不动        -> N5/N6 反向毒(改了必须红)
     承诺 C 回显锁被接上           -> N7/N8 摘掉调用
     承诺 D 缺 model 字段不当通过   -> N9
     承诺 E 归属法是硬的           -> N10 让它恒返一个值(仪器死亡)
"""
import hashlib
import io
import os
import re
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PKG = "tests/monitoring_retired_name_2026_09_15"
EXPECTED_N = 23

LINEAGE = "services/ai_surface_monitoring/lineage.py"
PLATFORMS = "services/research_monitor/platforms.py"
TESTER = "tools/ai_visibility/ai_tester.py"
BATCH = "tools/monitoring/batch_monitor.py"
MODELS = "config/deepseek_models.py"
CENSUS = "tests/model_line_flash_census_2026_09_15/census.py"

# 真正的毒表
POISONS = [
    ("NC 无害注释", BATCH,
     '        "kimi": ("moonshot", "kimi-k2.6", "ai_search", "web_search_tool"),',
     '        "kimi": ("moonshot", "kimi-k2.6", "ai_search", "web_search_tool"),  # 无害注释',
     "负样本 —— 必须读绿", 1),

    # ── 承诺 A:官方线零处退役名 ──
    #: 🔴 锚从**文件里取**,不凭记忆拼多行文本 —— 第一版拼的锚 0 命中,
    #:   注毒台当场报「毒没下成,不判」而不是读绿。
    #: 🔴 锚从**文件里取**,不凭记忆拼(第一版拼的锚 0 命中,注毒台报「毒没下成」)。
    #:   c1' 改写了这段注释 ⇒ 锚随之重建 —— **改判据文案会让注毒锚静默失效**。
    ("N1 活面(lineage 的 native_with_search)退回旧名", LINEAGE,
     '        provider_key="deepseek_official",\n        #: 🔴 [WO_221-c1] 官方线(provider_key=deepseek_official · DEEPSEEK_API_KEY)· availability=active · default_enabled=True —— **真正在污染监测数据的就是这一面**,工单没点到。\n        #:   官方页 `deepseek-v4-flash` 已退役。**两条端点行为不同**(2026-09-15 各打一发实测):\n        #:     /v1/chat/completions   请求旧名 -> 200,回显 `deepseek-flash`(厂商归一)\n        #:     /anthropic/v1/messages 请求旧名 -> 200,回显 `deepseek-v4-flash`(**原样**)\n        #:   ⇒ 在 Anthropic 那条端点上回显锁是**同义反复**,抓不到「旧名被 flash 承接」;\n        #:     那条线的防线是**发出前先归一**。\n        default_model_key=DEEPSEEK_OFFICIAL_FLASH,',
     '        provider_key="deepseek_official",\n        #: 🔴 [WO_221-c1] 官方线(provider_key=deepseek_official · DEEPSEEK_API_KEY)· availability=active · default_enabled=True —— **真正在污染监测数据的就是这一面**,工单没点到。\n        #:   官方页 `deepseek-v4-flash` 已退役。**两条端点行为不同**(2026-09-15 各打一发实测):\n        #:     /v1/chat/completions   请求旧名 -> 200,回显 `deepseek-flash`(厂商归一)\n        #:     /anthropic/v1/messages 请求旧名 -> 200,回显 `deepseek-v4-flash`(**原样**)\n        #:   ⇒ 在 Anthropic 那条端点上回显锁是**同义反复**,抓不到「旧名被 flash 承接」;\n        #:     那条线的防线是**发出前先归一**。\n        default_model_key="deepseek-v4-flash",',
     "真正在跑的那一面退回旧名 —— 工单漏掉的就是它", 1),

    ("N2 血缘标签(batch_monitor)退回旧名", BATCH,
     '        "deepseek": ("deepseek_official", DEEPSEEK_OFFICIAL_FLASH, "ai_search", "deepseek_native"),',
     '        "deepseek": ("deepseek_official", "deepseek-v4-flash", "ai_search", "deepseek_native"),',
     "写进观测账本的那一格错了 ⇒ 整列引擎名错,且不报错", 1),

    ("N3 ai_tester 退回旧名", TESTER,
     "DEEPSEEK_OFFICIAL_MODEL = DEEPSEEK_OFFICIAL_FLASH",
     'DEEPSEEK_OFFICIAL_MODEL = "deepseek-v4-flash"',
     "Anthropic 兼容端点那一条", 1),

    #: 🔴 锚随 c1' 的归一改写失效过一次(0 命中,注毒台拒判)——
    #:   **改代码会让注毒锚静默失效**,而「新红 0 条」和「锁有牙」读数一样。
    ("N4 platforms 兜底退回旧名", PLATFORMS,
     "        _load_model_from_config('model_deepseek_official', DEEPSEEK_OFFICIAL_FLASH)",
     "        _load_model_from_config('model_deepseek_official', 'deepseek-v4-flash')",
     "官方通道的模型兜底写回字面量旧名", 1),

    # ── 承诺 B:百炼侧一个字不动(反向毒) ──
    ("N5 [反向] 百炼兜底血缘被顺手改", BATCH,
     '        return ("dashscope", "deepseek-v4-flash",',
     '        return ("dashscope", DEEPSEEK_OFFICIAL_FLASH,',
     "百炼上这个 ID 没退役;改了就是把兜底态也污染了", 1),

    ("N6 [反向] 百炼 legacy 面被顺手改", LINEAGE,
     '        provider_key="dashscope",  # 实际供应商是阿里百炼,不是 deepseek 官方' + chr(10) +
     '        default_model_key="deepseek-v4-flash",',
     '        provider_key="dashscope",  # 实际供应商是阿里百炼,不是 deepseek 官方' + chr(10) +
     '        default_model_key=DEEPSEEK_OFFICIAL_FLASH,',
     "百炼那一面被顺手改 —— 它的 provider_key 是 dashscope", 1),

    #: ══════════════════════════════════════════════════════════════
    #: 🔴 [c1'] N11–N13 来自 **Review 的毒**,在我这儿全部存活过。
    #:   我的 `test_both_official_call_sites_actually_call_the_echo_assert` 是
    #:   AST「调用存在」锁 —— 它看不见**谁接住了异常**,也看不见**它在 record 前还是后**。
    #:   返回值对不对、有没有被接上、接住之后做了什么,是**三件事**。
    #: ══════════════════════════════════════════════════════════════
    ("N11 [Pa] 回显锁挪到 record(success=True) 之后", PLATFORMS,
     "            assert_official_echo(model, data.get('model'))\n            input_tokens, output_tokens, cached_tokens = usage_from_response_payload(data)",
     "            input_tokens, output_tokens, cached_tokens = usage_from_response_payload(data)\n            tracker.record(input_tokens=input_tokens, output_tokens=output_tokens, cached_tokens=cached_tokens, success=True)\n            assert_official_echo(model, data.get('model'))",
     "换模型的那一次被记成成功 —— 成本表与 WO_215 心跳都看不出异常", 1),

    ("N12 [Pb] 回显锁被 try/except pass 吞掉", PLATFORMS,
     "            assert_official_echo(model, data.get('model'))",
     "            try:\n                assert_official_echo(model, data.get('model'))\n            except Exception:\n                pass",
     "锁抛了但没人让它生效 —— 「调用存在」照样绿", 1),

    #: 🔴 [c1''] N16 按 Review 的 Pc **原样形态**:不改字面量,而是让那个 return
    #:   **不执行**(塞进 if False)。上一版判据是源码切片,文本还在就读绿 ——
    #:   毒要打的是「分支有没有真的走到」,不是「文本在不在」。
    ("N16 [Pc 原样] 回显不符分支不执行", TESTER,
     "                except OfficialModelEchoMismatch as _echo_err:\n                    print(f'[DeepSeek-Official] {_echo_err}')\n                    return ToolResponse(",
     "                except OfficialModelEchoMismatch as _echo_err:\n                    print(f'[DeepSeek-Official] {_echo_err}')\n                    if False:\n                        return ToolResponse(",
     "上游把一次身份不明的回答当成正常结果记进监测", 1),

    ("N13 [Pc] ai_tester 回显不符不返 engine_error", TESTER,
     '                        "engine_error": True,' + chr(10) +
     '                        "mentioned_brands": [], "brand_detected": False,' + chr(10) +
     '                        "web_search_enabled": False, "search_citations": [], "full_response": "",' + chr(10) +
     '                    }, ensure_ascii=False)}])' + chr(10) + chr(10) +
     '                ai_response = _deepseek_official_text(data)',
     '                        "engine_error": False,' + chr(10) +
     '                        "mentioned_brands": [], "brand_detected": False,' + chr(10) +
     '                        "web_search_enabled": False, "search_citations": [], "full_response": "",' + chr(10) +
     '                    }, ensure_ascii=False)}])' + chr(10) + chr(10) +
     '                ai_response = _deepseek_official_text(data)',
     "上游把一次身份不明的回答当成正常结果", 1),

    #: ── c1' 新增的两面 ──
    ("N14 官方通道取消归一", PLATFORMS,
     "    model = normalize_deepseek_model(",
     "    model = (lambda x: x)(",
     "Anthropic 端点回显原样 ⇒ 配置里的旧名会静默照发,锁放行", 1),

    ("N15 成本占位行退回百炼", "db/monitoring_db.py",
     '        "deepseek": _deepseek_cost_identity(),',
     '        "deepseek": ("dashscope", "deepseek-v4-flash"),',
     "DeepSeek 平台的成本记到百炼名下(WO_214 同族)", 1),
    # ── 承诺 C:回显锁被接上 ──
    ("N7 platforms 摘掉回显锁调用", PLATFORMS,
     "            assert_official_echo(model, data.get('model'))",
     "            pass  # 回显锁被摘",
     "锁还在、没人调 —— 返回值对不对和它有没有被接上是两件事", 1),

    ("N8 ai_tester 摘掉回显锁调用", TESTER,
     "                    assert_official_echo(DEEPSEEK_OFFICIAL_MODEL, data.get('model'))",
     "                    pass  # 回显锁被摘",
     "同上,另一侧", 1),

    # ── 承诺 D:缺字段不当通过 ──
    ("N9 缺 model 字段放行", MODELS,
     '    if not got:\n        raise OfficialModelEchoMismatch(\n            "响应里没有 model 字段(请求 %s)—— 官方响应应当回显模型名" % want)',
     '    if not got:\n        return',
     "宽容处理正是「会返 200 的错答案」长期存活的方式", 1),

    # ── 承诺 E:归属法是硬的 ──
    ("N10 归属法恒返 dashscope(仪器死亡)", CENSUS,
     "    same = src[i].split(\"#\", 1)[0]",
     "    return (\"dashscope\", \"POISON\", line)\n    same = src[i].split(\"#\", 1)[0]",
     "恒返一个值 ⇒ 「官方线零处」恒真。空集合与判据没在数在读数上同形", 1),
]


def sha(rel):
    return hashlib.sha256(io.open(os.path.join(ROOT, rel), "rb").read()).hexdigest()[:12]


def run():
    env = dict(os.environ)
    env.setdefault("TEST_DATABASE_URL",
                   "postgresql://geo_admin:testpw@127.0.0.1:55492/geo_c14_222_test")
    env.setdefault("DATABASE_URL", env["TEST_DATABASE_URL"])
    p = subprocess.run([sys.executable, "-m", "pytest", PKG, "-q", "--no-header",
                        "-p", "no:cacheprovider", "-p", "no:warnings"],
                       cwd=ROOT, capture_output=True, env=env)
    out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
    failed = set(re.findall(r"^FAILED [^:]+::(\S+)", out, re.M))
    collected = sum(int(m.group(1))
                    for m in re.finditer(r"(\d+) (?:passed|failed|error)", out))
    return collected, failed, out


def main():
    print("=" * 78)
    base_c, base_f, base_out = run()
    print("基线: collected=%d  failed=%s" % (base_c, sorted(base_f) or "空"))
    if base_c != EXPECTED_N or base_f:
        print("🔴 基线不对(条数 %d,红 %s)" % (base_c, sorted(base_f)))
        print(base_out[-1600:])
        return 2

    results = []
    for name, rel, anchor, repl, why, want in POISONS:
        if want == 0:
            continue
        full = os.path.join(ROOT, rel)
        raw = io.open(full, "rb").read()
        src = raw.decode("utf-8")
        hits = src.count(anchor)
        before = sha(rel)
        if hits != want:
            print("  %-38s 🔴 锚命中 %d 次(要 %d)—— 毒没下成,不判" % (name, hits, want))
            results.append((name, "ANCHOR_MISS", set()))
            continue
        io.open(full, "wb").write(src.replace(anchor, repl).encode("utf-8"))
        assert sha(rel) != before, "下毒后 sha 没变"
        try:
            c, f, _ = run()
        finally:
            io.open(full, "wb").write(raw)
        assert sha(rel) == before, "还原后 sha 对不上基线"
        new = f - base_f
        is_nc = name.startswith("NC")
        ok = (not new) if is_nc else bool(new)
        verdict = (("绿 ✓" if is_nc else "红 ✓") if ok
                   else ("🔴 负样本读红" if is_nc else "🔴 仍绿"))
        print("  %-38s %-8s 新红 %d 条  %s"
              % (name, verdict, len(new), ",".join(sorted(new))[:46]))
        results.append((name, verdict, new))

    print("-" * 78)
    bad = [r for r in results if "✓" not in r[1]]
    if bad:
        print("🔴 这些没过:%s" % [r[0] for r in bad])
        return 1
    print("✅ %d 发毒全部被抓 · 负样本绿"
          % len([r for r in results if not r[0].startswith("NC")]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
