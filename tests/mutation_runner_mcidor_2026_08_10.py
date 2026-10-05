"""🔴 P0 IDOR 热修 · 变异自检(工单 §验收 2「删归属校验 → 越权锁必转红」)。

三关缺一即判无效变异:
  1. 锚点唯一命中;2. 落盘后文件真变了;3. 至少一条锁转红。

M1-M3 是工单点名那条的三个实例(三个端点各删一次);M4-M8 是我补的 ——
只杀"删调用"这一种,修法②③(映射顺序 / 中间件白名单)就完全没有守卫。

🔴 文件读写一律走 bytes(Windows 上 read_text/write_text 会翻换行)。
🔴 子进程钉 PYTHONPYCACHEPREFIX + 禁写字节码(残留 .pyc 会让下一轮基线假红)。

跑法:TEST_DATABASE_URL=... python tests/mutation_runner_mcidor_2026_08_10.py
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[1]

API = "api/marketing_confirm_api.py"
MAPPING = "auth/module_mapping.py"
MIDDLEWARE = "auth/middleware.py"

T_IDOR = "tests/mcidor_2026_08_10/test_marketing_confirm_idor_2026_08_10.py"
T_META = "tests/mcidor_2026_08_10/test_mapping_and_middleware_2026_08_10.py"

_PYC_DIR = REPO / ".mutation_pycache_mcidor"

MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # ── 工单点名:删归属校验(三个端点各一次)────────────────────────────
    ("M1", "删 status 的归属校验 → 退回事故现场(u114 读 662 拿真 token)",
     API,
     '    # [P0 IDOR 热修] 这就是被生产实测打穿的那个端点(u114 → status/662 → 200 + 真 token)。\n'
     '    _require_brand_owner(request, brand_id)\n',
     ""),

    # 🔴 锚点必须带上 resend 专属那行注释:status 的 `_require_brand_owner + _ensure_table`
    #    与它逐字同形,只用后者会命中 2 次 → SKIP(2026-08-10 实测踩过)。
    ("M2", "删 resend 的归属校验 → 可越权重置别人家客户的确认反馈(写)",
     API,
     '    #   越权后果比 status 更重 —— 能把别人家客户已提交的确认反馈直接抹掉。\n'
     '    _require_brand_owner(request, brand_id)\n',
     '    #   越权后果比 status 更重 —— 能把别人家客户已提交的确认反馈直接抹掉。\n'),

    ("M3", "删 generate-link 的归属校验 → 可越权给别人家品牌签发新 token(body 向量,中间件不覆盖)",
     API,
     '    _require_brand_owner(request, req.brand_id)\n    _ensure_table()\n',
     '    _ensure_table()\n'),

    # ── 闸被弱化 / 被挪位(不是删)────────────────────────────────────────
    # 🔴 [分诊留档] 原 M4(把 allow_null 改成 True)实跑 **SURVIVED**,分诊 = **空操作变异**:
    #   这三个端点的 brand_id 是 `int` 路径参数 / pydantic `int` 字段,**永远不可能是 None**
    #   (非法值 FastAPI 在进 handler 之前就 422),所以 allow_null 分支不可达,
    #   两个取值可观察行为完全一样。写 allow_null=False 是**显式声明意图**,不是活判据。
    #   换成打在真正有风险的地方:把闸挪到写操作**之后** —— 仍然返 404,但数据已经被改。
    #   这正是 test_cross_tenant_resend_is_404_and_does_not_touch_the_row 存在的理由。
    #   ⚠️ 一次 replace 只能改一处,所以这里用**跨越两处的大锚点**(从闸一直到 commit),
    #      整段重写成"闸在 commit 之后"。语法仍然合法,响应码仍然是 404 ——
    #      唯一的差别就是那一行库数据已经被改掉。
    ("M4", "resend 的闸挪到 conn.commit() 之后 → 返 404 但别人家的行已被改(先写后判)",
     API,
     '    _require_brand_owner(request, brand_id)\n    _ensure_table()\n\n    conn = get_connection()\n    try:\n        cursor = conn.cursor()\n\n        # 找到最新的 session（feedback 或 pending 状态）\n        cursor.execute("""\n            SELECT id, token FROM marketing_confirm_sessions\n            WHERE brand_id = %s AND status IN (\'feedback\', \'pending\')\n            ORDER BY created_at DESC LIMIT 1\n        """, (brand_id,))\n        row = cursor.fetchone()\n\n        if not row:\n            conn.close()\n            raise HTTPException(400, "没有待处理的确认会话")\n\n        # 重新构建快照（拿最新资料）\n        brand_info = _get_brand_info(brand_id)\n        profile = _get_profile_for_brand(brand_id)\n        materials = _get_materials_for_brand(brand_id)\n        snapshot = _build_materials_snapshot(brand_info, profile, materials)\n\n        expires_at = datetime.now() + timedelta(days=7)\n\n        cursor.execute("""\n            UPDATE marketing_confirm_sessions\n            SET status = \'pending\', materials_snapshot = %s, customer_notes = \'\',\n                customer_patch_json = \'\', expires_at = %s, updated_at = CURRENT_TIMESTAMP\n            WHERE id = %s\n        """, (json.dumps(snapshot, ensure_ascii=False), expires_at, row[\'id\']))\n        conn.commit()\n',
     '    _ensure_table()\n\n    conn = get_connection()\n    try:\n        cursor = conn.cursor()\n\n        # 找到最新的 session（feedback 或 pending 状态）\n        cursor.execute("""\n            SELECT id, token FROM marketing_confirm_sessions\n            WHERE brand_id = %s AND status IN (\'feedback\', \'pending\')\n            ORDER BY created_at DESC LIMIT 1\n        """, (brand_id,))\n        row = cursor.fetchone()\n\n        if not row:\n            conn.close()\n            raise HTTPException(400, "没有待处理的确认会话")\n\n        # 重新构建快照（拿最新资料）\n        brand_info = _get_brand_info(brand_id)\n        profile = _get_profile_for_brand(brand_id)\n        materials = _get_materials_for_brand(brand_id)\n        snapshot = _build_materials_snapshot(brand_info, profile, materials)\n\n        expires_at = datetime.now() + timedelta(days=7)\n\n        cursor.execute("""\n            UPDATE marketing_confirm_sessions\n            SET status = \'pending\', materials_snapshot = %s, customer_notes = \'\',\n                customer_patch_json = \'\', expires_at = %s, updated_at = CURRENT_TIMESTAMP\n            WHERE id = %s\n        """, (json.dumps(snapshot, ensure_ascii=False), expires_at, row[\'id\']))\n        conn.commit()\n        _require_brand_owner(request, brand_id)\n'),

    ("M5", "闸退化成「只要登录就行」(丢掉归属那一半)",
     API,
     "    from auth.brand_access import require_brand_access\n"
     "    require_brand_access(request, brand_id, allow_null=False)\n",
     "    if not getattr(request.state, 'user', None):\n"
     "        raise HTTPException(401, '未登录')\n"),

    # ── 修法②:映射顺序 ────────────────────────────────────────────────
    ("M6", "把 marketing-confirm 条目挪到 /api/marketing 之后 → 又被前缀吞掉",
     MAPPING,
     '    ("/api/marketing-confirm/", None),        # 营销资料确认(销售端 3 端点 · 端点内 require_brand_access 归属隔离)\n'
     '    ("/api/marketing-confirm",  None),        # 兼容无斜杠\n'
     '    ("/api/marketing/",     None),            # 营销物料工厂(模板/生成/任务/物料库/举报)\n'
     '    ("/api/marketing",      None),            # 兼容无斜杠\n',
     '    ("/api/marketing/",     None),            # 营销物料工厂(模板/生成/任务/物料库/举报)\n'
     '    ("/api/marketing",      None),            # 兼容无斜杠\n'
     '    ("/api/marketing-confirm/", None),        # 营销资料确认\n'
     '    ("/api/marketing-confirm",  None),        # 兼容无斜杠\n'),

    # ── 修法③:中间件白名单 ────────────────────────────────────────────
    ("M7", "白名单条目去掉尾斜杠 → 变成前缀吞并(与 /api/marketing 同型错误)",
     MIDDLEWARE,
     '    "/api/marketing-confirm/status/",\n    "/api/marketing-confirm/resend/",\n',
     '    "/api/marketing-confirm/status",\n    "/api/marketing-confirm/resend",\n'),

    ("M8", "白名单改成通配(任何尾段数字都当 brand_id)→ quote_id 被误读,合法请求被 403",
     MIDDLEWARE,
     "    for _prefix in _PATH_BRAND_ID_PREFIXES:\n"
     "        if path.startswith(_prefix):\n"
     "            _tail = path[len(_prefix):].strip(\"/\")\n"
     "            if _tail and \"/\" not in _tail:\n",
     "    for _prefix in (\"/api/\",):\n"
     "        if path.startswith(_prefix):\n"
     "            _tail = path.rstrip(\"/\").split(\"/\")[-1]\n"
     "            if _tail:\n"),

    # ── 修法 2b:日志打码 ──────────────────────────────────────────────
    ("M9", "撤回日志打码 → 明文 token 又写进日志(不需越权即可捡到有效凭据)",
     API,
     'logger.info(f"重新发送确认链接: brand_id={brand_id}, token={_mask_token(row[\'token\'])}")',
     'logger.info(f"重新发送确认链接: brand_id={brand_id}, token={row[\'token\']}")'),
]


