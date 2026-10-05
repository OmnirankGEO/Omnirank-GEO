"""B-⑥ · 抽屉「转人工」拼装**不许超出后端合同**(微返修单 2026-08-21)。

## 缺陷形态

`useXiaobangHandoff.buildHandoffMessage()` 每轮截 200 字,但**总长没有上限**。
后端 `api/faq_api.py` 的 `FeedbackRequest.message = Field(..., min_length=5,
max_length=500)` 是**存量合同**(`FeedbackPage` 也在打同一个端点),不许改。

于是:用户跟小榜聊得越久 → 带的轮数越多 → 拼得越长 → 点「确认提交」拿 422。
**越是需要转人工的会话越必然提交失败** —— 这是 42 班新引入的。

## 判据打在哪

🔴 判据**不重写一份拼装逻辑**。同一个谓词写两处必有一处没人验,而漂的方向
   一定是判据那份更宽松。所以这里用 `handoff_message_probe.mjs` 在 node 里
   **原样执行生产源文件**(只把 `react` / `@/lib/api` 两个 import 换成空壳),
   再把它吐出来的**真请求体**打进**真端点 + 真库**。

   三层缺一不可:
     · 只验 `buildHandoffMessage` ⇒ 不证明有人把它的结果放进 body
       (`submit` 模式验的就是这一跳);
     · 只验 `len(message) <= 500` ⇒ 不证明后端真的在 500 这条线上
       (所以有 500/501 两条**打真端点**的反向对照);
     · 只验 `!= 422` ⇒ 漏掉整层库合同(所以断言 200 且落库)。

🔴 路径一律从 `__file__` 起算,不吃 cwd —— A/B 两臂各自必须探**自己那棵树**
   的 TS 文件,吃 cwd 会让两臂探同一份源码,双臂恒等。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api.faq_api as faq

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]
_PROBE = _HERE / "handoff_message_probe.mjs"
_HOOK = _REPO_ROOT / "frontend" / "src" / "hooks" / "useXiaobangHandoff.ts"

#: 后端合同上限。判据这边**写死**,不从前端常量读 —— 从被测方读它自己的尺子,
#: 尺子被改宽时判据会跟着变宽,等于没在守。
BACKEND_MESSAGE_MAX = 500

ELISION = "…(更早对话已省略)"

#: 生产中间件写的是 `user_id`(`auth/middleware.py:267`),**不是** `id`。
#: 夹具用生产从来不会发的键 = 端点在生产必挂而判据全绿。
USER = {"user_id": 8801, "username": "handoff-agent", "is_admin": False,
        "permissions": ["writing:read"]}


# ══════════════════════════════════════════════════════════════════════
# 探针:在 node 里跑生产 TS
# ══════════════════════════════════════════════════════════════════════

def _probe(mode: str, payload: dict) -> dict:
    assert _HOOK.exists(), "生产源文件不在:%s" % _HOOK
    assert _PROBE.exists(), "探针不在:%s" % _PROBE
    proc = subprocess.run(
        ["node", str(_PROBE), mode, str(_HOOK), json.dumps(payload, ensure_ascii=False)],
        capture_output=True, cwd=str(_REPO_ROOT),
    )
    if proc.returncode != 0:
        raise AssertionError("探针失败 rc=%s\nstderr=%s" % (
            proc.returncode, proc.stderr.decode("utf-8", "replace")))
    return json.loads(proc.stdout.decode("utf-8"))


def _payload(question: str, turns, page: str = "/writing") -> dict:
    return {
        "question": question,
        "aiAnswer": "小榜刚才答过的话",
        "currentPage": page,
        "recentTurns": list(turns),
    }


def _turns(n: int, chars: int = 180):
    """n 轮对话,每轮 `chars` 字,内容里带序号 —— 好判断留下的是**哪几轮**。"""
    out = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append({"role": role, "content": "第%02d轮" % i + ("话" * (chars - 5))})
    return out


# ══════════════════════════════════════════════════════════════════════
# 真端点 + 真库
# ══════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def client():
    faq.init_faq_tables()          # 真建表(真类型),不手抄一份 schema
    app = FastAPI()

    @app.middleware("http")
    async def _inject(request, call_next):
        request.state.user = dict(USER)
        request.state.organization_identity = None
        return await call_next(request)

    app.include_router(faq.router)
    return TestClient(app, raise_server_exceptions=False)


def _post_body(client, body: dict):
    return client.post("/api/faq/feedback", json=body)


def _row(feedback_id: int) -> dict:
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id, message, kind FROM faq_feedback WHERE id = %s", (feedback_id,))
        row = cur.fetchone()
    finally:
        conn.close()
    return dict(row) if row else {}


# ══════════════════════════════════════════════════════════════════════
# ⓪ 合同边界本身 —— 打真端点,证明 500 这条线是真的
# ══════════════════════════════════════════════════════════════════════

def _raw_body(message: str, tag: str) -> dict:
    return {"client_id": "bug_cap_%s" % tag, "kind": "bug", "message": message,
            "urgency": "mid", "screenshot_url": "", "ai_answer": ""}


def test_backend_accepts_exactly_500_and_persists(client):
    """恰 500 必须过 —— 否则我这边的封顶是**过紧**的,白白砍用户的正文。

    而且要落库:只验「不是 422」会漏掉整层库合同(本仓付过这笔学费)。
    """
    resp = _post_body(client, _raw_body("边" * 500, "exact500"))
    assert resp.status_code == 200, resp.text
    fid = int(resp.json()["id"])
    assert _row(fid).get("message") == "边" * 500


def test_backend_rejects_501(client):
    """恰 501 必须被拒 —— 这条不红,就说明「超限会 422」这个前提根本不成立,
    上面所有封顶判据都在守一个不存在的东西。"""
    resp = _post_body(client, _raw_body("边" * 501, "exact501"))
    assert resp.status_code == 422, (resp.status_code, resp.text)


# ══════════════════════════════════════════════════════════════════════
# ① 长对话(3+ 轮)点「确认提交」→ 200
# ══════════════════════════════════════════════════════════════════════

def test_long_conversation_submits_200_end_to_end(client):
    """工单判据①。真链:生产 TS `submit()` → 真请求体 → 真端点 → 真库。

    5 轮 × 180 字,不封顶时拼出来 900+ 字,必然 422。
    """
    out = _probe("submit", _payload("我点开始写作一直没反应，已经试了三次", _turns(5)))
    body = out["body"]
    assert out["url"] == "/api/faq/feedback" and out["method"] == "POST"
    assert out["length"] <= BACKEND_MESSAGE_MAX, out["length"]

    resp = _post_body(client, body)
    assert resp.status_code == 200, (out["length"], resp.status_code, resp.text)
    fid = int(resp.json()["id"])
    assert _row(fid).get("message") == body["message"]


def test_long_conversation_keeps_the_user_question_and_the_newest_turn(client):
    """取舍顺序:**用户当前描述 > 最近的轮 > 更早的轮**。

    丢的必须是最早那几轮,而且丢过要留记号 —— 否则人工以为对话只有这么点。
    """
    question = "我点开始写作一直没反应，已经试了三次"
    out = _probe("build", _payload(question, _turns(5)))
    msg = out["message"]
    assert question in msg, msg
    assert ELISION in msg, msg
    assert "第04轮" in msg, "最近一轮被丢了 —— 取舍顺序反了"
    assert "第00轮" not in msg, "最早一轮还在 —— 丢的不是最早那几轮"


# ══════════════════════════════════════════════════════════════════════
# ② 恰 500 / 501 边界探针
# ══════════════════════════════════════════════════════════════════════

def _calibrate_to(total: int) -> str:
    """造一个**不触发丢轮**的 payload,让拼装结果恰好 `total` 字。

    先量一次 baseline,再按线性关系补差 —— 不手算格式,免得格式一改判据就哑。
    """
    turns = [{"role": "user", "content": "嗯"}]
    base_q = "问" * 10
    base = _probe("build", _payload(base_q, turns))["length"]
    need = 10 + (total - base)
    assert need > 0, (base, total)
    question = "问" * need
    out = _probe("build", _payload(question, turns))
    return question, out["message"]


def test_exactly_500_passes_through_untouched(client):
    """恰 500 = 合同内 ⇒ 一个字都不许动,更不许留省略号。"""
    question, msg = _calibrate_to(BACKEND_MESSAGE_MAX)
    assert len(msg) == BACKEND_MESSAGE_MAX, len(msg)
    assert ELISION not in msg, "恰 500 就被丢轮了 —— 封顶过紧"
    assert question in msg, "恰 500 就被截了用户描述 —— 封顶过紧"

    resp = _post_body(client, _raw_body(msg, "probe500"))
    assert resp.status_code == 200, resp.text


def test_501_is_trimmed_instead_of_being_rejected(client):
    """恰 501 = 超一个字 ⇒ 必须被削到 500 以内,而**不是**原样发出去吃 422。

    反向对照:同一段文本不削(+1 字)打真端点,必须 422 —— 证明这一格
    真的在合同外,封顶这一行是承重的。
    """
    _, five_hundred = _calibrate_to(BACKEND_MESSAGE_MAX)
    assert _post_body(client, _raw_body(five_hundred + "超", "naive501")).status_code == 422

    _, msg = _calibrate_to(BACKEND_MESSAGE_MAX + 1)
    assert len(msg) <= BACKEND_MESSAGE_MAX, len(msg)
    assert _post_body(client, _raw_body(msg, "probe501")).status_code == 200


# ══════════════════════════════════════════════════════════════════════
# ③ 反向对照:短对话照常**全量**带上
# ══════════════════════════════════════════════════════════════════════

def test_short_conversation_is_not_trimmed_at_all(client):
    """封顶不许顺手把正常会话也砍了。

    这条是防「一刀切成 200 字」那种修法 —— 那样判据①也会绿,但用户
    每次转人工都少带上下文,人工接不住。
    """
    turns = [{"role": "user", "content": "开始写作点不动"},
             {"role": "assistant", "content": "先看看知识库是不是空的"},
             {"role": "user", "content": "知识库有东西啊"}]
    question = "到底为什么点不动"
    out = _probe("submit", _payload(question, turns))
    msg = out["message"]

    assert out["length"] < BACKEND_MESSAGE_MAX, out["length"]
    assert ELISION not in msg, "短对话被丢轮了"
    assert question in msg
    for turn in turns:                      # 每一轮**逐字**都在
        assert turn["content"] in msg, turn["content"]
    assert _post_body(client, out["body"]).status_code == 200


def test_a_two_hundred_char_turn_is_still_capped_per_turn():
    """反向对照的反面:单轮 200 字上限是 v1 就有的,本次不许弄丢。"""
    long_turn = [{"role": "user", "content": "长" * 400}]
    msg = _probe("build", _payload("短问题", long_turn))["message"]
    assert "长" * 200 in msg
    assert "长" * 201 not in msg


# ══════════════════════════════════════════════════════════════════════
# ④ 顺带修的两处,各自欠一条判据
# ══════════════════════════════════════════════════════════════════════

def test_truncation_never_splits_a_surrogate_pair(client):
    """截断刀口不许落在代理对中间。

    半个代理 pydantic 收得下,要等写库 encode UTF-8 才炸 —— 那是个 500,
    而且现场在数据库层,离前端十万八千里。所以这条直接打**真库**。
    """
    # 单轮 200 字上限:把 emoji 排到第 200 个码元上,刀口正好落在代理对中间
    content = "字" * 199 + "😀" + "尾" * 50
    out = _probe("build", _payload("短问题", [{"role": "user", "content": content}]))
    msg = out["message"]
    for ch in msg:
        assert not (0xD800 <= ord(ch) <= 0xDFFF), "拼出了孤立代理:%r" % ch
    resp = _post_body(client, _raw_body(msg, "surrogate"))
    assert resp.status_code == 200, resp.text
    assert _row(int(resp.json()["id"])).get("message") == msg


def test_absurdly_long_page_still_within_cap():
    """兜底硬钳:`currentPage` 荒谬地长时,预算会算成负数。

    没有那一钳,这里会拼出 5000+ 字 —— 判据必须驱动那一行,不是让它躺着。
    """
    out = _probe("build", _payload("短问题", _turns(3), page="/x" + "y" * 5000))
    assert out["length"] <= BACKEND_MESSAGE_MAX, out["length"]


# ══════════════════════════════════════════════════════════════════════
# ⑤ 探针自证:它真的在跑生产文件
# ══════════════════════════════════════════════════════════════════════

def test_the_probe_reads_the_production_constant():
    """探针拿到的 `HANDOFF_MESSAGE_MAX` 必须就是生产常量,且等于后端合同。

    两边对不上 = 前端在按一个和后端不一样的尺子封顶。
    """
    out = _probe("build", _payload("x", []))
    assert out["max"] == BACKEND_MESSAGE_MAX, out["max"]


def test_the_probe_fails_loudly_on_a_missing_export():
    """给探针注毒:指向一个没有那两个导出的文件,必须**非零退出**。

    探针静默返回空结果 ⇒ 上面每一条都会拿着空 message 全绿。
    """
    decoy = _HERE / "__probe_decoy.ts"
    decoy.write_text("export const nothing: number = 1\n", encoding="utf-8")
    try:
        proc = subprocess.run(
            ["node", str(_PROBE), "build", str(decoy), "{}"],
            capture_output=True, cwd=str(_REPO_ROOT),
        )
        assert proc.returncode != 0, proc.stdout.decode("utf-8", "replace")
    finally:
        decoy.unlink(missing_ok=True)
