#!/usr/bin/env python3
"""
P0-0 媒体成本快照构建 CLI(2026-06-13)

从 live mhz_media(active GEO 池)构建版本化成本快照。先跑字段映射 fail-closed 核验,
再出 snapshot,可选写入 media_cost_snapshot 表(append-only · 禁同版本静默覆盖不同内容)。

用法:
  python scripts/build_media_cost_snapshot.py --dry-run                 # 只读 · 打印 payload(prod 核验用)
  python scripts/build_media_cost_snapshot.py --output /tmp/snap.json   # 出 JSON 供审阅
  python scripts/build_media_cost_snapshot.py --write-db                # 写 DB 表(需表已迁移)

⚠️ 不默认覆盖 committed bootstrap(config/media_cost_snapshot.json);要更新基线请显式 --output 指向它并人工审。
"""
import argparse
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.media_cost_ssot import (  # noqa: E402
    build_snapshot_from_db, compare_to_bootstrap, MediaCostMappingError, _BOOTSTRAP_PATH,
)


def _write_db(payload: dict) -> str:
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        version = payload["snapshot_version"]
        sha = payload["payload_sha256"]
        cur.execute("SELECT id, payload_sha256 FROM media_cost_snapshot WHERE snapshot_version=%s", (version,))
        existing = cur.fetchone()
        if existing:
            ex_id = existing["id"] if isinstance(existing, dict) else existing[0]
            old_sha = existing["payload_sha256"] if isinstance(existing, dict) else existing[1]
            if old_sha != sha:
                # 内容寻址下不应发生;真发生说明 hash 逻辑被破坏 → fail,绝不静默覆盖
                raise SystemExit(
                    f"拒绝写入:版本 {version} 已存在但内容 hash 不同(old={old_sha} new={sha})。"
                    f"绝不静默覆盖。请排查 build_snapshot_payload 版本生成逻辑。")
            # [P0-A 前置 2026-06-15] 同版本同内容已存在 → 不新增行,但【确保它是唯一 active】
            #   (单 active 不变量:assert_db_active_snapshot/resolve_snapshot_source 取 latest active,
            #    若历史遗留多条 active 会语义不清;此处事务内归一为唯一 active = 你刚构建/确认的这份)。
            cur.execute("UPDATE media_cost_snapshot SET is_active=FALSE WHERE is_active AND id<>%s", (ex_id,))
            cur.execute("UPDATE media_cost_snapshot SET is_active=TRUE WHERE id=%s", (ex_id,))
            conn.commit()
            return f"no-op 行已存在·已归一为唯一 active: {version}"
        # [P0-A 前置 2026-06-15] 新版本:事务内先降所有旧 active,再 INSERT 新行(is_active 默认 TRUE)
        #   → 保证写完恰好【一条 active】(原代码 append-only + DEFAULT TRUE 但从不降旧 → 重建会累积多条 active)。
        cur.execute("UPDATE media_cost_snapshot SET is_active=FALSE WHERE is_active")
        cur.execute(
            "INSERT INTO media_cost_snapshot "
            "(snapshot_version, schema_version, source, payload, payload_sha256, geo_count, platform_media_markup, is_active) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s, TRUE) ON CONFLICT (snapshot_version) DO NOTHING",
            (version, payload.get("schema_version"), payload.get("source"),
             json.dumps(payload, ensure_ascii=False), sha, payload.get("geo_count"),
             payload.get("platform_media_markup_ratio")),
        )
        # [P0-A 前置 hardening 2026-06-15] INSERT 后校验 rowcount,commit 之前。
        #   极端场景:并发写入同版本 → ON CONFLICT DO NOTHING 命中 → rowcount=0,
        #   但上一句已把所有旧 active 降掉 → 若直接 commit 会留下【0 active】(系统静默回落 bootstrap)。
        #   连接非 autocommit(connection.py:180 强制 False),降旧+INSERT 在同一事务 →
        #   rowcount!=1 即 conn.rollback() 撤销降旧、raise,绝不留半截 0 active。
        if cur.rowcount != 1:
            conn.rollback()
            raise SystemExit(
                f"拒绝写入:INSERT 未成功(rowcount={cur.rowcount},疑似并发写入同版本 {version})。"
                f"已 rollback 撤销降旧 active,避免留下 0 active。请重跑 --write-db"
                f"(重跑会走 no-op 归一分支,把已存在行归一为唯一 active)。")
        conn.commit()
        return f"inserted(唯一 active): {version} (rowcount={cur.rowcount})"
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="Build media cost snapshot (P0-0)")
    ap.add_argument("--output", help="把 snapshot JSON 写到该路径(不指定则不写文件)")
    ap.add_argument("--write-db", action="store_true", help="写入 media_cost_snapshot 表(append-only)")
    ap.add_argument("--dry-run", action="store_true", help="只读:打印 payload,不写文件/DB")
    ap.add_argument("--excel-path", help="用该 Excel(docs/媒体成本 表)做三方映射核验(expect 值来自 Excel 外部真值)")
    ap.add_argument("--compare-bootstrap", action="store_true",
                    help="与 committed bootstrap 逐核心字段比对并打印差异")
    ap.add_argument("--strict-bootstrap", action="store_true",
                    help="比对后若核心字段超容差则 exit 3(隐含 --compare-bootstrap)")
    ap.add_argument("--bootstrap-tolerance", type=float, default=0.01, help="比对相对容差(默认 0.01)")
    args = ap.parse_args()

    try:
        payload = build_snapshot_from_db(excel_path=args.excel_path)
    except MediaCostMappingError as e:
        print(f"[FAIL-CLOSED] 字段映射核验未通过,拒绝构建快照: {e}", file=sys.stderr)
        sys.exit(2)

    summary = {k: payload[k] for k in ("snapshot_version", "schema_version", "source",
                                       "geo_count", "platform_media_markup_ratio") if k in payload}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("overall:", json.dumps(payload.get("overall", {}), ensure_ascii=False))
    print("by_tier:", json.dumps(payload.get("by_tier", {}), ensure_ascii=False))

    # 核对 committed bootstrap(把"dry-run 比对 bootstrap"落成代码)
    if args.compare_bootstrap or args.strict_bootstrap:
        with open(_BOOTSTRAP_PATH, encoding="utf-8") as f:
            bootstrap = json.load(f)
        cmp = compare_to_bootstrap(payload, bootstrap, tolerance=args.bootstrap_tolerance)
        print(f"[compare-bootstrap] ok={cmp['ok']} tolerance={cmp['tolerance']} diffs={len(cmp['diffs'])}")
        for d in cmp["diffs"]:
            print(f"  DIFF {d['field']}: candidate={d['candidate']} bootstrap={d['bootstrap']} rel_delta={d['rel_delta']}")
        if not cmp["ok"] and args.strict_bootstrap:
            print("[strict-bootstrap] 核心字段超容差 → exit 3", file=sys.stderr)
            sys.exit(3)

    if args.dry_run:
        print("[dry-run] 不写文件/DB。")
        return
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"written JSON → {args.output}")
    if args.write_db:
        print(_write_db(payload))


if __name__ == "__main__":
    main()