def _clean_pyc() -> None:
    shutil.rmtree(_PYC_DIR, ignore_errors=True)


def _child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPYCACHEPREFIX"] = str(_PYC_DIR)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run_suite(targets: list[str]) -> bool:
    _clean_pyc()
    r = subprocess.run(
        [sys.executable, "-B", "-m", "pytest", *targets, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=REPO, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=1800, env=_child_env())
    return r.returncode == 0


def main() -> int:
    if not os.environ.get("TEST_DATABASE_URL"):
        print("⚠️  没有 TEST_DATABASE_URL:越权锁会 skip,而 skip 的锁杀不掉变异。")

    targets = [T_IDOR, T_META]
    print("→ 基线…")
    if not run_suite(targets):
        print("❌ 基线就红 —— 变异结果无意义,先修基线")
        return 2
    print("✅ 基线绿\n")

    results = []
    for mid, desc, rel, old, new in MUTATIONS:
        path = REPO / rel
        original = path.read_bytes()
        old_b, new_b = old.encode("utf-8"), new.encode("utf-8")
        hits = original.count(old_b)
        if hits != 1:
            results.append((mid, f"SKIP(锚点命中 {hits} ≠ 1)"))
            print(f"⚠️  {mid} SKIP:锚点命中 {hits} 次 · {desc}")
            continue
        mutated = original.replace(old_b, new_b)
        assert mutated != original
        try:
            path.write_bytes(mutated)
            green = run_suite(targets)
        finally:
            path.write_bytes(original)
        if path.read_bytes() != original:
            print(f"🔴 {mid} 还原失败!{rel} 与原文不一致,人工介入")
            return 3
        if green:
            results.append((mid, "SURVIVED"))
            print(f"❌ {mid} SURVIVED(变异后仍绿,锁抓不到):{desc}")
        else:
            results.append((mid, "KILLED"))
            print(f"✅ {mid} KILLED:{desc}")

    killed = sum(1 for _, s in results if s == "KILLED")
    survived = [m for m, s in results if s == "SURVIVED"]
    skipped = [m for m, s in results if s.startswith("SKIP")]
    print(f"\n==== 变异结果:{killed} killed / {len(survived)} survived / {len(skipped)} skip ====")
    if survived:
        print("SURVIVED:", ", ".join(survived))
        return 1
    if skipped:
        print("SKIP(锚点问题,不算通过):", ", ".join(skipped))
        return 1
    _clean_pyc()
    print("✅ 全部变异被击杀,锁有判别力")
    return 0


if __name__ == "__main__":
    sys.exit(main())
