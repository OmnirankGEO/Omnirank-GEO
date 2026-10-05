"""
帮助中心 FAQ API (Phase 2 · 2026-05-18)

用户侧(任意登录用户):
  GET   /api/faq/items                  · 已上架 FAQ 列表 + 我的投票
  POST  /api/faq/vote                   · 投票 / 切换 / 撤销
  POST  /api/faq/feedback               · 提交反馈(client_id 幂等)

管理员侧(is_admin):
  GET   /api/admin/faq/items            · 全部 FAQ(含未上架)
  POST  /api/admin/faq/items            · 新建
  PATCH /api/admin/faq/items/{id}       · 部分更新
  DELETE /api/admin/faq/items/{id}      · 删除
  GET   /api/admin/faq/feedback         · 反馈列表 · 可按状态/紧急度筛选
  PATCH /api/admin/faq/feedback/{id}    · 改状态 / 写备注
  GET   /api/admin/faq/feedback/counts  · 各状态反馈数量(tab badge)

实现规范沿用 api/admin_api.py:
  - 当前用户从 request.state.user 拿 (auth 中间件注入)
  - admin 校验用 _require_admin (replicated 防跨模块耦合)
  - 错误统一抛 HTTPException
"""

import asyncio
import json
import logging
import os
from typing import Optional
from fastapi import APIRouter, BackgroundTasks, Request, HTTPException, UploadFile, File
from pydantic import BaseModel, Field

from db.faq_db import (
    init_faq_tables,  # noqa: F401 · server.py 启动 hook 用
    list_faq_items,
    get_faq_item,
    create_faq_item,
    update_faq_item,
    delete_faq_item,
    reorder_items,
    set_vote,
    create_feedback,
    list_feedback,
    update_feedback_status,
    get_feedback_counts,
)
from auth.user_ctx import current_user_id

logger = logging.getLogger("FAQ-API")
router = APIRouter(tags=["帮助中心 FAQ"])


# 2026-05-25 小榜:FAQ CRUD 后自动重建小榜 KB 的 faq 部分
# 设计:不阻塞 CRUD 响应,在后台跑;失败仅记 warning 不影响 FAQ 写库本身
#
# [GEO-R10-CAN-027] fire-and-forget 破坏性重建加固(调用侧):
#   1. asyncio.create_task 的返回值必须持强引用,否则事件循环只保留弱引用,
#      任务可能在完成前被 GC 回收 → 重建静默丢失(Python 官方文档明确 warning)。
#   2. reindex_faq 是 clear-then-insert 的破坏性重建,中途失败会留下空/残缺的
#      faq KB。加有界重试(指数退避)让瞬时失败自愈、尽量缩短空窗;重试耗尽才记 error。
#   注:完整的 shadow/版本化原子发布 + 跨 worker 失效在 tools/xiaobang_kb_indexer.py
#   (本文件范围外,不改),此处只做调用侧能做的加固(不再裸 fire-and-forget + 可自愈重试)。
_FAQ_REINDEX_TASKS: set = set()
_FAQ_REINDEX_MAX_RETRY = int(os.getenv("FAQ_REINDEX_MAX_RETRY", "3"))


async def _run_faq_reindex_with_retry():
    """后台重建 FAQ 索引 · 有界重试(reindex_faq 幂等 · 每次从 DB 全量重建,可安全重跑)。"""
    from api.xiaobang_api import trigger_reindex  # noqa: PLC0415
    last_err = None
    for attempt in range(1, _FAQ_REINDEX_MAX_RETRY + 1):
        try:
            await trigger_reindex(scope="faq")
            if attempt > 1:
                logger.info("[faq] 小榜 FAQ 重索引第 %s 次重试成功", attempt)
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning(
                "[faq] 小榜 FAQ 重索引失败(第 %s/%s 次): %s",
                attempt, _FAQ_REINDEX_MAX_RETRY, e,
            )
            if attempt < _FAQ_REINDEX_MAX_RETRY:
                await asyncio.sleep(min(2 ** attempt, 30))
    # 重试耗尽:faq KB 可能残缺,记 error 供告警(仍不影响 FAQ 写库本身)
    logger.error(
        "[faq] 小榜 FAQ 重索引重试 %s 次仍失败, faq 索引可能残缺: %s",
        _FAQ_REINDEX_MAX_RETRY, last_err,
    )


