"""#151-B · 一条 item 只存**它自己那一个**单号。

## 缺陷(Deploy 只读取证 2026-09-08)

批量提交时批级回执被扇给每条:`order_sn_map = {m: inline_sn for m in media_ids}`。
于是 22 条 item **各自存了整批 5 或 7 个单号**
(正常 651 条段数=1;段数>1 的**恰好**就是卡死全集)。

后果:状态回写 `WHERE mhz_order_id = %s` **永远匹配不上** ——
供应商那 22 个单号早已终态(17 已发布带 url / 5 被拒),
本地却 27 天一直显示「在发」,**两边各自看都正常**。

⇒ 不是「供应商没完结」也不是「没人捞」,是**匹配键从一开始就写错了**。
  (我在 #151-B 取证时把 A/B 两种机理并列、要了两个只读读数才敢动手 ——
   若按 A 写重探,在 B 下会「探到了还是写不进去」,而那看起来像重探没用。)

## 判据三面

  · **唯一写入点拒收**多段值(闸设在写入点,不是逐个调用点);
  · **三处扇出**都堵住(普查出来的,工单只提了写入侧);
  · **修复脚本默认 dry-run**,不传 `--execute` 一个字都不写。
"""

from __future__ import annotations

import ast
import io
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


# ══════════════════════════════════ 判别与写入闸

@pytest.mark.parametrize("value,expect", [
    ("SN20260810001", 1),
    ("SN-2026-0810-001", 1),          # 连字符是合法单号的一部分
    ("SN001,SN002", 2),
    ("SN001 SN002 SN003", 3),
    ("SN001;SN002|SN003", 3),
    ("", 0),
])
def test_segment_count_reads_separators_not_length(value, expect):
    """段数按**分隔符**数,不按长度。

    长度是猜的(多长算长?),分隔符是事实。
    🔴 连字符**不算**分隔符 —— 把它算进来会把合法单号误判成批。
    """
    from db.meijiehezi_db import order_sn_segment_count
    assert order_sn_segment_count(value) == expect


def test_the_sole_write_site_refuses_a_batch_receipt(monkeypatch):
    """🔴🔴 承重:唯一写入点**拒收**多段值,并返回 False。

    毒:去掉这道闸 ⇒ 本条红。
    闸设在这里而不是逐个调用点,是因为调用点有 6 处 ——
    逐个设闸漏一处就再造一批(仓里那道空单号闸也是同一个理由,同一个位置)。
    """
    import db.meijiehezi_db as mdb

    called = {"n": 0}
    monkeypatch.setattr(mdb, "_get_conn",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    assert mdb.update_order_item_submitted(1, "SN001,SN002") is False
    assert called["n"] == 0, "拒收了却仍然连了库 —— 说明闸在写之后"


def test_a_single_order_sn_still_passes(monkeypatch):
    """🔴 正样本臂:单个单号照常写。

    只证「多段被拒」不够 —— 一个把所有值都拒掉的实现同样能让上一条变绿,
    而那会让**每一次**下单都变成僵尸(空 submitted 那个老缺陷的复刻)。
    """
    import db.meijiehezi_db as mdb

    seen = {}

    class _Cur:
        def execute(self, sql, args=None):
            seen["sql"], seen["args"] = sql, args

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            seen["committed"] = True

        def close(self):
            pass

    monkeypatch.setattr(mdb, "_get_conn", lambda *a, **k: _Conn())
    assert mdb.update_order_item_submitted(7, "SN-2026-0810-001") is True
    assert seen.get("committed") is True
    assert "SN-2026-0810-001" in seen["args"]


# ══════════════════════════════════ 扇出:三处都要堵(③ 普查)

def test_every_fan_out_of_a_single_receipt_is_guarded():
    """🔴 **普查出来的三处**扇出全部带多段判别。

    工单只提了写入侧;我普查 `{int(x): inline_sn for x in ...}` 这个形状,
    发现 client.py 里有 **3 处**(:848 软文 / :1022 自媒体 / :1770 短视频),
    第三处是我第一版漏掉的。
    毒:任一处去掉判别 ⇒ 本条红。
    """
    src = io.open(ROOT / "services" / "meijiehezi" / "client.py", encoding="utf-8").read()
    tree = ast.parse(src)

    fanouts = [n for n in ast.walk(tree) if isinstance(n, ast.DictComp)
               and "inline_sn" in ast.unparse(n)]
    assert len(fanouts) >= 3, (
        "扇出点少于 3 处 —— 分母塌了,不是通过:%d" % len(fanouts))

    # 🔴 数**真实 Call 节点**,不数名字出现次数:
    #    `def _is_multi_sn(value)` 那一行本身就含 `_is_multi_sn(`,
    #    把计数垫高一个 —— 去掉一处判别照样"够数"。
    #    (与「裸串锁被 import 顶住」同族,我在这条上又栽了一次。)
    guards = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
              and getattr(n.func, "id", None) == "_is_multi_sn"]
    assert len(guards) >= len(fanouts), (
        "有扇出点没带多段判别(判别**调用** %d 次 / 扇出 %d 处)"
        % (len(guards), len(fanouts)))


