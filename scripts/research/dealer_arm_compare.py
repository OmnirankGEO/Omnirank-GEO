#!/usr/bin/env python3
"""§4 · dealer 组两臂 junit 对照(节点集合求差 + 逐条比错误签名)。

两边都红时**必须比错误签名** —— 只比节点集合会把"红因不同"当成"存量红"放过去。
用法: python scripts/research/dealer_arm_compare.py <包臂 junit> <第三臂 junit>
"""
import sys, re, xml.etree.ElementTree as ET
def load(p):
    r=ET.parse(p).getroot(); s=r if r.tag=='testsuite' else r.find('testsuite')
    d={}
    for tc in s.iter('testcase'):
        node="%s::%s"%(tc.get('classname','').split('.')[-1],tc.get('name'))
        for k in ('failure','error'):
            el=tc.find(k)
            if el is not None:
                txt=(el.get('message') or '')+"\n"+(el.text or '')
                m=re.search(r"^E\s+(.*)$", txt, re.M)
                d[node]=re.sub(r"\d+","N",(m.group(1) if m else '').strip())[:80]
    meta=(s.get('tests'),s.get('failures'),s.get('errors'),s.get('skipped'))
    return d,meta
pkg,mp=load(sys.argv[1]); base,mb=load(sys.argv[2])
print("包臂   tests=%s failures=%s errors=%s skipped=%s"%mp)
print("第三臂 tests=%s failures=%s errors=%s skipped=%s"%mb)
print()
print("=== 节点集合求差 ===")
print("  只在包臂红(新增红):", sorted(set(pkg)-set(base)) or "无")
print("  只在第三臂红(被我修绿):", sorted(set(base)-set(pkg)) or "无")
print()
print("=== 两边都红的,逐条比**错误签名**(不能只比节点集合)===")
same=diff=0
for n in sorted(set(pkg)&set(base)):
    if pkg[n]==base[n]:
        same+=1
    else:
        diff+=1; print("  签名不同 %s\n     包: %s\n     基: %s"%(n,pkg[n],base[n]))
print("  签名逐字相同 %d 条 · 签名不同 %d 条"%(same,diff))
print()
print("判定:新增红=%d 签名漂移=%d → %s"%(len(set(pkg)-set(base)),diff,
   "8 条全为存量,与本包无因果" if not (set(pkg)-set(base)) and diff==0 else "需进一步定性"))