def _trigger_xiaobang_faq_reindex():
    """在后台触发小榜 FAQ 重索引 · 调用方不等结果(持强引用防 GC + 自愈重试)"""
    try:
        task = asyncio.create_task(_run_faq_reindex_with_retry())
        # [GEO-R10-CAN-027] 持强引用防止任务完成前被 GC;完成后从集合移除
        _FAQ_REINDEX_TASKS.add(task)
        task.add_done_callback(_FAQ_REINDEX_TASKS.discard)
    except RuntimeError as e:
        # 无运行中的事件循环(async handler 内不会发生 · 兜底)
        logger.warning("[faq] 触发小榜 FAQ 重索引失败(无事件循环 · 不影响 FAQ 本身): %s", e)


# ==========================================
# 权限工具
# ==========================================

def _require_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def _require_admin(request: Request) -> dict:
    user = _require_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# ==========================================
# Pydantic 模型
# ==========================================

_CATEGORIES = {'billing', 'operation', 'data', 'account'}
_VISIBLE_TO = {'normal_user', 'agent', 'both'}
_URGENCIES = {'low', 'mid', 'high'}
_STATUSES = {'pending', 'read', 'done', 'closed'}
_FEEDBACK_KINDS = {'faq', 'bug'}
_BUG_SCREENSHOT_ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp"}
_BUG_SCREENSHOT_MAX_BYTES = int(os.getenv("BUG_FEEDBACK_IMAGE_MAX_BYTES", str(10 * 1024 * 1024)))


def _user_id(user: dict) -> int:
    return int(user.get("id") or user.get("user_id") or 0)


def _fetch_agent_level(user_id: int) -> int:
    if not user_id:
        return 0
    try:
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT agent_level FROM user_wallets WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            if not row:
                return 0
            raw = row.get("agent_level") if isinstance(row, dict) else row[0]
            return int(raw or 0)
        finally:
            conn.close()
    except Exception as e:
        logger.warning("[faq] 读取 agent_level 失败 user=%s err=%s（按普通用户处理）", user_id, e)
        return 0


def _is_agent_or_admin(user: dict) -> bool:
    if bool(user.get("is_admin")):
        return True
    level = user.get("agent_level")
    if level is None:
        level = _fetch_agent_level(_user_id(user))
    return int(level or 0) >= 1


def _agent_level(user: dict) -> int:
    level = user.get("agent_level")
    if level is None:
        level = _fetch_agent_level(_user_id(user))
    try:
        return int(level or 0)
    except Exception:
        return 0


def _resolve_identity(user: dict) -> str:
    """后端真实身份分档(不信前端传参):admin / l2 / agent(=L1) / normal_user。

    is_admin 来自 JWT(中间件注入,权威);agent_level 查 user_wallets(权威)。
    agent_level>=2 → 二级代理(l2),==1 → 一级代理(agent),0 → 普通用户。
    """
    if bool(user.get("is_admin")):
        return "admin"
    level = _agent_level(user)
    if level >= 2:
        return "l2"
    if level >= 1:
        return "agent"
    return "normal_user"


