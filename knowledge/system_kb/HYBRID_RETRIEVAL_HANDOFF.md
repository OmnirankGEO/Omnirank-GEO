# 小榜 Hybrid 检索升级 · 交接说明(给 Codex 复审)

> 日期:2026-06-02 · 范围:检索层准备(截图 OCR→文字抽取的真正接线属功能1,本次不做)
> 状态:33 tests passed · **未入库 · 未提交**

## 一、设计定调(已批准)
路由保底 + BM25 精准 + embedding 语义 + **身份前置过滤**。不做纯 embedding、不替代 BM25。
```
用户问题[+attachment_text截断] → query_vec=embed(问题+辅助文字)(失败→None→纯BM25)
 → _select_pool(身份)  ← RBAC 前置过滤,cosine 只在池内算(红线)
 → 池内 BM25 + cosine → ×路由同页 ×类型权重 → 合并排序
 → 强制注入 current_page 页面卡
 → 低置信(route卡有但内容分<阈值)→ 承认页面+转人工,不硬答
 → 否则 DeepSeek 基于命中内容回答
```

## 二、改了哪些文件/函数

### `api/xiaobang_api.py`
- 新增常量(env 可调):`ROUTE_MATCH_MULT=1.5`、`_TYPE_WEIGHT{sys_qa1.3, sys_button1.25, sys_error1.25, sys_field1.1}`、`AUX_TEXT_MAX=200`、`ROUTE_ONLY_MIN_CONTENT=1.0`。
- `bm25_search(...)` 新增参数 `current_page`、`aux_text`:
  - `aux_text` 截短后并入 BM25 query(`effective_query = query + aux[:AUX_TEXT_MAX]`);
  - 合并分后 `×ROUTE_MATCH_MULT`(命中 `_canonical_route(current_page)`)、`×_TYPE_WEIGHT[type]`;
  - **身份池 `_select_pool(is_admin, identity)` 不变,cosine 仅在池内算**。
- `XiaobangChatRequest` 新增 `attachment_text: Optional[str] = None`(原先缺字段,pydantic 会吞掉)。
- chat 入口:`attachment_text = _clip_attachment_text(req.attachment_text)`(**一处截断**)→ `embed_input = 问题 + 截断后辅助文字` → `bm25_search(current_page=req.current_page, aux_text=attachment_text)`。
- 新增纯函数(可单测):
  - `_is_route_only_insufficient(page_card, results, top_score)` → 低置信兜底判定(检索层)。
  - `_clip_attachment_text(text)` → 统一截断到 `AUX_TEXT_MAX`,embedding 与 BM25 吃同一份。
    - 说明:chat 入口的 `_clip_attachment_text` 是**主截断路径**;`bm25_search` 内部对 `aux_text` 再截断一次是**防御性保护**,防止未来有人直接调用 `bm25_search` 时误传超长文本。
- chat 入口低置信分支:命中 page_card 但 `top_score < ROUTE_ONLY_MIN_CONTENT` → 回"我能看到你大概在「X」页面,但这块知识库说明还不够…接人工",`meta.handoff=True`,不调 LLM。

### `tools/xiaobang_kb_indexer.py`
- `backfill_embeddings(batch_size=10, source_types=None)`:新增 `source_types` 过滤(只补指定类型);embedding 输入升级为**富文本** `页面名｜路由｜chunk类型｜小节(字段/按钮名)｜正文`(≤800字)。

### `tools/xiaobang_system_kb.py`
- `reindex_system(pages_glob, with_embeddings=True)`:入库后同步 `backfill_embeddings(source_types=SYS_SOURCE_TYPES)`,**只补 sys_page/sys_field/sys_button/sys_error/sys_qa**,不碰旧 doc/faq/preset;失败仅告警(BM25 仍可用)。测试可传 `with_embeddings=False` 跳过网络。

## 三、Codex 4 条硬要求 + 2 修复 对应

| 要求 | 落点 | 测试 |
|---|---|---|
| embedding 在身份池内算 | cosine 仅在 `_select_pool` 选出的池内 | `test_embedding_pool_isolation_normal_user`(agent 向量=query 也拿不到) |
| backfill 只补 sys_* | `source_types=SYS_SOURCE_TYPES` | — |
| aux_text 不塞整段 OCR | `_clip_attachment_text` 入口截断 + bm25 内部兜底截断 | `test_attachment_text_truncated_consistently` |
| 低置信由检索层判 | `_is_route_only_insufficient` | `test_route_only_insufficient_handoff` |
| 修1: attachment_text 字段 | 加到 `XiaobangChatRequest` + 入口读 `req.attachment_text` | `test_request_accepts_attachment_text` / `test_canned_not_short_circuited_with_attachment` |
| 修2: 一处截断 | `_clip_attachment_text` 同时供 embed_input 与 bm25 | `test_attachment_text_truncated_consistently` |
| route/current_page 加权 | `×ROUTE_MATCH_MULT` | `test_route_match_boost` |
| 类型加权 | `×_TYPE_WEIGHT` | `test_chunk_type_boost_qa_over_page` |
| query_vec 失败 fallback | `query_vec=None` → 纯 BM25 | `test_query_vec_fallback_to_bm25` |
| aux 提升按钮/字段/报错召回 | aux 并入 query | `test_aux_text_improves_button_recall` |

## 四、如何验证
```
pytest tests/system_kb -q          # 33 passed(含 9 条 hybrid/接线测试)
```
全部离线,用手工注入的假 embedding + 假 query_vec,不依赖 DashScope。

## 五、未做 / 边界
- **截图 OCR → 按钮/字段/报错/状态/步骤 的真正抽取属功能1**;本次只把 `attachment_text` 参数、截断、加权、测试备好。前端接线后传 `attachment_text` 即可生效。
- **未入库**(crawl_claude 空)、**未提交**;真向量生成留到放行后 `reindex_system()`(需 `DASHSCOPE_API_KEY`,会自动 backfill sys_* 向量)。
- 没碰 Codex 会话 / 没动旧 doc/faq/preset 向量。

## 六、env 可调旋钮
`XIAOBANG_ROUTE_MATCH_MULT` / `XIAOBANG_W_SYS_QA|BUTTON|ERROR|FIELD` / `XIAOBANG_AUX_TEXT_MAX` / `XIAOBANG_ROUTE_ONLY_MIN_CONTENT` / `XIAOBANG_BM25_WEIGHT|COSINE_WEIGHT|COSINE_SCALE`。
