"""窗G 反向变异 —— 逐发拆掉本包接的那几处,看判据会不会红。

用法(worktree 根目录)::

    TEST_DATABASE_URL=postgresql://geo_admin:testpw@localhost:55481/xbexec_pkgg_test \\
        python tests/xiaobang_execute_2026_08_20/_mutation_runner_pkgg.py

🔴 纪律(照抄 tests/defensive_geo_w4_2026_08_22/_mutation_runner.py):
   · 锚点必须**唯一**(命中数 != 1 判 ANCHOR-AMBIGUOUS,不是"打第一处");
   · 还原用**内存原文写回**并逐字节校验,**禁 git checkout**;
   · 读写一律 ``io.open(..., newline="")`` —— Windows 上不加会把 LF 翻成 CRLF,
     还原后文件与原文不逐字节相同(本仓记过);
   · 判定四态:ANCHOR-MISS / ANCHOR-AMBIGUOUS / SURVIVED / RED-BUT-WRONG-NODE / KILLED;
   · 变异窗**零并行**。
"""
from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUITE = "tests/xiaobang_execute_2026_08_20"

MUTATIONS: list[dict] = [
    {
        "id": "M1-摘掉 confirm 的预估门",
        "why": "Owner 2026-08-24:没有预估不许确认。摘掉它,用户又能在一个数字都没看见时按确认。",
        "file": "api/xiaobang_operations_api.py",
        "old": "                assert_estimate_shown(row)\n",
        "new": "                pass  # MUTANT: 预估门被摘掉\n",
        "expect_red": ["test_confirm_is_refused_when_nobody_can_explain_the_missing_estimate"],
    },
    {
        "id": "M2-让取数口凭空造一个数字",
        "why": ("frozen_estimate_amount 是全仓唯一的「这次估了多少」取数口。"
                "让它在没报价时也返回一个数,confirm 与 execute 两侧的门同时失效。"),
        "file": "services/defensive_geo/xiaobang/compute_estimate.py",
        "old": "    amount = intent_row.get(\"compute_quote_amount\")\n    if amount is None:\n        return None\n    return int(amount)\n",
        "new": "    amount = intent_row.get(\"compute_quote_amount\")\n    if amount is None:\n        return 1  # MUTANT: 凭空造数字\n    return int(amount)\n",
        "expect_red": ["test_confirm_is_refused_when_nobody_can_explain_the_missing_estimate"],
    },
    {
        "id": "M3-拆掉 POR-13 回执守卫的委派",
        "why": ("把 assert_receipt_usable 换成「查到行就放行」,回执绑在上一版也照样执行。"
                "这一发直接打 receipt_guard 的接线 —— 在窗G 之前它是只有判据在 import 的死函数。"),
        "file": "api/xiaobang_operations_api.py",
        "old": "        assert_receipt_usable(\n",
        "new": "        _ = receipt_row is not None or assert_receipt_usable(  # MUTANT: 守卫被短路\n",
        "expect_red": ["test_a_receipt_bound_to_an_older_revision_is_refused"],
    },
    {
        "id": "M4-把 Owner 的句式改掉",
        "why": "「本次预计消耗你的算力 X,确认后开始」是 Owner 逐字给的。改了就不是他要的那句话。",
        "file": "services/defensive_geo/copy_registry.py",
        "old": "    \"quoted\": \"本次预计消耗你的算力 {amount}，确认后开始\",\n",
        "new": "    \"quoted\": \"预计 {amount}\",  # MUTANT: 句式被改\n",
        "expect_red": ["test_the_estimate_sentence_owner_asked_for_is_actually_on_the_wire"],
    },
    {
        "id": "M5-让估价段顺手签一张回执",
        "why": ("估价段零副作用。签回执 = 替用户点了确认 —— 这是最坏的一种副作用,"
                "而它不动钱,只数 point_freezes 的判据抓不到。"),
        "file": "api/xiaobang_operations_api.py",
        "old": "        conn.commit()\n\n    return _clean(_intent_dto(row, entry=entry, extra={\n        \"interaction_id\": payload.interaction_id,\n",
        "new": "        cursor.execute(  # MUTANT: 估价段偷签回执\n            \"INSERT INTO xiaobang_confirmation_receipts (receipt_id, intent_id,\"\n            \" intent_revision, actor_user_id, challenge_hash, receipt_expires_at)\"\n            \" VALUES (%s,%s,%s,%s,%s, NOW() + interval '10 min')\"\n            \" ON CONFLICT DO NOTHING\",\n            (\"xcr_mutant\", str(row[\"intent_id\"]), int(row[\"intent_revision\"]),\n             int(actor.actor_user_id), \"m\"))\n        conn.commit()\n\n    return _clean(_intent_dto(row, entry=entry, extra={\n        \"interaction_id\": payload.interaction_id,\n",
        "expect_red": ["test_estimate_stage_has_zero_funding_side_effects"],
    },
    {
        "id": "M6-让 confirm 门抢走领域的答案",
        "why": ("反向:本仓已签发过「必须是渠道资格那条码,不是『还没算出算力』」。"
                "把门改成凡没数字就拦,这一发必须被那条反向判据抓住 —— "
                "它守的是「我修一个洞时没顺手把另一件对的事弄坏」。"),
        "file": "services/defensive_geo/xiaobang/compute_estimate.py",
        "old": "    if bool(preview.get(DOMAIN_BLOCKER_KEY)):\n        return None\n",
        "new": "    if False:  # MUTANT: 不再让路给领域\n        return None\n",
        "expect_red": ["test_confirm_does_not_steal_the_domains_answer"],
    },
    # -- 段二② POR-13 hash 三维:把任一维改回自比较 → 必须红 --
    {
        'id': 'M7-payload_hash 改回自比较',
        'why': '改回 row 自己的列 ⇒ 那一支恒 False ⇒ 内容漂移永远测不出来。',
        'file': 'api/xiaobang_operations_api.py',
        'old': '        "payload_hash": content_hash(frozen),\n',
        'new': '        "payload_hash": row["payload_hash"],  # MUTANT: 自比较\n',
        'expect_red': ['test_a_tampered_frozen_payload_no_longer_matches_its_own_hash'],
    },
    {
        'id': 'M8-object_manifest_hash 改回自比较',
        'why': '这一维是「她刚才看的那份内容还是不是现在这份」。改回自比较 = POR-13 的 object 维没人守。',
        'file': 'api/xiaobang_operations_api.py',
        'old': '        "object_manifest_hash": content_hash(manifest),\n',
        'new': '        "object_manifest_hash": row["object_manifest_hash"],  # MUTANT: 自比较\n',
        'expect_red': ['test_object_drift_between_prepare_and_confirm_is_refused_in_plain_words'],
    },
    {
        'id': 'M9-compute_quote_hash 改回自比较',
        'why': '改回自比较 ⇒ 价目在 prepare 与 confirm 之间变了也照样放行,她确认的数字不是要扣的数字。',
        'file': 'api/xiaobang_operations_api.py',
        'old': '        "compute_quote_hash": ((quote.quote_hash if quote is not None else None)\n                               if include_price else row.get("compute_quote_hash")),\n',
        'new': '        "compute_quote_hash": row.get("compute_quote_hash"),  # MUTANT: 自比较\n',
        'expect_red': ['test_price_drift_between_prepare_and_confirm_says_it_is_the_price'],
    },
    {
        'id': 'M10-把两档漂移文案压成同一句',
        'why': 'Owner 点名要区分「内容变了/价格变了」两种下一步。压成一句她只能重走全流程。',
        'file': 'services/defensive_geo/xiaobang/drift_notice.py',
        'old': '    **{r: "price_changed" for r in PRICE_DRIFT},\n',
        'new': '    **{r: "content_changed" for r in PRICE_DRIFT},  # MUTANT: 两档压成一句\n',
        'expect_red': ['test_price_drift_between_prepare_and_confirm_says_it_is_the_price'],
    },
    {
        'id': 'M11-把合同计费码改回旧码',
        'why': ('改回去 ⇒ prepare 查的又是另一条链的目录条目(不是 execute 末端真正冻结的那个码),'
                '负向锁也该红。⚠️ 早先这里写的是「目录价又查不到 ⇒ 报价档退回 unavailable」,'
                '那条根因已撤回:生产两个码都在,查得到。'),
        'file': 'services/gap_operation_map.py',
        'old': '    billing_feature="media_proxy_publish",\n',
        'new': '    billing_feature="media_publish",  # MUTANT: 改回旧码\n',
        'expect_red': ['test_prepare_can_actually_find_the_catalog_price_now'],
    },
    {
        'id': 'M12-让路由声明与 handler 实扣打架',
        'why': ('[Review 2026-08-24 裁定条件① 重写] 旧 M12 的前提是「动这一格会改变组织预算归属」,'
                'census 证伪了它(该字段无运行期消费方)。现在守的不是「原样不动」而是「声明==实扣」:'
                '把声明改成一个 handler 从不扣的码 ⇒ 一致性锁必须红。'),
        'file': 'services/organization_route_contract.py',
        'old': '    _p("POST", "/api/meijiehezi/publish", "publish.execute", "publish_order", "create", billing="media_proxy_publish"),\n',
        'new': '    _p("POST", "/api/meijiehezi/publish", "publish.execute", "publish_order", "create", billing="quote_generate"),  # MUTANT\n',
        'expect_red': ['test_the_declared_billing_matches_what_the_handler_charges'],
    },
    {
        'id': 'M13-把冻结理由改回写死空数组',
        'why': '改回 reason_facts=[] ⇒ recommendation_reasons 恒空 ⇒ frozen_reasons 回到死函数。',
        'file': 'api/xiaobang_operations_api.py',
        'old': '                reason_facts=_frozen_reason_facts(cursor, actor, manifest),\n',
        'new': '                reason_facts=[],  # MUTANT: 回到死函数\n',
        'expect_red': ['test_the_frozen_reason_actually_reaches_the_wire'],
    },
    {
        'id': 'M14-把 GEO-only 闸从索引链摘掉',
        'why': '摘掉它 ⇒ 社媒条目能进小榜知识库(Owner 2026-08-18 铁律 XIAOBANG-GEO-ONLY)。',
        'file': 'tools/xiaobang_system_kb.py',
        'old': '            _assert_kb_page_is_geo_only(fp, card)\n',
        'new': '            pass  # MUTANT: 闸被摘掉\n',
        'expect_red': ['test_the_kb_index_chain_really_calls_the_geo_only_gate'],
    },
    {
        'id': 'M15-把「新页必须进索引」那道闸摘掉',
        'why': '摘掉它 ⇒ 页写了不进索引也没人发现(本仓 34 班记过的假绿形态)。',
        'file': 'tools/xiaobang_system_kb.py',
        'old': '        assert_declared_pages_are_indexed([pages_glob])\n',
        'new': '        pass  # MUTANT: 闸被摘掉\n',
        'expect_red': ['test_a_declared_page_outside_the_index_glob_aborts_the_rebuild'],
    },
    # ── R2 · 存量 P0 修复(Owner 2026-08-24 批「修」)────────────────────────
    {
        'id': 'M16-把 extra_cost 摘回去(复现生产 500 形态)',
        'why': ('这一发就是**修之前的生产代码**。目录价 = 0(动态定价 SKU)⇒ '
                'total_cost = 0 + 0 = 0 ⇒ billing 在开事务前 return free/零句柄 ⇒ '
                'FreezeProducedNoHandle ⇒ SETTLEMENT_HANDLE_INVALID 500。'
                '判据必须复现出这个形态,否则「我修好了」是一句没有被证伪过的话。'),
        'file': 'services/geo_douyin/publish_batch_core.py',
        'old': '                brand_id=None, expected_points=amount,\n                extra_cost=int(amount),\n',
        'new': '                brand_id=None, expected_points=amount,  # MUTANT: extra_cost 摘回\n',
        'expect_red': ['test_execute_freezes_the_media_price_under_the_production_catalog_shape'],
    },
    {
        'id': 'M17-让冻结额 != 确认屏展示额',
        'why': ('Owner 的落点是「冻的钱 == 她看到并据此按下确认的那个数」。'
                '把**上屏投影**的数字翻倍(冻结侧不动)⇒ 展示 2X、实冻 X。'
                'freeze_one_item 内部那道 amount != expected_points 核对**看不见**'
                '这件事(它比的是冻结额与权威价,两边都还是 X)—— '
                '所以这一发只能被「拿屏上真的出现过的数去比冻结增量」那条判据抓到。'),
        'file': 'services/defensive_geo/xiaobang/compute_estimate.py',
        'old': '        out["amount"] = amount\n',
        'new': '        out["amount"] = amount * 2  # MUTANT: 上屏数字与实冻不同源\n',
        'expect_red': ['test_execute_freezes_the_media_price_under_the_production_catalog_shape'],
    },
]


