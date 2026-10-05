"""[工单 2026-08-06 §1] 全站禁用原生 confirm()/alert() 的回归锁。

生产事故(2026-08-06 access log 实证):用户 133 在客户详情点「删除客户」,
20 秒内发了 38 次 `GET /api/my-clients/679/archive-preview`(全 200),
`DELETE /api/my-clients/679` **一次都没发出**。preview 与 DELETE 之间只隔着
取字段和一个 `confirm()`,取的字段后端无条件返回且全部有兜底 ——
排除后,`confirm()` 返回 false 是唯一剩下的路径,而用户根本没看见对话框。

🔴 这是**第三次**修同一个 bug(2026-05-09 / 2026-07-01 / 2026-08-06)。
2026-07-01 那次全扫除用的 grep 是 `window\\.(confirm|alert)\\(`,
**裸写法 `confirm(...)` / `alert(...)` 整批漏网** —— 修了一半,
漏的那一半是靠 grep 写法区分的,不是靠重要性。

所以本锁的第一条职责是:**能抓到裸写法**。它用的是
`scripts/scan_native_dialogs_2026_08_06.py` 那个真扫描器(剥注释/字符串/正则字面量、
识别局部同名绑定),不是正则。判别力自证见 test_scanner_selfcheck.py。
"""
import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCANNER = _ROOT / "scripts" / "scan_native_dialogs_2026_08_06.py"


def _scanner():
    spec = importlib.util.spec_from_file_location("scan_native_dialogs", _SCANNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_no_native_confirm_or_alert_anywhere_in_frontend():
    """面锁:全前端不许再有原生 confirm()/alert() 调用点(豁免清单除外)。

    🔴 反向对照要求:本锁跑在**修改前**的 tree 上必须红(能抓到 84 处 / 48 文件)。
    只跑修改后 = 恒真,整条作废。交付说明里必须带上那次 A/B 的实跑输出。
    """
    m = _scanner()
    bad = m.offenders()
    assert not bad, (
        "原生 confirm()/alert() 复活了。它在部分 WebKit 会话里会静默返回 false 且不弹窗,\n"
        "表现成「点了没反应」,而用户看不出任何原因。请改用 useConfirmDialog / toast:\n"
        + "\n".join(f"  {h.path}:{h.line}  [{h.kind}]  {h.snippet}" for h in bad)
    )


def test_version_poll_exemption_is_still_the_only_one():
    """🔴 必须不命中:versionPoll.ts 那处是**灾难兜底**,工单 §1.6 明确要求保留。

    这条同时防两头:
      - 有人「顺手」把它一起删掉(那会让版本刷新失败时彻底无声);
      - 有人往豁免清单里加新条目来让上面那条锁变绿(豁免清单不是垃圾桶)。
    """
    m = _scanner()
    assert set(m._EXEMPT) == {"frontend/src/lib/versionPoll.ts"}, (
        f"豁免清单变了:{sorted(m._EXEMPT)}。加豁免 = 关掉一部分闸,必须在工单里论证。"
    )
    exempted = [h for h in m.scan() if h.path in m._EXEMPT]
    assert exempted, "versionPoll.ts 的灾难兜底 confirm 被删了 —— 版本刷新失败时会彻底无声"


def test_archive_client_flow_actually_reaches_delete():
    """🔴 工单 §1.2 的验收判别点:任何「修好了」的证明必须看到 DELETE 真的发出。

    只证明「弹窗弹出来了」不算 —— 133 那次就是弹窗这一步断掉的。
    这里锁的是**代码路径**:确认之后紧跟着的必须是 authApi.delete,
    中间不许再插入任何会中断的同步判定。
    """
    for rel in ("frontend/src/pages/Brand/BrandDetailPage.tsx",
                "frontend/src/pages/Brand/MyClientsPage.tsx"):
        src = (_ROOT / rel).read_text(encoding="utf-8")
        assert "askConfirm({" in src, f"{rel}: 归档客户没走应用内弹窗"
        # 确认点之后必须真的发 DELETE
        # 🔴 锚点用 URL 的完整收尾形态(反引号结尾),不能只用 "archive-preview" ——
        #    本文件的注释里也写了这个词,只匹配词会锚到注释上,判据当场失去意义。
        idx = src.index("archive-preview`")
        tail = src[idx: idx + 2000]
        assert "await askConfirm(" in tail, f"{rel}: preview 之后没有应用内确认"
        assert "authApi.delete(" in tail, f"{rel}: 确认之后没有发出 DELETE —— 这正是 133 卡住的那一步"
        assert "confirm(`归档客户" not in src and "confirm('归档客户" not in src, \
            f"{rel}: 原生 confirm 还在"
