# -*- coding: utf-8 -*-
"""WO_220-c1 注毒台:证明这 11 条判据**有牙**。"""
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
PKG = "tests/vision_deepseek_flash_2026_09_15"
EXPECTED_N = 22

ROUTING = "config/vision_routing.py"
DESCRIBE = "tools/vision/image_describe.py"
OCR = "services/geo_douyin/ocr_qa.py"

#: (名字, 文件, 锚, 换成什么, 说明, 期望命中数)
POISONS = [
    ("NC 无害注释", ROUTING,
     'logger = logging.getLogger("GEO-VisionRouting")',
     'logger = logging.getLogger("GEO-VisionRouting")  # 无害注释',
     "负样本 —— 必须读绿", 1),

    ("N1 不关思考", ROUTING,
     'DEEPSEEK_NO_THINKING: Dict[str, Any] = {"thinking": {"type": "disabled"}}',
     'DEEPSEEK_NO_THINKING: Dict[str, Any] = {}',
     "复杂图 200 但 content 为空 ⇒ 线上静默落 _fallback,表现为「识别失败」", 1),

    ("N2 DeepSeek 线走回百炼端点", ROUTING,
     '        return VisionTarget(model=model, endpoint=DEEPSEEK_ENDPOINT,',
     '        return VisionTarget(model=model, endpoint=DASHSCOPE_ENDPOINT,',
     "模型换了端点没换 —— 四样分开写时最常漏的那一样", 1),

    ("N3 platform 记成 dashscope", ROUTING,
     '                            api_key_env="DEEPSEEK_API_KEY", platform="deepseek",',
     '                            api_key_env="DEEPSEEK_API_KEY", platform="dashscope",',
     "成本记到另一家名下(WO_214 同族)", 1),

    ("N4 撤掉 pro→flash 的能力守卫", ROUTING,
     "    if model in DEEPSEEK_OFFICIAL_EMITTABLE and model not in DEEPSEEK_VISION_CAPABLE:",
     "    if False:",
     "配成 v4-pro 会返 200 但答「无法查看该图片」—— 会返 200 的错答案", 1),

    ("N5 image_describe 摘掉回显锁", DESCRIBE,
     '        if echoed and echoed != model:\n            return _fallback(original_filename, f"回显模型不符:{echoed} != {model}")\n',
     "",
     "供应商静默换模型 ⇒ 错的资产标注被当成功写进库", 1),

    ("N6 ocr_qa 自己拼端点(绕过解析器)", OCR,
     "                target.endpoint,",
     '                "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",',
     "两个消费方只切一个等于没切;单点解析器形同虚设", 1),

    #: 🔴 这一发是**真厂商实证**补出来的,不是我想出来的:
    #:   13 条桩判据全绿时,真实路径返回的是 qwen3.6-flash/dashscope,
    #:   因为共享键被 ensure_schema 播种进库,模块兜底永远轮不到。
    ("N8 解析器读回共享键(被播种成 qwen)", ROUTING,
     '        raw = get_admin_setting(_ADMIN_SETTING_KEY, str, DEFAULT_VISION_MODEL)',
     '        raw = get_admin_setting(_SHARED_SOCIAL_SETTING_KEY, str, DEFAULT_VISION_MODEL)',
     "读共享键 = 代码默认改了也不生效,而判据若都桩掉模型解析就看不见", 1),

    #: ══════════════════════════════════════════════════════════════
    #: 🔴 [c1prime] 以下 6 发来自 **Review 的 10 发**,在我这儿全部存活过。
    #:   它们不是「毒够不着」,是我一个也没钉过。根因:**作者的毒来自作者的判据** ——
    #:   我上一轮写的 8 发全红,因为每一发都打在我已经钉过的面上。
    #:   注毒台是作者写的,它反映「我锁了什么」,不是「被测对象有几个面」。
    #:   ⇒ 列毒的清单要从**抬头承诺的每一样**来(端点/key/platform/thinking),
    #:      不能把已有判据倒过来当清单。
    #: ══════════════════════════════════════════════════════════════
    ("N9 [P2] DeepSeek 档 key 取百炼的", ROUTING,
     '                            api_key_env="DEEPSEEK_API_KEY", platform="deepseek",',
     '                            api_key_env="DASHSCOPE_API_KEY", platform="deepseek",',
     "拿百炼 key 打 DeepSeek ⇒ 401 ⇒ 每张图「识别失败」,零报错", 1),

    ("N10 [P3] 消费方写死百炼 key", DESCRIBE,
     "    api_key = os.environ.get(target.api_key_env)",
     '    api_key = os.environ.get("DASHSCOPE_API_KEY")',
     "同上,但发生在消费方那一层 —— 解析器对不对都没用", 1),

    ("N11 [P4] ocr_qa 摘掉回显锁", OCR,
     "        if _echoed and _echoed != model:",
     "        if False:",
     "代码有锁判据没钉 —— 核验器被换掉却照常出报告", 1),

    ("N12 [P8] 解析器去掉旧名归一", ROUTING,
     "    model = normalize_deepseek_model(raw) or DEFAULT_VISION_MODEL",
     "    model = raw or DEFAULT_VISION_MODEL",
     "旧名原样发→官方回显 flash→我的回显锁 100% 全拒、零报错", 1),

    ("N13 [P10] 回显不符仍记成功", DESCRIBE,
     "                                       success=False," + chr(10) +
     '                                       error_msg=f"回显模型 {echoed} != 请求 {model}")',
     "                                       success=True," + chr(10) +
     '                                       error_msg=f"回显模型 {echoed} != 请求 {model}")',
     "成本表里看不出供应商静默换模型,而 WO_215 心跳判据靠这个字段", 1),

    ("N14 [P11] 解析器无视后台行", ROUTING,
     "        raw = get_admin_setting(_ADMIN_SETTING_KEY, str, DEFAULT_VISION_MODEL)",
     "        raw = None",
     "恒返默认 ⇒ 后台改了不生效,而「共享键是 qwen 仍得 flash」那条照样绿", 1),

    ("N7 管理员配 Qwen 也被发去 DeepSeek", ROUTING,
     "    if model in DEEPSEEK_OFFICIAL_EMITTABLE:",
     "    if True:",
     "qwen 的名字发给 DeepSeek ⇒ 400 → 静默 fallback,用户看到「识别失败」", 1),
]


def sha(rel):
    return hashlib.sha256(io.open(os.path.join(ROOT, rel), "rb").read()).hexdigest()[:12]


def run():
    env = dict(os.environ)
    env.setdefault("TEST_DATABASE_URL",
                   "postgresql://geo_admin:testpw@127.0.0.1:55492/geo_c14_222_test")
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
        print("🔴 基线不对(条数 %d,红 %s)—— 后面全部作废" % (base_c, sorted(base_f)))
        print(base_out[-1600:])
        return 2

    results = []
    for name, rel, anchor, repl, why, want in POISONS:
        full = os.path.join(ROOT, rel)
        raw = io.open(full, "rb").read()
        src = raw.decode("utf-8")
        hits = src.count(anchor)
        before = sha(rel)
        if hits != want:
            print("  %-36s 🔴 锚命中 %d 次(要 %d)—— 毒没下成,不判" % (name, hits, want))
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
        print("  %-36s %-8s 新红 %d 条  %s"
              % (name, verdict, len(new), ",".join(sorted(new))[:52]))
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
