"""变异验证 —— 把本批每处修复改回老形态，确认对应的锁**真的转红**。

用法：`python tests/publish_center_scope_2026_07_30/run_mutations.py`

🔴 还原方式：脚本把改动前的文件字节存在内存里，跑完逐字节写回
   (`read_bytes` / `write_bytes`，**不用** `Path.write_text` —— 那会把 LF 变成 CRLF)。
   不用 git checkout / stash 还原：那会连带冲掉工作区里其它未提交改动。

🔴 EXPECTED_SURVIVORS：期望**存活**（不转红）的变异必须显式登记，并写明理由。
   不许把「N 转红 + M 存活」合并写成「变异全转红」。

变异 ①-⑧ 对应工单 §4.2 原表；⑨⑩ 是我自加的两条，用来证明**接线门禁**
(verify-publish-center-scope-ui.mjs) 有判别力 —— 行为锁够不着接线层，
把 `selectAllUnpublishedTarget(scope)` 改回 `(articles)` 时逻辑模块仍然全绿，
只有接线门禁能抓。没有这两条，那个门禁等于没被验过。
"""
import subprocess
import sys
from pathlib import Path

try:  # Windows 控制台默认 GBK，打印 ✅/🔴 会 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"

L_LOCKS = ("locks", "scripts/test-publish-center-scope.mjs")
L_WIRING = ("wiring", "scripts/verify-publish-center-scope-ui.mjs")

#: 期望存活的变异 → 理由。空 dict = 全部期望转红。
EXPECTED_SURVIVORS: dict[str, str] = {}


class Mutation:
    def __init__(self, name, rel, old, new, expect_red, note="", must_include=(), must_not_include=()):
        self.name = name
        self.rel = rel
        self.old = old
        self.new = new
        self.expect_red = expect_red  # (标签, 脚本相对路径)
        self.note = note
        #: 🔴 判别力不能只写在 note 里 —— 这两项把它变成可执行断言:
        #: 变异后的失败集合**必须含**/**必须不含**哪条具体断言。
        #: ①② 的分水岭就靠它证明,否则"能区分改一半"只是一句自我声明。
        self.must_include = tuple(must_include)
        self.must_not_include = tuple(must_not_include)


F_LOGIC = "frontend/src/pages/Publishing/publishCenterScopeLogic.ts"
F_GROUP = "frontend/src/components/publishing/ArticleGroupSection.tsx"
F_NOTICE = "frontend/src/components/publishing/HiddenArticlesNotice.tsx"
F_PANEL = "frontend/src/components/publishing/StrictPresubmitPanel.tsx"
F_STRICT = "frontend/src/contracts/strictMediaPresubmit.ts"
F_PAGE = "frontend/src/pages/Publishing/PublishCenter.tsx"