def _read(p: Path) -> str:
    with io.open(p, "r", encoding="utf-8", newline="") as f:
        return f.read()


def _write(p: Path, s: str) -> None:
    with io.open(p, "w", encoding="utf-8", newline="") as f:
        f.write(s)


def _run_suite() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", SUITE, "-q", "-p", "no:cacheprovider"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    # 只跑点名的那几发(子串匹配 id)。不传参 = 全跑。
    # 🔴 加这个不是为了图快:复审要求「这一发到底是被哪几条判据抓到的」必须贴
    #    实测 FAILED 行,而不是我推测。逐发单跑是唯一能贴出那份清单的办法。
    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    results: list[tuple[str, str, str]] = []
    for mut in MUTATIONS:
        if only and not any(k in mut["id"] for k in only):
            continue
        path = ROOT / mut["file"]
        original = _read(path)
        if mut["old"] not in original:
            results.append((mut["id"], "ANCHOR-MISS", "锚点找不到 —— 这一发没打出去"))
            continue
        n = original.count(mut["old"])
        if n != 1:
            results.append((mut["id"], "ANCHOR-AMBIGUOUS", f"锚点命中 {n} 处"))
            continue
        try:
            _write(path, original.replace(mut["old"], mut["new"], 1))
            rc, out = _run_suite()
            if rc == 0:
                results.append((mut["id"], "SURVIVED", "判据全绿 —— 这一行没有任何判据在守"))
            else:
                missing = [n for n in mut["expect_red"] if n not in out]
                if missing:
                    tail = [l for l in out.splitlines() if l.startswith("FAILED")][:4]
                    results.append((mut["id"], "RED-BUT-WRONG-NODE",
                                    f"红了但点名的判据没红:{missing};实红={tail}"))
                else:
                    # 🔴 把**实红节点全集**打出来。只报「杀了」会让人以为是我
                    #    点名那条判据独力抓到的 —— 而两把锁叠在同一路径上时,
                    #    「相关判据全绿」证明不了那一行被验过(本仓记过)。
                    #    谁抓到的、有没有别人顺手抓到,必须看得见。
                    for line in out.splitlines():
                        if line.startswith("FAILED"):
                            print("      红:" + line[7:].split(" - ")[0])
                    results.append((mut["id"], "KILLED",
                                    out.strip().splitlines()[-1] if out.strip() else ""))
        finally:
            _write(path, original)
            assert _read(path) == original, f"{mut['file']} 还原失败"

    print("\n" + "=" * 78)
    bad = 0
    for mid, verdict, note in results:
        flag = "OK " if verdict == "KILLED" else "!! "
        if verdict != "KILLED":
            bad += 1
        print(f"{flag}{verdict:<18} {mid}\n      {note[:160]}")
    print("=" * 78)
    print(f"KILLED {len(results) - bad}/{len(results)}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
