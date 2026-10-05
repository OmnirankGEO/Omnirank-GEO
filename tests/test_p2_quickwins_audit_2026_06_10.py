# -*- coding: utf-8 -*-
"""P2 快赢批(audit 8 条 · 2026-06-10):
#2 单篇重写失败仍扣费 / #7 失败重写降级已完成选题 / #3 total_keywords 覆写 / #4 sync 丢超红海标 /
#5 audit 改价不同步 clusters_data / #8 文章文件端点(410×3+batch上限) / #14 PUT topics IDOR / #15 reset IDOR"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / "server.py").read_text(encoding="utf-8")


def _blk(src, anchor, span=2600):
    i = src.find(anchor)
    assert i > 0, f"未找到 {anchor}"
    return src[i:i + span]


def test_2_rewrite_error_raises_no_charge():
    b = _blk(SERVER, "article = await service.rewrite_article(")
    assert "article.get('error')" in b
    assert "重写失败,本次未收费" in b  # raise → _bill_feature_ctx 完成才扣被正确触发


def test_7_rewrite_fail_keeps_completed():
    src = (ROOT / "writing" / "article_generator_service.py").read_text(encoding="utf-8")
    assert "AND status != 'completed'" in src, "重写失败禁降级已完成成稿(防孤儿文章)"


def test_3_total_keywords_count_not_overwrite():
    src = (ROOT / "db" / "diagnosis_db.py").read_text(encoding="utf-8")
    i = src.find("UPDATE quotes SET total_keywords")
    blk = src[i:i + 240]
    assert "SELECT COUNT(*) FROM confirmed_keywords WHERE quote_id" in blk, \
        "total_keywords 必须 COUNT 重算(多包订单旧版被最后一包覆写)"


def test_4_sync_carries_super_red_ocean():
    """[#5 返修] cluster-core / cluster-covered / unclustered / flat 四处具体路径均须透传超红海标
    (原 ⑥ 漏 unclustered sink → 未分组核心词丢 super_red_ocean → 达标排除失效)。"""
    src = (ROOT / "services" / "quote_keyword_sync.py").read_text(encoding="utf-8")
    assert src.count('"super_red_ocean": bool(') >= 4, "四处路径都必须透传超红海标(含 unclustered)"
    # unclustered(自定义)路径必须具体含 super_red_ocean
    i_uk = src.find('"category": "自定义"')
    assert i_uk > 0, "未找到 unclustered(自定义)路径"
    uk_blk = src[i_uk:i_uk + 800]
    assert '"super_red_ocean": bool(uk.get(' in uk_blk, "[#5 返修] unclustered 路径漏 super_red_ocean"


def test_5_audit_keyword_syncs_clusters_data():
    src = (ROOT / "api" / "selection_api.py").read_text(encoding="utf-8")
    i = src.find("def audit_single_keyword" if "def audit_single_keyword" in src else "audit_single_keyword")
    i = src.find("async def audit_single_keyword")
    blk = src[i:i + 9000]
    assert "_cluster_synced" in blk
    assert 'clusters_data=json.dumps(clusters_data, ensure_ascii=False)' in blk
    assert '"savings"' in blk  # 包级重算齐全


def test_8_download_endpoints_gone():
    for dec, label in [('@app.post("/api/articles/download-batch")', "download-batch"),
                       ('@app.get("/api/articles/download-file")', "download-file"),
                       ('@app.delete("/api/articles/delete")', "delete 单删")]:
        b = _blk(SERVER, dec, 700)
        assert "410" in b, f"{label} 必须 410 下线"
    b = _blk(SERVER, '@app.post("/api/articles/batch-delete")', 1600)
    assert "单次批量上限 100" in b


def test_14_update_topic_rbac():
    b = _blk(SERVER, '@app.put("/api/writing/topics/{topic_id}")')
    assert "require_quote_access(http_request, _row" in b
    assert "http_request: Request" in b


def test_15_reset_to_pending_rbac_and_cap():
    b = _blk(SERVER, '@app.post("/api/writing/reset-to-pending")', 3200)
    assert "require_quote_access(http_request, _qid)" in b
    assert "单次批量上限 500" in b
    assert "SELECT DISTINCT quote_id FROM topics WHERE id = ANY(%s)" in b


# ============================================================
# #5 Fable 返修(2026-06-10)
# ============================================================
def test_5_1_frontend_single_delete_uses_batch():
    """[#5 返修] /api/articles/delete 已 410 下线 → 前端单删按钮(WritingCenter 真按钮)+ api 客户端
    均须改走已加固的 batch-delete,否则单删上线即坏(注释「前端 0 调用」是伪命题)。"""
    wc = (ROOT / "frontend" / "src" / "pages" / "Writing" / "WritingCenter.tsx").read_text(encoding="utf-8")
    # confirmDelete 单删分支不得再调死端点 articlesApi.delete
    i_conf = wc.find("const confirmDelete")
    blk = wc[i_conf:i_conf + 800]
    assert "articlesApi.batchDelete([deleteConfirm.filePath])" in blk, "单删须走 batchDelete([filePath])"
    assert "articlesApi.delete(deleteConfirm.filePath)" not in blk, "不得再调已 410 的单删端点"
    api_ts = (ROOT / "frontend" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
    i_del = api_ts.find("    delete: (filePath: string) =>")
    assert i_del > 0
    del_blk = api_ts[i_del:i_del + 160]
    assert "/api/articles/batch-delete" in del_blk, "客户端 delete 须重指向 batch-delete(防误调死端点)"


def test_5_2_put_topic_null_quote_admin_only():
    """[#5 返修] PUT topics 反查 quote_id 为 NULL(归属不明)时 fail-closed:仅 admin 可改,
    非 admin 403(消除潜伏跨租户旁路)。"""
    b = _blk(SERVER, '@app.put("/api/writing/topics/{topic_id}")', 1400)
    assert 'if _row.get("quote_id"):' in b, "有 quote_id 才走 require_quote_access"
    assert "require_quote_access(http_request, _row" in b
    # NULL quote_id 分支 admin-only fail-closed
    assert 'if not (_user and _user.get("is_admin")):' in b, "NULL quote_id 须 admin-only"
    assert "选题归属不明" in b and "status_code=403" in b, "非 admin 须 403 拒绝"