MUTATIONS = [
    Mutation(
        "① 全选候选集改回 articles 全集(老 BUG 原形)",
        F_LOGIC,
        "  const list = scope.unpublished || [];",
        "  const list = scope.allArticles || [];",
        L_LOCKS,
        "锁1/锁2 都该红；锁2 里「购物车那篇不在结果里」这条**只有 ① 会红**，是与 ② 的分水岭",
        must_include=("锁2 购物车里那篇(501)不在全选结果里",),
    ),
    Mutation(
        "② 全选候选集改成 availableArticles(只去掉分组维度 · 改一半)",
        F_LOGIC,
        "  const list = scope.unpublished || [];",
        "  const list = scope.availableArticles || [];",
        L_LOCKS,
        "锁2 必须仍红 —— 证明锁能区分「改一半」，而不是只认「全都不改」",
        must_include=("锁2 已发布(201)不在全选结果里",),
        # 🔴 分水岭:② 没把购物车那道过滤去掉,所以这条**必须仍绿**。
        #    它红了说明 ①② 变异不等价,锁也就分不出「改一半」。
        must_not_include=("锁2 购物车里那篇(501)不在全选结果里",),
    ),
    Mutation(
        "③ 按钮文案改回「全选」(数字整个没了)",
        F_LOGIC,
        "    label: selectableCount === groupCount\n"
        "      ? `全选未分发 (${groupCount})`\n"
        "      : `全选未分发 (${selectableCount}/${groupCount})`,",
        "    label: '全选',",
        L_LOCKS,
        "锁3 —— 🔴 这条我第一版写成 `label: '全选' || selectableCount === groupCount`，"
        "`||` 优先级高于 `?:`，被解析成 `('全选'||A) ? B : C` 恒真，实际变成了 ⑫ 的重复。"
        "变异本身写错时，报出来的是「判别力不成立」而不是锁的问题",
        must_include=("锁3 按钮文案带数字", "锁3 按钮文案含「未分发」字样"),
    ),
    Mutation(
        "⑫ 按钮数字改回分组总数(复审订正掉的**旧错误口径** · 按钮说谎)",
        F_LOGIC,
        "      : `全选未分发 (${selectableCount}/${groupCount})`,",
        "      : `全选未分发 (${groupCount})`,",
        L_LOCKS,
        "锁9 —— 5 篇里 2 篇未过审时按钮显示 5 而只勾 3,正是本单要消灭的「看到的数≠选到的数」",
        must_include=(
            "锁3 按钮主数字 == 点下去真正被勾中的篇数",
            "锁9 文案把两个数都摆出来",
        ),
        # 三个数各自的断言**不该**因为文案变了就红 —— 它们是独立量
        must_not_include=(
            "锁9 按钮数字(点了会勾几篇) = 3",
            "锁9 实际勾选 = 3",
            "锁9 分组标签(这组有几篇) = 5",
        ),
    ),
    Mutation(
        "④ 空组恢复「空即不渲染」",
        F_GROUP,
        "  const empty = count === 0;",
        "  const empty = count === 0;\n  if (empty) return null;",
        L_LOCKS,
        "锁4",
    ),
    Mutation(
        "⑤ 隐藏提示改成恒显示",
        F_NOTICE,
        "  if (!hidden || hidden.total <= 0) return null;",
        "  if (false) return null;",
        L_LOCKS,
        "锁5 的**反向**那条(三条件全假时该行不出现)",
    ),
    Mutation(
        "⑥ 隐藏提示的数字写死成常量",
        F_NOTICE,
        "      data-hidden-total={hidden.total}",
        '      data-hidden-total={3}',
        L_LOCKS,
        "锁5 正向(数字必须等于被过滤掉的真实条数)",
    ),
    Mutation(
        "⑦ 拦截面板只渲染 message(丢掉 actions / repair_hint / 影响说明)",
        F_PANEL,
        "  const contract = toGovernanceContract(block, extraActions);",
        "  const contract = { code: 'STRICT', message: block.message,\n"
        "    reason: '', impact: '', actions: [] } as unknown as ReturnType<typeof toGovernanceContract>;",
        L_LOCKS,
        "锁6 —— 🔴 这条最初写成 `{false && <GovernanceAlert` ，JSX 不配对直接构建失败，"
        "「转红」是语法错不是锁抓到的(输出里连一行 ❌ 都没有)。改成合法代码后才是真信号",
        must_include=(
            "锁6 渲染出后端动作按钮 ai_fix_this_span",
            "锁6 渲染 repair_hint",
        ),
    ),
    Mutation(
        "⑧ 给面板加一个「忽略继续投放」按钮",
        F_STRICT,
        "  SWITCH_MEDIA_ACTION_ID,\n];",
        "  SWITCH_MEDIA_ACTION_ID,\n  'override_and_publish',\n];",
        L_LOCKS,
        "锁7(白名单被撑开 → 敌意 payload 里的放行按钮渲染出来)",
    ),
    # ── 自加两条：证明接线门禁有判别力 ────────────────────────────────
    Mutation(
        "⑨[自加] 接线改回 articles 全集(逻辑模块一字不改)",
        F_PAGE,
        "selectAllUnpublishedTarget(scope)",
        "selectAllUnpublishedTarget({ ...scope, unpublished: articles })",
        L_WIRING,
        "行为锁在这条上**仍然全绿** —— 只有接线门禁能抓，证明那道门不是摆设",
    ),
    Mutation(
        "⑩[自加] 把「已分发」分组重新包上 length>0 守卫",
        F_PAGE,
        '            <ArticleGroupSection\n                groupKey="published"',
        '            {publishedArticles.length > 0 && <ArticleGroupSection\n                groupKey="published"',
        L_WIRING,
        "同上：空即不渲染在页面层复活，行为锁看不见",
    ),
    Mutation(
        "⑬[自加] 把锁 9 整块跳过(验「0 skipped」是不是算出来的)",
        "frontend/scripts/test-publish-center-scope.mjs",
        "//    写成两两比对就退回旧错误了 —— 期望值是三个写死的常量,彼此无关。\n{",
        "//    写成两两比对就退回旧错误了 —— 期望值是三个写死的常量,彼此无关。\nif (false) {",
        L_LOCKS,
        "整块被跳过时**一条断言都不失败** —— 若 skipped 只是打印一句话而不参与判定，"
        "这条就会「全绿」通过。它红了才证明 0 skipped 是真判据",
        must_include=("以下锁一条断言都没跑",),
    ),
    Mutation(
        "⑪[自加] 严审拦截退回单条 toast(真·退回现状)",
        F_PAGE,
        "        const block = parseStrictPresubmitBlock(detail);",
        "        const block = null as ReturnType<typeof parseStrictPresubmitBlock>;",
        L_WIRING,
        "⑦ 只能证明面板内部渲染对不对；这条证明**页面还认不认那条载荷**",
        must_include=("接线5 提交失败分支里先解析预审载荷",),
    ),
]


