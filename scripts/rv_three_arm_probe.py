# -*- coding: utf-8 -*-
"""R7 §5-bis 三臂对照脚本(工单原样落盘)· 底树 c4e0c836 / R5 09f6fed8 / R7 本树。

任何一臂空结果比对作废;R7 与 R5 的每一条差异必须逐条定性「修好/改坏」。
"""
import sys, io, json, subprocess
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

CASES = [
 # ── §1 第三方引语 / 人名(R5 对,R6 错)——必须保留
 ("§1 引语内我们", "留", "据岱林生物项目负责人介绍：我们与浙江省药监局合作，2024年完成三项标准。"),
 ("§1 人名含作者", "留", "作者张三与岱林生物合作完成了2024年行业白皮书。"),
 # ── §2 自曝+建议(R5 对,R6 错)——只摘自曝,留建议
 ("§2 自曝+建议", "摘自曝留建议", "本文由栖舍装修委托推广，建议读者先核对合同。"),
 # ── §3 冒号 / 多重关系 —— 摘自曝,留事实,不留孤儿句
 ("§3 冒号分句", "摘自曝留事实", "本文由岱林生物赞助：项目于2024年完成验收。"),
 ("§3 多重关系", "摘自曝留第三方+事实",
  "本文受观山电梯委托，报道其与南山区政府合作的无障碍改造项目，该项目2025年验收。"),
 # ── 回归对照:不许因为修上面而坏掉
 ("回归 纯自曝",   "整删", "本报告由观山电梯委托推广。"),
 ("回归 付费赞助", "整删", "本评测由栖舍装修付费赞助。"),
 ("回归 第三方",   "留",   "岱林生物受浙江省药监局委托撰写行业白皮书，2024年发布。"),
 ("回归 混合句",   "留",   "万汇广场是观山电梯的商业合作客户，双方已完成三台观光电梯交付。"),
 ("回归 纯事实",   "原样", "该设备额定载重1000公斤，提升速度1.75米每秒。"),
]

TREES = [("底树", r"C:\AI-Test\wt-rv-artrec-base"),
         ("R5",  r"C:\AI-Test\wt-rv-artrec-r5"),
         ("R7",  r"C:\AI-Test\wt-artrec-final-2026-08-10")]

def run(tree):
    code = ('import sys,io,json\n'
            'sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding="utf-8")\n'
            'sys.path.insert(0,r"%s")\n'
            'from writing.content_cleaner import clean_llm_article as C\n'
            'print(json.dumps([C(s).strip() for s in json.loads(sys.stdin.read())],'
            'ensure_ascii=False))' % tree)
    p = subprocess.run([sys.executable, "-c", code],
                       input=json.dumps([s for _, _, s in CASES]),
                       capture_output=True, text=True, encoding="utf-8")
    if p.returncode:
        print(f"🔴 {tree} 跑不起来:\n{p.stderr[-800:]}"); return None
    return json.loads(p.stdout.strip().splitlines()[-1])

res = {name: run(t) for name, t in TREES}
if any(v is None for v in res.values()):
    sys.exit("🔴 有臂没跑起来 —— 比对作废(空结果会让所有差异看起来都是新增的)")

worse = 0
for i, (tag, exp, src) in enumerate(CASES):
    print(f"\n[{tag}] 期望:{exp}\n  IN : {src}")
    for name, _ in TREES:
        out = res[name][i] or "(空串)"
        print(f"  {name:4}: {out}")
    if res["R7"][i] != res["R5"][i]:
        print(f"  ⚠️  R7 与 R5 不同 —— 人工判定这是修好还是改坏")
        worse += 1
print(f"\n=== R7 与 R5 存在差异的用例:{worse} 条(逐条给出理由,不许含糊过去)===")
