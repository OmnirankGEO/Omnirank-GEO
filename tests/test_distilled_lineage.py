"""
P1/P2(Codex 复审返修)· distilled 溯源 lineage 回归测试。

P1:_compute_distilled_hash 纯逻辑 + 缓存/重蒸馏两路径都调用 _ensure_quote_distilled_lineage。
P2:_save_article 与 rewrite_article 都调用 _copy_article_distilled_lineage(rewrite lineage 不为空)。

跑法:python -m pytest tests/test_distilled_lineage.py -q --noconftest -o addopts="" -p no:cacheprovider
"""
import inspect

from writing.article_generator_service import (
    _compute_distilled_hash,
    _ensure_quote_distilled_lineage,
    _copy_article_distilled_lineage,
    ArticleGeneratorService,
)


# ---------- P1 · _compute_distilled_hash 纯逻辑 ----------
def test_hash_deterministic_key_order():
    a = _compute_distilled_hash('{"b":1,"a":2}')
    b = _compute_distilled_hash('{"a":2,"b":1}')  # key 顺序不同 → 规范化后同 hash
    assert a is not None and len(a) == 64
    assert a == b


def test_hash_str_dict_equal():
    assert _compute_distilled_hash('{"a":1,"b":2}') == _compute_distilled_hash({"a": 1, "b": 2})


def test_hash_none_on_empty_or_invalid():
    assert _compute_distilled_hash(None) is None
    assert _compute_distilled_hash("") is None
    assert _compute_distilled_hash("{not valid json") is None
    assert _compute_distilled_hash({}) is None  # 空 dict 视为无内容


def test_hash_changes_with_content():
    assert _compute_distilled_hash('{"a":1}') != _compute_distilled_hash('{"a":2}')


# ---------- P1 · 缓存路径 + 重新蒸馏路径都调用 lineage helper ----------
def test_generate_single_calls_lineage_both_paths():
    src = inspect.getsource(ArticleGeneratorService._generate_single)
    # 缓存路径 backfill(无 source_hash/force)
    assert "_ensure_quote_distilled_lineage(self.quote_id)" in src
    # 重新蒸馏路径 force 覆盖写
    assert "_ensure_quote_distilled_lineage(self.quote_id, source_hash=" in src
    assert "force=True" in src
    # 重蒸馏 hash 必须基于真实 raw_data_json(不拿 fallback 基础数据)
    # [Review-CTO 2026-07-26] 变量早已 row→quote_row 改名,本断言在 base 就红
    # (陈年失修,非 WP12 回归);同步标识符,意图不变。
    assert "_compute_distilled_hash(quote_row['raw_data_json']" in src


# ---------- P2 · _save_article 与 rewrite_article 都复制 lineage ----------
def test_save_article_copies_lineage():
    src = inspect.getsource(ArticleGeneratorService._save_article)
    assert "_copy_article_distilled_lineage(article_id, self.quote_id)" in src


def test_rewrite_article_copies_lineage():
    src = inspect.getsource(ArticleGeneratorService.rewrite_article)
    assert "_copy_article_distilled_lineage(article_id, self.quote_id)" in src


def test_lineage_helpers_are_callable():
    # 防御:helper 存在且签名符合预期(便于其他模块复用)
    assert callable(_ensure_quote_distilled_lineage)
    assert callable(_copy_article_distilled_lineage)
    sig = inspect.signature(_ensure_quote_distilled_lineage)
    assert "source_hash" in sig.parameters and "force" in sig.parameters


# ---------- P2 边角(Codex 二审):copy 前必须先 ensure quote lineage(防 rewrite 老 quote 复制 NULL)----------
def test_copy_lineage_ensures_quote_before_copy(monkeypatch):
    # 真行为验证:monkeypatch 两个 helper + 注入 fake db.diagnosis_db(避免真 init_db 连库),确认调用顺序。
    # db/diagnosis_db.py 模块级有 init_db() · 直接 import 会连真库 · 故用 sys.modules 注入轻量假模块。
    import sys
    import types
    import writing.article_generator_service as ags

    calls = []

    class _Cur:
        def execute(self, sql, params=None):
            calls.append(("execute", sql))

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            calls.append(("commit", None))

        def close(self):
            pass

    fake_ddb = types.ModuleType("db.diagnosis_db")
    fake_ddb.get_connection = lambda: _Conn()
    monkeypatch.setitem(sys.modules, "db.diagnosis_db", fake_ddb)

    def _fake_ensure(quote_id, source_hash=None, force=False):
        calls.append(("ensure", quote_id))

    monkeypatch.setattr(ags, "_ensure_quote_distilled_lineage", _fake_ensure)

    ags._copy_article_distilled_lineage(article_id=11, quote_id=22)

    # ensure(quote 22)必须发生,且【早于】复制文章的 UPDATE articles —— 保证老 quote 先 backfill 再复制
    assert ("ensure", 22) in calls, f"copy 未先 ensure quote lineage,调用序: {calls}"
    ensure_idx = next(i for i, c in enumerate(calls) if c[0] == "ensure")
    update_idx = next(i for i, c in enumerate(calls) if c[0] == "execute" and "UPDATE articles" in c[1])
    assert ensure_idx < update_idx, f"ensure 必须先于 copy UPDATE,实际调用序: {calls}"