def run(script_rel: str) -> tuple[bool, str]:
    proc = subprocess.run(
        ["node", script_rel],
        cwd=FRONTEND, capture_output=True, text=True, encoding="utf-8", errors="replace",
        shell=(sys.platform == "win32"),
    )
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def baseline() -> bool:
    allgreen = True
    for label, rel in (L_LOCKS, L_WIRING):
        green, _ = run(rel)
        print(f"  基线 {label}: {'绿' if green else '🔴 红'}")
        allgreen = allgreen and green
    return allgreen


def main() -> int:
    print("=== 基线(变异前) ===")
    if not baseline():
        print("🔴 基线就不是全绿，变异结论无意义，先修基线")
        return 2

    red, survived = 0, []
    for m in MUTATIONS:
        target = ROOT / m.rel
        original = target.read_bytes()
        text = original.decode("utf-8")
        if m.old not in text:
            print(f"\n🔴 {m.name}\n   锚点没命中，变异未生效(锚: {m.old[:60]!r})")
            survived.append(m.name + " [锚点失效]")
            continue
        mutated = text.replace(m.old, m.new, 1)
        target.write_bytes(mutated.encode("utf-8"))
        try:
            label, rel = m.expect_red
            green, out = run(rel)
            fails = [ln.strip() for ln in out.splitlines() if ln.startswith("❌")]
            if green:
                print(f"\n🟡 {m.name}\n   [{label}] 仍绿 = **存活**  ← {m.note}")
                survived.append(m.name)
            elif not fails:
                # 🔴 退出码非 0 但一行 ❌ 都没有 = 构建/运行炸了，不是锁抓到的。
                #    这种"红"是假信号，必须当失败处理。
                print(f"\n🔴 {m.name}\n   [{label}] 退出码非 0 但**无任何断言失败** = 假红(疑似语法/构建错)")
                print("   " + "\n   ".join(out.strip().splitlines()[:4]))
                survived.append(m.name + " [假红:无断言失败]")
            else:
                bad = [s for s in m.must_include if not any(s in f for f in fails)]
                bad += [f"(不该红却红了) {s}" for s in m.must_not_include if any(s in f for f in fails)]
                if bad:
                    print(f"\n🔴 {m.name}\n   [{label}] 转红了，但**判别力断言不成立**: {bad}")
                    survived.append(m.name + " [判别力不成立]")
                else:
                    red += 1
                    print(f"\n✅ {m.name}\n   [{label}] 转红({len(fails)} 条断言失败)  ← {m.note}")
                    print(f"   首条: {fails[0]}")
                    for s in m.must_include:
                        print(f"   判别力✓ 含: {s}")
                    for s in m.must_not_include:
                        print(f"   判别力✓ 不含: {s}")
        finally:
            target.write_bytes(original)
            assert target.read_bytes() == original, f"还原失败: {m.rel}"

    print("\n=== 还原后基线复验 ===")
    restored = baseline()

    print(f"\n统计: {red} 条转红 / {len(survived)} 条存活 / 共 {len(MUTATIONS)} 条")
    if survived:
        print("存活清单:")
        for s in survived:
            reason = EXPECTED_SURVIVORS.get(s)
            print(f"  - {s} … {'预期存活: ' + reason if reason else '🔴 非预期存活'}")
    if not restored:
        print("🔴 还原后基线不绿")
        return 2
    unexpected = [s for s in survived if s not in EXPECTED_SURVIVORS]
    return 1 if unexpected else 0


if __name__ == "__main__":
    raise SystemExit(main())
