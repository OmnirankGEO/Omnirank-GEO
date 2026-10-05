# -*- coding: utf-8 -*-
"""前端入口变异 runner · 2026-08-06 二次返工 ①

判据来源是 **Playwright 在网络层抓到的 POST body**,不是源码字符串扫描 ——
返工单明确要求"以后不许再用直调后端函数冒充产品端到端",同理也不能用
"源码里有这一行"冒充"请求体里真的带上了"。

用法:
    python scripts/research/mutation_runner_geovid_ranking_frontend_2026_08_06.py
    python scripts/research/mutation_runner_geovid_ranking_frontend_2026_08_06.py --selftest

🔴 与后端 runner 同样三条纪律:先证锚点唯一命中 / 字节级读写保持行尾 /
   每轮跑完字节级还原并校验。
"""
from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
FE = ROOT / "frontend"
# [WO_271 · 2026-09-23] 已失效:变异目标 DouyinImagePost.tsx 于 09-08 删除(6b491ab23),本脚本不可再跑;
#   它打的那几把前端锁已改指现役文件(见各锁的 WO_271 注与 tests/RETIRED_TESTS.txt)。留作研究记录。
TSX = FE / "src" / "pages" / "Writing" / "DouyinImagePost.tsx"
CONFIG = "playwright.geo-douyin-ranking.config.ts"
DETAIL = FE / "src" / "pages" / "Writing" / "DouyinPostDetail.tsx"


@dataclass
class Mutant:
    name: str
    path: pathlib.Path
    old: bytes
    new: bytes


M: list[Mutant] = [
    Mutant("创建请求不带 content_form(退回「后端通了、产品不通」)", TSX,
           b"                    content_form: contentForm,\n", b""),
    Mutant("content_form 写死空串(点了榜单也发卡组)", TSX,
           b"                    content_form: contentForm,",
           b"                    content_form: '',"),
    Mutant("家数恒 0(选了没反应)", TSX,
           b"                    ranking_entity_count:\n"
           b"                        contentForm === CONTENT_FORM_RANKING ? rankingCount : 0,",
           b"                    ranking_entity_count: 0,"),
    Mutant("手动选版式不置 ranking_force(覆盖失效)", TSX,
           b"                    ranking_force:\n"
           b"                        contentForm === CONTENT_FORM_RANKING && rankingTemplate !== '',",
           b"                    ranking_force: false,"),
    Mutant("切回卡组后榜单字段仍残留", TSX,
           b"                    ranking_template:\n"
           b"                        contentForm === CONTENT_FORM_RANKING ? rankingTemplate : '',",
           b"                    ranking_template: rankingTemplate,"),
    Mutant("降级告知整块不渲染(静默换货复活)", TSX,
           b"                                            if (!nt || noticeDismissed[p.id]) return null;",
           b"                                            if (true) return null;"),
    Mutant("「就用这版」点了不关(假出口)", TSX,
           b"onClick={() => setNoticeDismissed(\n"
           b"                                                                                s => ({ ...s, [p.id]: true }))}>",
           b"onClick={() => undefined}>"),
    Mutant("A1 闸提示不渲染(闸输出又没消费方)", TSX,
           b"                                        {(p.generation_meta?.ranking_gates || [])",
           b"                                        {([] as RankingGateFinding[])"),
    Mutant("家数按钮不再受张数预算约束(付费后才发现装不下)", TSX,
           b"                                                disabled={n > entitySlots}\n", b""),
    # 🔴 第一版这条变异只改了句子**前半段**,而判据盯的是后半段的可执行建议 ——
    #    于是它存活其实是变异体没打到判据要保护的东西。变异体也要对着判据设计。
    Mutant("张数不够时不说怎么办(只说不行)", TSX,
           "（封面和收尾各占一张）—— 把张数加到 ${rankingCount + 2} 张才能做 ${rankingCount} 家".encode(),
           "".encode()),
    Mutant("再次创作把榜单参数在前端重拼(而不是服务端继承)", DETAIL,
           b"                    ...(regenTemplate !== null ? { ranking_template: regenTemplate } : {}),",
           b"                    ranking_template: regenTemplate ?? '',"),
    Mutant("弹窗不显示继承来的榜单设置(用户以为会变普通图文)", DETAIL,
           b"                        {detail?.ranking?.content_form === 'ranking' && (",
           b"                        {false && ("),
    Mutant("母版下拉默认值不取快照(退回空)", DETAIL,
           b"                                    value={regenTemplate ?? detail.ranking.template}",
           b"                                    value={regenTemplate ?? ''}"),
]



def _run() -> int:
    """跑一整套 Playwright,返回失败用例数。"""
    r = subprocess.run(
        ["npx", "playwright", "test", "--config", CONFIG, "--reporter=line"],
        cwd=str(FE), capture_output=True, text=True,
        encoding="utf-8", errors="replace", shell=True)
    out = r.stdout + r.stderr
    m = re.search(r"(\d+) failed", out)
    if m:
        return int(m.group(1))
    # 一条没跑起来(编译错/服务起不来)也算失败,但要能区分出来
    if "passed" not in out:
        print(out[-1500:])
        return -1
    return 0


def selftest() -> int:
    bad = 0
    for mu in M:
        n = mu.path.read_bytes().count(mu.old)
        if n != 1:
            print(f"  x 锚点命中 {n} 次(应为 1):{mu.name}")
            bad += 1
    print(f"锚点自检:{len(M) - bad}/{len(M)} 唯一命中")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if selftest():
        print("锚点自检未过,拒绝跑变异(判据不可信)")
        return 1

    base = _run()
    if base != 0:
        print(f"基线红(failed={base})—— 先修基线")
        return 1
    print("基线全绿\n")

    killed = survived = 0
    for mu in M:
        orig = mu.path.read_bytes()
        mu.path.write_bytes(orig.replace(mu.old, mu.new, 1))
        try:
            f = _run()
        finally:
            mu.path.write_bytes(orig)
            assert mu.path.read_bytes() == orig, f"源码未还原:{mu.path}"
        if f != 0:
            print(f"  OK 杀死 ({f:>2} failed)  {mu.name}")
            killed += 1
        else:
            print(f"  XX 存活          {mu.name}   <- 锁没判别力")
            survived += 1
    print(f"\n前端变异 {killed}/{len(M)} 杀死,{survived} 存活")
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())