def _notify_admins_for_bug(feedback_id: int, message: str, urgency: str, user_id: int) -> None:
    """新 bug 反馈站内通知管理员。失败不影响用户提交。"""
    try:
        from db.connection import get_connection
        from db.team_db import create_user_notification

        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT u.id
                FROM users u
                WHERE EXISTS (
                    SELECT 1
                    FROM user_roles ur
                    JOIN roles r ON r.id = ur.role_id
                    WHERE ur.user_id = u.id AND r.name = 'admin'
                )
                ORDER BY u.id
                LIMIT 50
            """)
            admin_rows = cur.fetchall()
        finally:
            conn.close()

        title = "新的问题反馈"
        preview = message.strip().replace("\n", " ")[:80]
        content = f"{'紧急 · ' if urgency == 'high' else ''}{preview}"
        for row in admin_rows:
            admin_id = row.get("id") if isinstance(row, dict) else row[0]
            create_user_notification(
                int(admin_id),
                type="bug_feedback",
                title=title,
                content=content,
                link="/admin/help-center",
                level="important" if urgency == "high" else "light",
                metadata={"feedback_id": feedback_id, "user_id": user_id, "kind": "bug"},
            )
    except Exception as e:
        logger.warning("[faq] bug 反馈管理员通知失败 feedback=%s err=%s", feedback_id, e)


def _sign_feedback_screenshot(item: dict) -> dict:
    key = (item.get("screenshot_url") or "").strip()
    if not key or key.startswith("http://") or key.startswith("https://"):
        return item
    try:
        from services.oss_service import generate_feedback_signed_url
        item = dict(item)
        item["screenshot_key"] = key
        item["screenshot_url"] = generate_feedback_signed_url(key)
    except Exception as e:
        logger.warning("[faq] 反馈截图签名失败 key=%s err=%s", key, e)
        item = dict(item)
        item["screenshot_key"] = key
        item["screenshot_url"] = ""
    return item


class VoteRequest(BaseModel):
    faq_id: int
    vote: Optional[str] = Field(None, description="up / down / None(撤销)")


class FeedbackRequest(BaseModel):
    client_id: str = Field(..., min_length=8, max_length=64, description="前端生成 uuid · 去重用")
    faq_id: Optional[int] = None
    message: str = Field(..., min_length=5, max_length=500)
    urgency: str = Field('low', description="low / mid / high")
    contact: str = Field('', max_length=100)
    kind: str = Field('faq', description="faq / bug")
    screenshot_url: str = Field('', max_length=500)
    ai_answer: str = Field('', max_length=10000)


class CreateFAQRequest(BaseModel):
    question: str = Field(..., min_length=2, max_length=200)
    category: str = Field(..., description="billing / operation / data / account")
    answer_md: str = Field('', max_length=20000)
    sort_order: int = Field(0)
    is_published: bool = Field(True)
    visible_to: str = Field('both', description="normal_user / agent / both")


class UpdateFAQRequest(BaseModel):
    question: Optional[str] = Field(None, min_length=2, max_length=200)
    category: Optional[str] = None
    answer_md: Optional[str] = Field(None, max_length=20000)
    sort_order: Optional[int] = None
    is_published: Optional[bool] = None
    visible_to: Optional[str] = Field(None, description="normal_user / agent / both")


class UpdateFeedbackRequest(BaseModel):
    status: Optional[str] = None
    admin_note: Optional[str] = Field(None, max_length=2000)


# ==========================================
# 帮助文档身份白名单(后端权威 · 正文已迁后端按身份下发)
# ==========================================
# 帮助文档正文存在后端(api/help_docs_content.json),由 /api/help/docs/{slug} 按真实身份返回;
# 前端 bundle 不含正文。本处的 slug 集合是身份分档的权威来源,必须与 docs-data.ts 的
# audience 标注保持一致(见下方 test_nav_matches_content 校验)。
#
# 四档可见性:
#   - 普通用户(normal_user):both + NORMAL_ONLY,看不到任何代理/管理员文档
#   - 一级代理(agent / L1):both + AGENT_ONLY,看不到 L2 专属、普通专属、管理员文档
#   - 二级代理(l2):both + AGENT_ONLY + L2_ONLY,看不到普通专属、管理员文档
#   - 管理员(admin):全部
AGENT_ONLY_DOC_SLUGS = frozenset({
    'provider-guide', 'leads',
    # 服务商经营后台。客户、报价、对外品牌和团队能力是两种账号共享的。
    'stock-up', 'set-pricing', 'profit',
    # 服务商钱包 / 银行卡 / 结算 / 推广 / 协议
    'agent-wallet', 'wallet-bank-cards', 'agent-settlement', 'agent-promotion', 'agent-agreement',
})
# 服务费流水是二级代理专属:一级代理也看不到
L2_ONLY_DOC_SLUGS = frozenset({'wallet-service-fee-history'})
# 客户额度页是普通用户专属:代理侧 hideForAgent,代理看不到
NORMAL_ONLY_DOC_SLUGS = frozenset({'standard-guide', 'customer-wallet', 'customer-recharge'})
ADMIN_ONLY_DOC_SLUGS = frozenset({'team-members', 'roles', 'audit-log'})


def _hidden_doc_slugs(identity: str) -> list[str]:
    """按真实身份返回不允许访问的文档 slug。"""
    if identity == 'admin':
        return []
    if identity == 'l2':  # 二级代理:看不到普通专属 + 管理员
        return sorted(NORMAL_ONLY_DOC_SLUGS | ADMIN_ONLY_DOC_SLUGS)
    if identity == 'agent':  # 一级代理:再加上 L2 专属也看不到
        return sorted(NORMAL_ONLY_DOC_SLUGS | L2_ONLY_DOC_SLUGS | ADMIN_ONLY_DOC_SLUGS)
    # normal_user:代理专属 + L2专属 + 管理员都看不到(但能看 NORMAL_ONLY)
    return sorted(AGENT_ONLY_DOC_SLUGS | L2_ONLY_DOC_SLUGS | ADMIN_ONLY_DOC_SLUGS)


@router.get("/api/help/docs/acl", summary="帮助文档身份白名单(当前身份能看哪些 slug)")
async def api_help_docs_acl(request: Request):
    user = _require_user(request)
    identity = _resolve_identity(user)
    return {
        "role": identity,
        "hidden_slugs": _hidden_doc_slugs(identity),
        "agent_only_slugs": sorted(AGENT_ONLY_DOC_SLUGS),
        "l2_only_slugs": sorted(L2_ONLY_DOC_SLUGS),
        "normal_only_slugs": sorted(NORMAL_ONLY_DOC_SLUGS),
        "admin_only_slugs": sorted(ADMIN_ONLY_DOC_SLUGS),
    }


# 帮助文档正文真隔离:正文存在后端 JSON,按真实身份返回。普通用户请求代理 slug → 403,
# 浏览器/bundle 里根本没有代理正文。
_HELP_DOCS_PATH = os.path.join(os.path.dirname(__file__), "help_docs_content.json")
_help_docs_cache = None


def _load_help_docs() -> dict:
    global _help_docs_cache
    if _help_docs_cache is None:
        try:
            with open(_HELP_DOCS_PATH, encoding="utf-8-sig") as f:
                _help_docs_cache = json.load(f)
        except Exception as e:
            logger.error("[help] 加载帮助文档正文失败: %s", e)
            _help_docs_cache = {}
    return _help_docs_cache


@router.get("/api/help/docs/{slug}", summary="帮助文档正文(后端按真实身份返回 · 真隔离)")
async def api_help_doc_body(slug: str, request: Request):
    user = _require_user(request)
    identity = _resolve_identity(user)
    # 身份不够直接 403,正文不下发(普通用户拿不到代理/管理员正文)
    if slug in _hidden_doc_slugs(identity):
        raise HTTPException(status_code=403, detail="无权访问该帮助文档")
    doc = _load_help_docs().get(slug)
    if not doc:
        raise HTTPException(status_code=404, detail="帮助文档不存在")
    return {"slug": slug, "title": doc.get("title"), "body": doc.get("body", "")}


# ==========================================
# 用户侧端点
# ==========================================

@router.get("/api/faq/items", summary="拿 FAQ 列表(已上架 · 按身份过滤)")
async def api_list_faq(request: Request, category: Optional[str] = None):
    user = _require_user(request)
    if category is not None and category not in _CATEGORIES:
        raise HTTPException(status_code=400, detail=f"category 无效 · 必须是 {_CATEGORIES}")
    # 后端真实身份过滤:普通用户拿不到代理专属 FAQ(不信前端传参)
    identity = _resolve_identity(user)
    items = list_faq_items(
        user_id=current_user_id(user),
        category=category,
        include_unpublished=False,
        identity=identity,
    )
    return {"items": items}


@router.post("/api/faq/vote", summary="投票 / 切换 / 撤销")
async def api_vote(request: Request, body: VoteRequest):
    user = _require_user(request)
    if body.vote is not None and body.vote not in ('up', 'down'):
        raise HTTPException(status_code=400, detail="vote 必须是 up / down / null")
    item = get_faq_item(body.faq_id)
    if not item:
        raise HTTPException(status_code=404, detail="FAQ 不存在")
    result = set_vote(body.faq_id, current_user_id(user), body.vote)
    return result  # {thumbs_up, thumbs_down, my_vote}


@router.post("/api/faq/feedback", summary="提交反馈")
async def api_submit_feedback(request: Request, body: FeedbackRequest, background_tasks: BackgroundTasks):
    user = _require_user(request)
    if body.urgency not in _URGENCIES:
        raise HTTPException(status_code=400, detail=f"urgency 无效 · 必须是 {_URGENCIES}")
    if body.kind not in _FEEDBACK_KINDS:
        raise HTTPException(status_code=400, detail=f"kind 无效 · 必须是 {_FEEDBACK_KINDS}")
    if body.faq_id is not None:
        item = get_faq_item(body.faq_id)
        if not item:
            raise HTTPException(status_code=404, detail="关联 FAQ 不存在")
    submitter_identity = _resolve_identity(user)
    submitter_agent_level = _agent_level(user)
    result = create_feedback(
        client_id=body.client_id,
        user_id=_user_id(user),
        message=body.message,
        urgency=body.urgency,
        contact=body.contact,
        faq_id=body.faq_id,
        kind=body.kind,
        screenshot_url=body.screenshot_url,
        ai_answer=body.ai_answer,
        submitter_identity=submitter_identity,
        submitter_agent_level=submitter_agent_level,
    )
    if body.kind == "bug" and result.get("status") == "new":
        _notify_admins_for_bug(result.get("id", 0), body.message, body.urgency, _user_id(user))
        # AI Ops 自动建诊断任务 hook(DB flag ai_ops.auto_create_from_feedback 或
        # env AI_OPS_AUTO_CREATE_FROM_FEEDBACK 任一开启即生效 · 默认全关)。
        # BackgroundTasks + sync 函数 → FastAPI 在响应后于线程池执行,
        # flag/DB 读取不再阻塞事件循环(包A 复审 P3);内部已 try/except 兜底。
        try:
            from services.ai_ops.task_service import maybe_create_ai_ops_task_for_feedback
            background_tasks.add_task(
                maybe_create_ai_ops_task_for_feedback,
                result.get("id", 0), body.kind, result.get("status"),
            )
        except Exception as _e:  # noqa: BLE001
            logger.warning("[faq] ai_ops 自动建任务 hook 失败(不影响提交): %s", _e)
    return result  # {id, status: 'new'|'existing'}


@router.post("/api/faq/feedback/screenshot", summary="上传问题反馈截图")
async def api_upload_feedback_screenshot(
    request: Request,
    file: UploadFile = File(...),
):
    user = _require_user(request)
    content_type = (file.content_type or "").lower()
    if content_type not in _BUG_SCREENSHOT_ALLOWED_TYPES:
        raise HTTPException(status_code=415, detail="只支持 JPG/PNG/WebP/GIF/BMP 图片")
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="截图为空")
    if len(raw) > _BUG_SCREENSHOT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="截图不能超过 10MB")

    suffix = (file.filename or "").rsplit(".", 1)[-1].lower() if "." in (file.filename or "") else ""
    if not suffix:
        suffix = content_type.split("/", 1)[-1]
    try:
        from services.oss_service import build_bug_feedback_key, upload_bug_feedback_image
        key = build_bug_feedback_key(_user_id(user), suffix)
        stored_key = upload_bug_feedback_image(key, raw, content_type=content_type)
        return {"ok": True, "screenshot_url": stored_key}
    except Exception as e:
        logger.error("[faq] 上传问题反馈截图失败: %s", e)
        raise HTTPException(status_code=503, detail="截图上传失败, 请稍后再试")


# ==========================================
# 管理员侧 · FAQ CRUD
# ==========================================

@router.get("/api/admin/faq/items", summary="管理员 · 列出全部 FAQ(含未上架)")
async def admin_list_faq(request: Request, category: Optional[str] = None):
    admin = _require_admin(request)
    if category is not None and category not in _CATEGORIES:
        raise HTTPException(status_code=400, detail=f"category 无效")
    items = list_faq_items(
        user_id=_user_id(admin),
        category=category,
        include_unpublished=True,
    )
    return {"items": items}


@router.get("/api/admin/faq/items/{faq_id}", summary="管理员 · 拿单条 FAQ(编辑页 fetch)")
async def admin_get_faq(faq_id: int, request: Request):
    _require_admin(request)
    item = get_faq_item(faq_id)
    if not item:
        raise HTTPException(status_code=404, detail="FAQ 不存在")
    return item


# ── [R3-P7 ①] 术语门 · API 前置校验 ─────────────────────────────────────
# 与 db/kb_db.py 的 writer 门**同一份实现**(services.kb_terminology_gate),
# 不各写一套 —— 两份必然各自漂移,而漂移方向一定是"用户碰得到的那份更松"。
#
# 两道门目的不同,缺一不可:
#   · writer 门是收口点,保证**任何**写入路径都过闸(fail-closed);
#   · 这道前置门保证管理员在**提交那一刻**就拿到「哪个词、改成什么」,
#     而不是提交成功 → 后台重建静默失败 → 下次问小榜才发现。
#     (`_trigger_xiaobang_faq_reindex` 是 fire-and-forget,失败只记 warning。)
def _assert_faq_terminology_clean(question, answer_md, *, entering_release: bool) -> None:
    """[R3-P8 ⑤] 门的形态:**草稿可存 · violating release 禁激活**。

    R3-P7 的形态是「任何含旧词的保存一律 400」。那条太硬:管理员改一条长 FAQ
    改到一半想先存下来,会被门顶回去 —— 门于是变成"别用草稿"的压力,
    而压力最后会落在把门关掉上。

    现在按「会不会进 release」分档:

    * ``is_published=False``(草稿)—— **放行**。它不进索引
      (``build_faq_chunks`` 的 SQL 明写 ``WHERE is_published = TRUE``),
      所以存一份带旧词的草稿对用户零影响;
    * ``is_published=True``(要进 release)—— **拒绝**,并给出带
      ``rule_id``/``version`` 的错误合同。

    第二道防线仍在 ``db/kb_db.py`` 的 writer 上(fail-closed):即使有人绕过本 API
    把违规行标成 published,重建也会在 **DELETE 之前**被拒 ⇒
    **违规 release 不会被激活,上一 release 保持 active**。
    """
    if not entering_release:
        return
    from services.kb_terminology_gate import (
        KbTerminologyViolation,
        assert_kb_text_clean,
        violation_contract,
    )

    blob = (question or "") + chr(10) + (answer_md or "")
    try:
        assert_kb_text_clean(blob, where="faq_api")
    except KbTerminologyViolation as exc:
        raise HTTPException(status_code=400, detail=violation_contract(
            exc,
            message="这条 FAQ 用了已经废弃的说法,发布出去会让小榜照着念错。"
                    "可以先存成草稿(取消勾选「已发布」),改好再发布。",
        ))

def _as_terminology_400(exc) -> HTTPException:
    """把 writer(``db/faq_db.py`` 同事务那道)抛的违规翻成与前门**同一份**合同。

    前门已经拦过一遍,writer 那道只在**前门被绕过**时才触发 —— 并发竞态
    (读写不在一个事务里)、或者别的调用方直接调 writer。那种情况下回 500
    等于把"门起作用了"报成"服务器坏了",下一个人只会来把门关掉。
    """
    from services.kb_terminology_gate import violation_contract

    return HTTPException(status_code=400, detail=violation_contract(
        exc,
        message="这条 FAQ 用了已经废弃的说法,发布出去会让小榜照着念错。"
                "可以先存成草稿(取消勾选「已发布」),改好再发布。",
    ))


@router.post("/api/admin/faq/items", status_code=201, summary="管理员 · 新建 FAQ")
async def admin_create_faq(request: Request, body: CreateFAQRequest):
    admin = _require_admin(request)
    if body.category not in _CATEGORIES:
        raise HTTPException(status_code=400, detail=f"category 无效")
    if body.visible_to not in _VISIBLE_TO:
        raise HTTPException(status_code=400, detail=f"visible_to 无效 · 必须是 {_VISIBLE_TO}")
    _assert_faq_terminology_clean(body.question, body.answer_md,
                                  entering_release=bool(body.is_published))
    from services.kb_terminology_gate import KbTerminologyViolation

    try:
        new_id = create_faq_item(
            question=body.question,
            category=body.category,
            answer_md=body.answer_md,
            sort_order=body.sort_order,
            is_published=body.is_published,
            updated_by=_user_id(admin),
            visible_to=body.visible_to,
        )
    except KbTerminologyViolation as exc:
        raise _as_terminology_400(exc)
    _trigger_xiaobang_faq_reindex()
    return {"id": new_id}


@router.patch("/api/admin/faq/items/{faq_id}", summary="管理员 · 部分更新 FAQ")
async def admin_update_faq(faq_id: int, request: Request, body: UpdateFAQRequest):
    admin = _require_admin(request)
    if body.category is not None and body.category not in _CATEGORIES:
        raise HTTPException(status_code=400, detail=f"category 无效")
    if body.visible_to is not None and body.visible_to not in _VISIBLE_TO:
        raise HTTPException(status_code=400, detail=f"visible_to 无效 · 必须是 {_VISIBLE_TO}")
    existing = get_faq_item(faq_id)
    if not existing:
        raise HTTPException(status_code=404, detail="FAQ 不存在")
    # PATCH 是部分更新:未传的字段沿用库里的旧值,门要打**合并后**的文本。
    # 只打 body 的话,「把干净标题改成脏正文」这种改法会从缝里过去。
    # PATCH 半档都要按**合并后**的值判:未传的字段沿用库里的旧值。
    # `is_published` 同理 —— 只传脏正文、不传 is_published 的 PATCH,
    # 若库里那条本来就是 published,它就是在改一份**在线**的 release。
    _assert_faq_terminology_clean(
        body.question if body.question is not None else existing.get("question"),
        body.answer_md if body.answer_md is not None else existing.get("answer_md"),
        entering_release=bool(
            body.is_published if body.is_published is not None
            else existing.get("is_published")),
    )
    from services.kb_terminology_gate import KbTerminologyViolation

    try:
        changed = update_faq_item(
            faq_id,
            question=body.question,
            answer_md=body.answer_md,
            category=body.category,
            sort_order=body.sort_order,
            is_published=body.is_published,
            updated_by=_user_id(admin),
            visible_to=body.visible_to,
        )
    except KbTerminologyViolation as exc:
        raise _as_terminology_400(exc)
    if changed:
        _trigger_xiaobang_faq_reindex()
    return {"ok": changed}


@router.delete("/api/admin/faq/items/{faq_id}", summary="管理员 · 删除 FAQ")
async def admin_delete_faq(faq_id: int, request: Request):
    _require_admin(request)
    deleted = delete_faq_item(faq_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="FAQ 不存在")
    _trigger_xiaobang_faq_reindex()
    return {"ok": True}


class ReorderRequest(BaseModel):
    ids: list[int] = Field(..., min_length=1, max_length=200, description="按目标顺序排好的 FAQ id 列表")


@router.post("/api/admin/faq/items/reorder", summary="管理员 · 批量拖拽排序")
async def admin_reorder(request: Request, body: ReorderRequest):
    _require_admin(request)
    updated = reorder_items(body.ids)
    return {"ok": True, "updated": updated}


# ==========================================
# 管理员侧 · 反馈
# ==========================================

@router.get("/api/faq/feedback/mine", summary="提交者 · 我的反馈状态")
async def api_my_feedback(request: Request, limit: int = 50):
    """提交者查**自己**提过的反馈及其处理状态。

    🔴 [#96 2026-09-05] 在此之前:能提交、能去重、能结单,但**结单之后**
    `user_notifications` 一条不写、提交者也没有任何查询出口 ——
    她提完就再也看不到下文,只能当石沉大海。

    🔴 **不返回 `admin_note`**:那是内部备注,不是给她的回复。
    「公开回复」要一列独立字段,本单没加(见工单回报);
    拿内部备注冒充回复是内部口径外泄,而且不会有任何东西报警。
    """
    user = _require_user(request)
    uid = _user_id(user)
    if not uid:
        raise HTTPException(status_code=401, detail="未登录")
    from db.faq_db import list_feedback_for_user
    items = list_feedback_for_user(uid, limit=max(1, min(int(limit or 50), 200)))
    return {"items": items, "total": len(items)}


@router.get("/api/admin/faq/feedback", summary="管理员 · 反馈列表")
async def admin_list_feedback(
    request: Request,
    status: Optional[str] = None,
    urgency: Optional[str] = None,
    kind: Optional[str] = None,
    limit: int = 100,
):
    _require_admin(request)
    if status is not None and status not in _STATUSES:
        raise HTTPException(status_code=400, detail=f"status 无效")
    if urgency is not None and urgency not in _URGENCIES:
        raise HTTPException(status_code=400, detail=f"urgency 无效")
    if kind is not None and kind not in _FEEDBACK_KINDS:
        raise HTTPException(status_code=400, detail=f"kind 无效")
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=400, detail="limit 范围 1-500")
    items = list_feedback(status=status, urgency=urgency, kind=kind, limit=limit)
    items = [_sign_feedback_screenshot(dict(item)) for item in items]
    return {"items": items}


@router.patch("/api/admin/faq/feedback/{feedback_id}", summary="管理员 · 改反馈状态 / 写备注")
async def admin_update_feedback(
    feedback_id: int,
    request: Request,
    body: UpdateFeedbackRequest,
):
    admin = _require_admin(request)
    if body.status is not None and body.status not in _STATUSES:
        raise HTTPException(status_code=400, detail=f"status 无效")
    if body.status is None and body.admin_note is None:
        raise HTTPException(status_code=400, detail="至少要改 status 或 admin_note 之一")
    ok = update_feedback_status(
        feedback_id,
        status=body.status,
        admin_note=body.admin_note,
        handled_by=_user_id(admin),
    )
    if not ok:
        raise HTTPException(status_code=404, detail="反馈不存在")
    return {"ok": True}


@router.get("/api/admin/faq/feedback/counts", summary="管理员 · 各状态反馈数量")
async def admin_feedback_counts(
    request: Request,
    kind: Optional[str] = None,
    urgency: Optional[str] = None,
):
    _require_admin(request)
    if kind is not None and kind not in _FEEDBACK_KINDS:
        raise HTTPException(status_code=400, detail=f"kind 无效")
    if urgency is not None and urgency not in _URGENCIES:
        raise HTTPException(status_code=400, detail="urgency 无效")
    return get_feedback_counts(kind=kind, urgency=urgency)
