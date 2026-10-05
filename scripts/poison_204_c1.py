# -*- coding: utf-8 -*-
"""WO_204 c1 注毒闸。

纪律:
  · 基线失败集必须为空,且条数 == 预期;
  · 每发毒自证**字节变了**;还原用字节拷回并核 sha;
  · 判读比**失败集差**,不比 rc;
  · 仪器自己不许带控制字符;
  · 🔴 **每轮先 DROP 判据库**:本包的库由 conftest 从生产 schema + 059 建,
       不重建的话「毒在迁移里」那几发根本**够不着**(库还是上一版建的),
       而够不着与"锁没牙"在读数上完全同形。
"""
import glob
import hashlib
import io
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = "tests/imgnote_topics_2026_09_13"
EXPECTED_N = 52
DB_NAME = "geo_c14_204b_test"
PG_CONTAINER = "defgeo-c14-62-pg"
WHITESPACE = chr(10) + chr(13) + chr(9)

MIG = "db/migration_059_geo_douyin_topics_2026_09_13.sql"
DB = "db/geo_douyin_db.py"
PT = "services/geo_douyin/production_task.py"
DT = "services/geo_douyin/distill_task.py"
# [c1b] Review 复审补的两发毒打在 api 层(端点那一跳)
API = "api/geo_douyin_api.py"

ENV = dict(os.environ)
ENV["TEST_DATABASE_URL"] = "postgresql://geo_admin:testpw@localhost:55492/%s" % DB_NAME
ENV["PYTHONIOENCODING"] = "utf-8"


def sha(path):
    return hashlib.sha256(io.open(os.path.join(ROOT, path), "rb").read()).hexdigest()[:12]


def read(path):
    return io.open(os.path.join(ROOT, path), encoding="utf-8").read()


def write(path, text):
    io.open(os.path.join(ROOT, path), "w", encoding="utf-8", newline="\n").write(text)


def drop_db():
    for sql in ("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname='%s'" % DB_NAME,
                'DROP DATABASE IF EXISTS "%s"' % DB_NAME):
        subprocess.run(["docker", "exec", PG_CONTAINER, "psql", "-U", "geo_admin",
                        "-d", "postgres", "-c", sql], capture_output=True)