def test_the_multi_sn_predicate_has_exactly_one_definition():
    """🔴 判别只有**一份**(client 复用 db 层那个)。

    两份判别漂开时的表现是「client 放行、db 拒收」,而两边各自看都正常。
    """
    client = io.open(ROOT / "services" / "meijiehezi" / "client.py",
                     encoding="utf-8").read()
    assert "from db.meijiehezi_db import _looks_like_multi_order_sn" in client, (
        "client 自己写了第二份多段判别")


# ══════════════════════════════════ 修复脚本:默认不写

def test_the_repair_script_is_dry_run_by_default():
    """🔴 不传 `--execute` ⇒ **一个字都不写**。

    这批数据的根因就是「把一个值当成另一个值」,修它的脚本更不能手滑。
    毒:把 `--execute` 改成默认真、或让 dry-run 分支落到写库 ⇒ 本条红。
    """
    path = ROOT / "scripts" / "repair_151b_batch_order_sn_2026_09_08.py"
    assert path.exists()
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "main")
    body = ast.unparse(fn)
    # 🔴 不依赖引号风格:`ast.unparse` 会把双引号规范成单引号,
    #    判据写死 `action="store_true"` 会因为**格式**而红 —— 那是判据脆,不是代码错。
    assert "store_true" in body, "--execute 不是 opt-in"
    # dry-run 分支必须在任何写库之前 return
    i_guard = body.find("if not args.execute")
    i_write = min([x for x in (body.find("UPDATE mhz_publish_order_items"),
                               body.find("update_order_item_status")) if x != -1] or [-1])
    assert i_guard != -1, "没有 dry-run 闸"
    assert i_write == -1 or i_guard < i_write, (
        "写库出现在 dry-run 闸**之前**(%d vs %d)" % (i_guard, i_write))


def test_the_repair_script_does_not_invent_a_second_status_mapping():
    """🔴 回写走**既有**映射与既有函数,不新造。

    脚本里另写一份 2/-1/-2 的映射,就会出现「同步写 A、修复写 B」。
    """
    src = io.open(ROOT / "scripts" / "repair_151b_batch_order_sn_2026_09_08.py",
                  encoding="utf-8").read()
    assert "update_order_item_status" in src, "没走既有回写函数"
    assert "退款" in src and "不新造" in src, "没写明退款走既有规则"


def test_ambiguous_items_are_left_alone():
    """🔴 配不唯一的 item **保持 submitted**,列出来给人看。

    不猜 —— 这批数据出问题的根因就是「把一个值当成另一个值」,
    再猜一次等于用同一种错误去修它。
    """
    src = io.open(ROOT / "scripts" / "repair_151b_batch_order_sn_2026_09_08.py",
                  encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "plan")
    body = ast.unparse(fn)
    assert "ambiguous" in body, "没有「判不了」这一档"
    # 判不了的那一支不许产出 new_status
    assert body.index("ambiguous.append") < body.index("decided.append"), (
        "判不了的分支排在确定分支之后 —— 检查它是不是仍会落进 decided")
