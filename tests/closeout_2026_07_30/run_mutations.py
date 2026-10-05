"""变异验证 —— 把本批每处修复/每条判据改回老形态，确认对应的锁**真的转红**。

用法：`python tests/closeout_2026_07_30/run_mutations.py`

🔴 还原方式：脚本把改动前的文件字节存在内存里，跑完逐字节写回。
   **不用 git checkout / stash 还原** —— 那会连带冲掉工作区里其它未提交改动。
   跑完做还原校验：所有被动过的文件必须字节级复原且基线重新全绿。

🔴 EXPECTED_SURVIVORS：期望**存活**（不转红）的变异必须显式登记在这里，
   并写明"判别力由哪两处证明"。不许把「N 转红 + 1 声明存活」合并写成「变异全转红」。
"""
import subprocess
import sys
from pathlib import Path

try:  # Windows 控制台默认 GBK，打印 ✅/🔴 会 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[2]

L_RACE = "tests/test_selection_create_quote_race_2026_07_30.py"
L_PRIV = "tests/test_publish_channel_privacy.py"
L_SCHED = "tests/regression/test_fix2_scheduler.py"
L_SIGNAL = "tests/svideo_gate_2026_07_30/test_t1c_media_id_missing_signal.py"

#: 基线全绿范围（变异前后都要全绿）
BASELINE = [L_RACE, L_PRIV, L_SCHED, "tests/svideo_gate_2026_07_30"]

#: 期望存活的变异 → 理由 + 判别力由哪两处证明。空 dict = 全部期望转红。
EXPECTED_SURVIVORS: dict[str, str] = {}


class Mutation:
    def __init__(self, name, rel, old, new, expect_red, note=""):
        self.name = name
        self.rel = rel
        self.old = old
        self.new = new
        self.expect_red = expect_red   # 期望转红的选择器；None = 期望仍绿
        self.note = note


MUTATIONS = [
    # ── P1-A 选词并发 500 ───────────────────────────────────────────────
    Mutation(
        "MA1 db 层去掉 UniqueViolation 处理(退回裸 INSERT → 漏成 500)",
        "db/diagnosis_db.py",
        "        except UniqueViolation:",
        "        except SyntaxWarning:",   # 永不触发 → 等价于没有这个 handler
        L_RACE,
        "证明锁抓的是「唯一冲突被翻成幂等信号」这件事本身",
    ),
    Mutation(
        "MA2 db 层无脑吞掉唯一冲突(不再区分该 quote 有无 session)",
        "db/diagnosis_db.py",
        "            if not winner:\n                raise\n            raise ValueError",
        "            if False:\n                raise\n            raise ValueError",
        L_RACE,
        "证明锁不允许把 token 撞车之类的唯一冲突一起吞掉",
    ),
    Mutation(
        "MA3 端点层去掉幂等翻译分支(SESSION_EXISTS_FOR_QUOTE 不再被识别)",
        "api/selection_api.py",
        'if str(exc) == "SESSION_EXISTS_FOR_QUOTE":',
        "if False:",
        L_RACE,
        "证明端点侧那一半也在锁的覆盖范围内",
    ),
    Mutation(
        "MA4 端点层去掉赢家存在性校验(winner 为空也照编 200)",
        "api/selection_api.py",
        "            winner = get_session_by_quote(quote_id)\n            if not winner:\n                raise",
        "            winner = get_session_by_quote(quote_id)\n            if False:\n                raise",
        L_RACE,
        "证明锁不允许在查不到赢家时编造成功响应",
    ),

    # ── P1-B 供应商名口径 ───────────────────────────────────────────────
    Mutation(
        "MB1 关掉行注释剥离(`//` 段重新计入用户可见层)",
        L_PRIV,
        "            cut = idx\n            break",
        "            break",
        L_PRIV,
        "证明 PublishCenter.tsx:522/523 那两处 `//` 命中是靠行注释剥离才消失的",
    ),
    Mutation(
        "MB2 关掉块注释掩码(`/* */` 段重新计入)",
        L_PRIV,
        "        mask, in_block = _block_comment_mask(line, in_block)",
        "        mask, in_block = [], in_block",
        L_PRIV,
        "证明 :579/580 那两处 `/* */` 命中是靠块注释掩码才消失的",
    ),
    Mutation(
        "MB3 把口径改成恒绿(可见文本恒为空 —— 最危险的那种「修法」)",
        L_PRIV,
        "        if visible.strip():\n            kept.append((lineno, visible))",
        "        if False:\n            kept.append((lineno, visible))",
        L_PRIV,
        "证明「把红改成静默」会被判别力反证抓住,而不是变成一条恒绿测试",
    ),

    # ── P1-B CAN-031 口径 ──────────────────────────────────────────────
    Mutation(
        "MC1 拆掉持久化失败的耐久兜底(except 里不再结算 cell)",
        "api/scheduler.py",
        "        except Exception as exc:\n            finish_monitoring_cell_error(",
        "        except Exception as exc:\n            logger.warning(",
        L_SCHED,
        "证明 AST 判据 (1) 不是恒真:守卫拆掉即转红",
    ),
    Mutation(
        "MC2 去掉终态 failed 迁移(外层永不释放冻结)",
        "api/scheduler.py",
        'update_task_status(task_id, "failed")',
        "pass",
        L_SCHED,
        "证明 AST 判据 (2) 不是恒真",
    ),

    # ── P2-A media_id 缺失信号 ─────────────────────────────────────────
    Mutation(
        "MD1 去掉 media_id 缺失留痕(退回静默落 0)",
        "db/meijiehezi_db.py",
        "                if not resource_id:",
        "                if False:",
        L_SIGNAL,
        "证明锁抓的是「有信号」这件事",
    ),
    Mutation(
        "MD2 改成无条件留痕(恒打 = 等于没有信号)",
        "db/meijiehezi_db.py",
        "                if not resource_id:",
        "                if True:",
        L_SIGNAL,
        "证明锁不接受恒真信号",
    ),
    Mutation(
        "MD3 留痕里不再打远端键名(改名了还得再抓包)",
        "db/meijiehezi_db.py",
        "                        order_id, order_sn, sorted(str(k) for k in o.keys()),",
        '                        order_id, order_sn, "<omitted>",',
        L_SIGNAL,
        "证明「带远端键名全集」这条要求本身被锁住",
    ),
]