def run():
    drop_db()
    p = subprocess.run(
        [sys.executable, "-m", "pytest", PKG, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=ROOT, env=ENV, capture_output=True)
    out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
    failed = set()
    for line in out.splitlines():
        # 🔴 只认**短摘要行**:`FAILED <nodeid>` / `ERROR <nodeid>`,nodeid 必带 `::`。
        #    pytest 的 captured-log 会打 `ERROR    GEO-Douyin-Task:...` 这种**日志行**,
        #    按前缀收会把它算成一条失败 —— 于是"只有一条杂散日志"的那一轮
        #    会被读成「毒被抓住了」。毒没下成与锁没牙本来就同形,
        #    再加一个假的失败来源,判读就彻底不可信了。
        if not (line.startswith("FAILED ") or line.startswith("ERROR ")):
            continue
        parts = line.split()
        if len(parts) < 2 or "::" not in parts[1]:
            continue
        failed.add(parts[1].split(" - ")[0])
    total = len([l for l in out.splitlines() if "::" in l and
                 (l.startswith("FAILED ") or l.startswith("ERROR "))])
    # 条数单独收集(--collect-only 不建库,快)
    c = subprocess.run(
        [sys.executable, "-m", "pytest", PKG, "-q", "--collect-only",
         "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, env=ENV, capture_output=True)
    total = len([l for l in c.stdout.decode("utf-8", "replace").splitlines()
                 if "::" in l])
    return failed, total


POISONS = [
    ("T1-去掉「以选题为准」(WO C3 点名的毒)", PT,
     '        _final_title = str(topic_title or "").strip() or content.title',
     "        _final_title = content.title",
     "用户改了标题,做出来还是 AI 自己想的那个。"),

    ("T2-title_source 恒 llm", PT,
     '        _title_source = "topic" if str(topic_title or "").strip() else "llm"',
     '        _title_source = "llm"',
     "分不出哪些成品是按用户选题做的,效果复盘就没有分组依据。"),

    # 🔴 这一发换过。上一版写成 `ON CONFLICT DO NOTHING` —— 那**没有冲突目标**,
    #    与原版行为几乎等价,所以它绿**不说明锁没牙**,是毒自己空转。
    #    真正的"按 title 去重"要在 Python 侧先按标题折叠。
    ("T3-改成按 title 去重", DB,
     '    rows = [t for t in (topics or []) if str((t or {}).get("title") or "").strip()]',
     '    rows = list({str((t or {}).get("title") or "").strip(): t\n'
     '                 for t in (topics or [])\n'
     '                 if str((t or {}).get("title") or "").strip()}.values())',
     "标题相同的第二条被静默吞掉 —— 而两条选题的标题可以合法地相同。"),

    ("T4-重放不再幂等", DB,
     "                   DO NOTHING\n                   RETURNING id",
     "                   DO UPDATE SET title = EXCLUDED.title\n                   RETURNING id",
     "重放同一个 task 会报告「又落了一批」,而表里其实没新增。"),

    ("T5-抢单守卫从 WHERE 里拿掉", DB,
     "                WHERE id = %%s AND status = 'pending'\n                RETURNING %s",
     "                WHERE id = %%s\n                RETURNING %s",
     "双击时两次都抢到 ⇒ 同一条选题建两次单、扣两次算力。"),

    ("T6-做中也允许改标题", DB,
     "                WHERE id = %s AND status = 'pending'\n                RETURNING id",
     "                WHERE id = %s\n                RETURNING id",
     "已经在做的那一条被改了标题 ⇒ 成品与选题对不上。"),

    ("T7-做中也允许删除", DB,
     "            \"DELETE FROM geo_douyin_topics WHERE id = %s AND status = 'pending' RETURNING id\",",
     "            \"DELETE FROM geo_douyin_topics WHERE id = %s RETURNING id\",",
     "删掉在做的选题 ⇒ 成品成孤儿。"),

    ("T8-dispatch 不给选题收尾", PT,
     "        if not topic_id:\n            return",
     "        if True:\n            return",
     "选题永远卡在制作中:用户点不动,列表也不解释为什么。"),

    ("T9-迁移去掉 post_shape 那条 CHECK", MIG,
     "    CONSTRAINT ck_geo_douyin_topic_post_shape\n"
     "        CHECK ((status = 'done'::text AND post_id IS NOT NULL)\n"
     "               OR (status <> 'done'::text AND post_id IS NULL)),",
     "",
     "「做失败了但还留着上一次的 post_id」变成合法状态,点进去是别人的成品。"),

    ("T10-迁移去掉外键", MIG,
     "    CONSTRAINT fk_geo_douyin_topic_post\n"
     "        FOREIGN KEY (post_id) REFERENCES geo_douyin_posts (id)",
     "    CONSTRAINT ck_geo_douyin_topic_noop CHECK (true)",
     "选题可以指向一个不存在的成品。"),

    ("T11-迁移去掉幂等唯一索引", MIG,
     "CREATE UNIQUE INDEX IF NOT EXISTS uq_geo_douyin_topics_distill_slot",
     "CREATE INDEX IF NOT EXISTS uq_geo_douyin_topics_distill_slot",
     "ON CONFLICT 认不出这把索引 ⇒ 落表整条炸(而不是幂等)。"),

    # 🔴 这一发也换过。上一版只把 `payload["topics_persisted"] = …` 改成 `pass`,
    #    那**没有改位置** —— 落表仍在 with 体内,所以它绿同样是毒空转。
    #    真毒是把整段挪到 `charged = True` 之后(即扣费之外)。
    ("T12-落表挪到扣费 with 之外", DT,
     '            inserted = await asyncio.to_thread(\n'
     '                ddb.insert_distilled_topics,\n'
     '                brand_id=int(brand_id), created_by=int(user_id),\n'
     '                distill_task_id=int(task_id),\n'
     '                topics=[dict(t, city=city) for t in payload.get("topics") or []])\n'
     '            payload["topics_persisted"] = int(inserted)\n'
     '        # 走到这里 = with 正常退出 = 已扣费\n'
     '        charged = True',
     '        # 走到这里 = with 正常退出 = 已扣费\n'
     '        charged = True\n'
     '        inserted = await asyncio.to_thread(\n'
     '            ddb.insert_distilled_topics,\n'
     '            brand_id=int(brand_id), created_by=int(user_id),\n'
     '            distill_task_id=int(task_id),\n'
     '            topics=[dict(t, city=city) for t in payload.get("topics") or []])\n'
     '        payload["topics_persisted"] = int(inserted)',
     "落表失败仍然扣 130、任务显示成功、列表里一条都没有。"),

    # ── c1b:Review 复审 5ffaff96b 时两发全绿的毒,原样搬进来 ──────────────
    ("P6-选题鉴权去掉品牌校验(Review 原毒)", API,
     '    require_brand_access(request, int(row["brand_id"]))\n    return row',
     "    return row",
     "跨租户改/删别人的选题。原来 42 条全绿 —— 判据全在直接调 helper,"
     "端点那一跳裸奔。"),

    ("P7-异常路径不给选题收尾(Review 原毒)", PT,
     "            await _settle_topic(False)\n            return None",
     "            return None",
     "选题永远卡在「制作中」:用户点不动,列表也不解释为什么。"
     "原来的失败判据只走了 ok=False 那条路。"),

    ("P8-finish_topic 不限 making", DB,
     '                  SET status = \'done\', post_id = %s, updated_at = now()\n'
     "                WHERE id = %s AND status = 'making'",
     '                  SET status = \'done\', post_id = %s, updated_at = now()\n'
     "                WHERE id = %s",
     "一条没人做过的 pending 选题会被直接标成已做,并挂上别人的成品。"),

    # 锚要带 PATCH 专属的尾巴:两个端点开头逐字相同,只锚开头会命中 2 次。
    ("P9-总闸关着也照做(PATCH)", API,
     "    if not is_pipeline_enabled():\n        return _COMING_SOON\n"
     "    _user(request)\n    await _topic_or_404(topic_id, request)\n"
     "\n    import asyncio\n\n    from db import geo_douyin_db as ddb\n"
     "\n    outcome = await asyncio.to_thread(\n"
     "        ddb.update_topic_title, topic_id=int(topic_id), title=req.title)",
     "    _user(request)\n    await _topic_or_404(topic_id, request)\n"
     "\n    import asyncio\n\n    from db import geo_douyin_db as ddb\n"
     "\n    outcome = await asyncio.to_thread(\n"
     "        ddb.update_topic_title, topic_id=int(topic_id), title=req.title)",
     "半成品总闸形同虚设 —— 闸关着却照样改库。"),
]


def _no_control_chars():
    bad = []
    for f in [__file__] + glob.glob(os.path.join(ROOT, PKG, "*.py")):
        t = io.open(f, encoding="utf-8").read()
        hits = [hex(ord(c)) for c in t if ord(c) < 32 and c not in WHITESPACE]
        if hits:
            bad.append((f, sorted(set(hits))))
    return bad


def main():
    bad = _no_control_chars()
    if bad:
        print("  !! 仪器自己带控制字符,先修仪器:")
        for f, hits in bad:
            print("     %s %s" % (f, hits))
        return 2

    paths = sorted({p for _, p, _, _, _ in POISONS})
    base_sha = {p: sha(p) for p in paths}
    base_src = {p: read(p) for p in paths}

    failed, total = run()
    print("基线: 收集 %d 条 / 失败集 %d 条" % (total, len(failed)))
    if failed:
        print("  !! 基线失败集非空,先修基线")
        for f in sorted(failed):
            print("     " + f)
        return 2
    if total != EXPECTED_N:
        print("  !! 收集到 %d 条,预期 %d 条" % (total, EXPECTED_N))
        return 2

    rows = []
    for name, path, old, new, why in POISONS:
        src = base_src[path]
        cnt = src.count(old)
        if cnt != 1:
            rows.append((name, "毒没下成", "锚命中 %d 次(要 1 次)" % cnt))
            continue
        write(path, src.replace(old, new, 1))
        psha = sha(path)
        if psha == base_sha[path]:
            write(path, base_src[path])
            rows.append((name, "毒没下成", "sha 没变"))
            continue
        pf, ptotal = run()
        write(path, base_src[path])
        assert sha(path) == base_sha[path], "还原没回到基线 sha!"
        verdict = "红(锁有牙)" if pf else "绿(没牙/够不着/冗余)"
        rows.append((name, verdict, "sha %s->%s · 收集 %d · 抓住它的: %s" % (
            base_sha[path], psha, ptotal,
            ", ".join(sorted(x.split("::")[-1] for x in pf))[:140] or "无")))

    print()
    for name, verdict, detail in rows:
        print("%-40s %-22s %s" % (name, verdict, detail))
    f2, t2 = run()
    print("\n还原后复跑: 收集 %d / 失败 %d" % (t2, len(f2)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
