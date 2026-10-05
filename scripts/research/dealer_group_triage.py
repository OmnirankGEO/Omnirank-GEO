#!/usr/bin/env python3
"""§4 · dealer_inventory_resale 组红/skip 逐条定性(WO-LATENT-TRAPS 2026-08-17)

背景:该组历史上**从没进过 A/B 分母** —— `conftest.py` 在 import 阶段就 `raise RuntimeError`
(要专用高位端口库 + 库名恰为 dealer_resale_test + ALLOW_DESTRUCTIVE_TEST_DB=1),
中止在 collection 之前,**junit 一个字节都不写**。
于是它在两臂同时消失,`NEW_RED=0` 照常输出,没有任何一处提示分母少了一块。
两臂对称所以没人被假绿骗过,但洞是真的。

本脚本读 junit,把每条 failure / error / skipped 拉出来,附上分类所需的原始信息
(节点名、异常类型、断言首行),供人工定性成:
  坏护栏(判据本身写错 / 恒真恒红)· 坏夹具(测试环境造数不对)· 真缺陷(生产代码有 bug)

用法:python scripts/research/dealer_group_triage.py <junit.xml>
"""
from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter


def main() -> int:
    if len(sys.argv) < 2:
        return int(bool(sys.stderr.write("用法: dealer_group_triage.py <junit.xml>\n")))
    root = ET.parse(sys.argv[1]).getroot()
    suite = root if root.tag == "testsuite" else root.find("testsuite")

    total = int(suite.get("tests") or 0)
    print("=== junit 分母自证(先证跑起来了,再谈过没过)===")
    print("  tests=%s failures=%s errors=%s skipped=%s time=%ss"
          % (suite.get("tests"), suite.get("failures"), suite.get("errors"),
             suite.get("skipped"), suite.get("time")))
    if total == 0:
        print("  🔴 分母为 0 —— 这组根本没跑起来(门禁 raise 在 collection 之前),不算跑过")
        return 1

    buckets = {"failure": [], "error": [], "skipped": []}
    for tc in suite.iter("testcase"):
        node = "%s::%s" % (tc.get("classname", "").split(".")[-1], tc.get("name"))
        for kind in buckets:
            el = tc.find(kind)
            if el is not None:
                msg = (el.get("message") or "").strip()
                body = (el.text or "").strip()
                buckets[kind].append((node, el.get("type") or "", msg, body))

    for kind in ("failure", "error", "skipped"):
        rows = buckets[kind]
        print()
        print("=== %s · %d 条 ===" % (kind.upper(), len(rows)))
        for node, typ, msg, body in rows:
            print("--- %s" % node)
            if typ:
                print("    type: %s" % typ)
            first = ""
            for line in (msg + "\n" + body).split("\n"):
                s = line.strip()
                if s.startswith(("E ", "assert", "Assertion", "psycopg2", "RuntimeError",
                                 "KeyError", "TypeError", "ValueError")) or "Error" in s:
                    first = s
                    break
            print("    首个错误行: %s" % (first or (msg.split("\n")[0] if msg else "(空)"))[:170])
            sig = re.sub(r"\d+", "N", first)[:90]
            print("    错误签名  : %s" % sig)

    print()
    print("=== 错误签名分布(两边都红时必须比签名,不能只比节点集合)===")
    sigs = Counter()
    for kind in ("failure", "error"):
        for _node, _t, msg, body in buckets[kind]:
            m = re.search(r"^E\s+(.*)$", msg + "\n" + body, re.M)
            sigs[re.sub(r"\d+", "N", (m.group(1) if m else msg).strip())[:70]] += 1
    for s, n in sigs.most_common():
        print("  %2d× %s" % (n, s))
    return 0


if __name__ == "__main__":
    sys.exit(main())