def run_pytest(selectors) -> bool:
    if isinstance(selectors, str):
        selectors = [selectors]
    r = subprocess.run(
        [sys.executable, "-m", "pytest", *selectors, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return r.returncode == 0


def apply(rel, old, new, backups):
    p = ROOT / rel
    data = p.read_bytes()
    backups.setdefault(rel, data)
    text = data.decode("utf-8")
    if text.count(old) != 1:
        raise SystemExit(
            f"❌ 变异锚点在 {rel} 出现 {text.count(old)} 次（应恰好 1 次）：{old[:60]!r}"
        )
    p.write_bytes(text.replace(old, new).encode("utf-8"))


def main():
    backups: dict = {}
    results = []
    try:
        if not run_pytest(BASELINE):
            raise SystemExit("❌ 基线未全绿，先修测试再跑变异")
        print("✅ 基线全绿")

        for m in MUTATIONS:
            local: dict = {}
            apply(m.rel, m.old, m.new, local)
            backups.setdefault(m.rel, local[m.rel])
            target = m.expect_red or BASELINE
            green = run_pytest(target)
            for rel, data in local.items():
                (ROOT / rel).write_bytes(data)

            if m.expect_red is None:
                ok = green
                verdict = "SURVIVED(期望存活)" if green else "🔴 转红(不该红)"
            else:
                ok = not green
                verdict = "✅ 转红" if not green else "🔴 SURVIVED(锁没抓住)"
            results.append((m.name, ok, verdict, m.note))
            print(f"{verdict:24s} {m.name}\n{'':26s}{m.note}")

    finally:
        for rel, data in backups.items():
            (ROOT / rel).write_bytes(data)
        print("\n— 还原校验 —")
        if not run_pytest(BASELINE):
            print("❌ 还原后基线未绿！工作区可能被破坏，请人工检查")
            sys.exit(2)
        print("✅ 还原后基线仍全绿")

    bad = [r for r in results if not r[1]]
    survivors = [r[0] for r in results if r[2].startswith("SURVIVED")]
    print(f"\n变异总计 {len(results)} 条，未达预期 {len(bad)} 条，"
          f"登记存活 {len(survivors)} 条")
    for name in survivors:
        print(f"  存活(已登记): {name} → {EXPECTED_SURVIVORS.get(name, '未登记!')}")
    for name, _, verdict, _n in bad:
        print(f"  - {name}: {verdict}")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
