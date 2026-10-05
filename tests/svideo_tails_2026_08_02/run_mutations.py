"""变异测试：逐条破坏本包修复的性质，确认锁真会红。

存活 = 那条锁是摆设。
用法：PYTHONIOENCODING=utf-8 TEST_DATABASE_URL=... python tests/svideo_tails_2026_08_02/run_mutations.py
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
API = ROOT / "api" / "meijiehezi_api.py"
DB = ROOT / "db" / "meijiehezi_db.py"
TESTS = "tests/svideo_tails_2026_08_02/test_upload_drain_and_media_name.py"

# (名字, 目标文件, 原文, 替换, 应由哪条锁拦下)
MUTATIONS = [
    (API, "M1 把扩展名早退改回裸 raise（502 复发）",
     'await _reject(400, "这个视频发不了，换个 MP4 或 FLV 格式的")',
     'raise HTTPException(status_code=400, detail="这个视频发不了，换个 MP4 或 FLV 格式的")',
     "drain 锁"),
    (API, "M2 drain 只是摆设（不真读流）",
     "async for _ in request.stream():\n                pass",
     "if False:\n                pass",
     "drain 必须真读流"),
    (API, "M3 drain 把主路径的流也吃掉",
     "chunks=request.stream(),",
     "chunks=(_ async for _ in []),",
     "反向锁：正常路径仍须流式"),
    # 🔴 2026-08-02 去重返工:M4/M5 的锚点原本对着【本包那份重复实现】的文案。
    #   保留生产已上线的 `_resolve_svideo_media_names` 后,那两个锚点必然失配 ——
    #   而锚点失配 = 这条变异什么都没测。runner 会当场报红(不静默跳过),已重新对准。
    (API, "M4 媒体名改回只信客户端",
     "_name_map.get(int(mid)) or _client_name",
     "_client_name",
     "服务端权威表取名"),
    (API, "M5 取名失败改成抛异常（阻断下单）",
     "        logger.warning(f\"[svideo] 取账号名失败(回退客户端值)",
     "        raise RuntimeError(f\"[svideo] 取账号名失败",
     "fail-soft 锁"),
    # 🔴 配对反向对照(复审 2026-08-02 点名要的):往 except 块里**额外插**一句 raise,
    #   但**保留** logger 那行 —— 与 M5(替换掉 logger 行)是两种写法。
    #   它证明 `test_media_name_lookup_is_fail_soft` 的断言真的落在 except 块内部,
    #   而不是因为切片切成空串/切反了而恒过。
    #   背景:本包原实现排在 `_recompute_publish_charge` **之前**,生产版排在其**之后**,
    #   老切片写死切到那个函数为止 → 换成生产版后起点在终点之后 → 切片失效。
    (API, "M8 except 里插 raise（保留 logger）— 证明锁打在 except 块内",
     "        logger.warning(f\"[svideo] 取账号名失败(回退客户端值)",
     "        raise RuntimeError(\"boom\")\n        logger.warning(f\"[svideo] 取账号名失败(回退客户端值)",
     "切片有效性 + fail-soft 锁"),
    (DB, "M6 回写改成无条件覆盖 media_name（冲掉正确名字）",
     "media_name = COALESCE(NULLIF(media_name, ''), %s)",
     "media_name = %s",
     "只补空值不覆盖"),
    (API, "M7 长度校验挪到读流之后（白吃带宽）",
     '_size = int(request.headers.get("content-length") or 0)',
     '_size = 1',
     "校验须在读体前"),
]


def run() -> bool:
    r = subprocess.run([sys.executable, "-m", "pytest", TESTS, "-q", "--no-header", "-x"],
                       cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode == 0


def _read_verbatim(p) -> str:
    """按字节原样读(不做行尾翻译)。

    🔴 **不能用 `Path.read_text(newline="")`** —— 那个参数是 **Python 3.13** 才加的,
    而部署目标是 **3.12.13**(生产实测 `inspect.signature(pathlib.Path.read_text)`
    = `(self, encoding=None, errors=None)`,传 newline 直接 TypeError)。
    `Path.write_text(newline=)` 倒是 3.12 就有 —— 同一个类两个方法参数不一致,
    肉眼极易看漏。

    2026-08-02 实测教训:我在本机 3.14 上跑出"8/8 全杀",而在 3.12 上这个 runner
    **在跑第一个变异之前就崩了,一个都没测** —— "本机跑通"和"目标运行时跑通"是两件事。
    同一份交付里另一个 runner 用的是 `open(..., newline="")`(全版本可用),
    它没出事不是因为判断对,只是写法不同。现在统一成这个写法。
    """
    with open(p, "r", encoding="utf-8", newline="") as f:
        return f.read()


def main() -> int:
    originals = {p: _read_verbatim(p) for p in (API, DB)}
    if not run():
        print("!! 基线就是红的，先修基线")
        return 2
    print("基线: 绿\n")
    survived = []
    try:
        for path, name, old, new, why in MUTATIONS:
            src = originals[path]
            if old not in src:
                print(f"[SKIP] {name}\n       锚点没匹配到 —— 锁与代码脱节，同样是问题")
                survived.append(name + "（锚点失配）")
                continue
            path.write_text(src.replace(old, new, 1), encoding="utf-8", newline="")
            killed = not run()
            print(f"[{'KILLED' if killed else 'SURVIVED'}] {name}   <- 应由「{why}」拦下")
            if not killed:
                survived.append(name)
            path.write_text(src, encoding="utf-8", newline="")
    finally:
        for p, s in originals.items():
            p.write_text(s, encoding="utf-8", newline="")
    print()
    if survived:
        print(f"X {len(survived)} 个变异存活:")
        for s in survived:
            print("   -", s)
        return 1
    print(f"OK 全部 {len(MUTATIONS)} 个变异均被杀死")
    return 0


if __name__ == "__main__":
    sys.exit(main())
