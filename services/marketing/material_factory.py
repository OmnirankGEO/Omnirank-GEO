"""
services/marketing/material_factory.py — 物料工厂编排(机器闸门制全链 · §0 / §D.2)

管线(每一步都可测):
  ① 生成前守卫(违禁词/承诺词/广告法/竞品清洗)· **拦截发生在冻结前 = 零扣费**(job=blocked · 无 freeze)
  ② 日上限实时 COUNT(超限 → blocked · 零扣费)
  ③ freeze_points(job 创建后 · 守卫过了才冻)——只调 middleware.billing 公共接口(红线 diff=0)
  ④ 生成:V1 朋友圈文案(LLM)/ V2 海报(GPT-Image-2 直出 → 落自有存储)/ V3 场景套装(多尺寸多 asset)
     重试 ≤2;每次 attempt 落库(含 safety_status)
  ⑤ 首个成功 asset → commit_freeze;全部 attempts 失败 → release_freeze(自动退)
  ⑥ 用户预览自审(is_final)+ 外发风控三态(publish_allowed/rights_confirmed);军师 platform 物料挂 case_id 过审批

计费:全走现有 freeze/commit/release(billing 红线 diff=0);定价读 feature_pricing(mktg_* 已 seed)。
"""
import logging
from typing import Optional

from db import marketing_db
from services.marketing.guards import scan_forbidden, clean_competitors
from services.marketing import image_client, material_storage

logger = logging.getLogger("GEO-Marketing-Factory")

# 固定尾缀(§2 实测抓到的坑:模型自发画 logo)
PROMPT_TAIL = ("Render all quoted text verbatim, no extra words, no duplicate text, "
               "accurate Chinese characters, elegant Chinese typography, "
               "no third-party brand logos or trademarks.")

# material_kind + resolution → feature_code(feature_pricing 已 seed)
def _resolve_feature_code(material_kind: str, resolution: str, template: Optional[dict]) -> str:
    if template and template.get("feature_code"):
        return template["feature_code"]
    if material_kind == "copy":
        return "mktg_moments_copy"
    if material_kind == "bundle":
        return "mktg_bundle_pro" if resolution in ("2k", "4k") else "mktg_bundle_std"
    return "mktg_poster_pro" if resolution in ("2k", "4k") else "mktg_poster_basic"


# 结构化字段 → 违禁词扫描目标文本
_TEXT_FIELDS = ("title", "subtitle", "date", "contact", "selling_point", "body", "cta")


def _gather_text(input_fields: dict) -> str:
    return " ".join(str(input_fields.get(k, "")) for k in _TEXT_FIELDS if input_fields.get(k))


def pre_guard(input_fields: dict, competitors: Optional[list] = None) -> dict:
    """① 生成前守卫。返回 {passed, block_reason?, flags?}。拦截即零扣费(调用方不 freeze)。"""
    text = _gather_text(input_fields)
    r = scan_forbidden(text, competitors=competitors)
    if not r["passed"]:
        return {"passed": False, "block_reason": "forbidden_content", "flags": r["flags"]}
    return {"passed": True}


def _build_prompt(template: Optional[dict], input_fields: dict) -> str:
    """② prompt 装配:英文骨架 + 引号包中文原文 + 固定尾缀(§2)。竞品已清洗。"""
    skeleton = (template or {}).get("prompt_skeleton") or \
        "A clean, modern Chinese marketing poster, elegant layout, brand-green accents."
    lines = [skeleton]
    if input_fields.get("title"):
        lines.append(f"Main title Chinese text: 「{input_fields['title']}」")
    if input_fields.get("subtitle"):
        lines.append(f"Subtitle: 「{input_fields['subtitle']}」")
    if input_fields.get("date"):
        lines.append(f"Date line: 「{input_fields['date']}」")
    if input_fields.get("contact"):
        lines.append(f"Contact line: 「{input_fields['contact']}」")
    lines.append(PROMPT_TAIL)
    return "\n".join(lines)


# 营销目的 → 人话(文案 prompt 用;前端两套目的清单与此同源:选客户=替他生意,选自己=推广服务)
_PURPOSE_LABELS = {
    "recharge": "吸引新用户完成首次充值", "repurchase": "唤回老客户再次使用或充值",
    "restock": "提醒服务商补充算力库存", "notice": "把新活动或权益告诉客户",
    "opening": "客户门店开业/上新造势", "promo": "客户促销活动宣传",
    "invite": "邀请顾客参加线下活动", "daily": "日常专业形象内容(轻分享,不硬卖)",
    "acquire": "推广自己的 AI 搜索优化服务获客", "callback": "唤回自己的沉默老客户",
}


async def _gen_copy(input_fields: dict, competitors: Optional[list],
                    purpose: str = "") -> Optional[str]:
    """V1 朋友圈文案(LLM · 过出口守卫)。fail-soft。

    [P0-B 批 2026-07-05] 升级:走技能包装配的 system prompt(金枪大叔标题/舒老师口语,
    Codex 蒸馏包落库后自动变聪明)+ 营销目的直通;数字/事实仍全部来自 input_fields。"""
    purpose_line = f"营销目的:{_PURPOSE_LABELS.get(purpose, purpose)}\n" if purpose else ""
    prompt = (
        "为下面的品牌写一条自然口语、不含任何承诺词、对客用'算力'不用'积分'的朋友圈营销文案"
        "(≤120字,短句,像跟朋友说话,以一个明确的动作收尾):\n"
        f"{purpose_line}"
        f"标题:{input_fields.get('title','')}\n卖点:{input_fields.get('selling_point','')}\n"
        f"联系方式:{input_fields.get('contact','')}"
    )
    try:
        from services.marketing.skill_packs import assemble_system_prompt
        sp = assemble_system_prompt(rule_key="material_copy", task_hint="朋友圈营销文案")
        prompt = f"{sp}\n\n{prompt}"
    except Exception as e:  # noqa: BLE001 · 技能包装配挂了不挡文案(退回裸 prompt)
        logger.warning("[factory] 技能包装配失败(用裸 prompt): %s", e)
    try:
        from tools.multi_llm_caller import call_llm_with_fallback
        text = await call_llm_with_fallback(prompt, verbose=False)
    except Exception as e:  # noqa: BLE001
        logger.warning("[factory] 文案 LLM 失败: %s", e)
        return None
    if not text:
        return None
    text = clean_competitors(text, competitors)
    r = scan_forbidden(text, competitors=competitors)
    if r["flags"]:  # 任一守卫命中(含 advisory)弃 LLM 版,内部 fail-safe,非用户面硬拦
        logger.warning("[factory] 文案出口守卫拦截: %s", r["flags"])
        return None
    return text.strip()[:600]


async def _gen_poster(job: dict, template: Optional[dict], input_fields: dict,
                      size: str, resolution: str, owner_key: str) -> Optional[dict]:
    """V2 海报:GPT-Image-2 直出 → 下载 → 落自有存储。重试 ≤2。返回 asset 存储信息 / None。"""
    prompt = _build_prompt(template, input_fields)
    marketing_db.update_job(job["id"], final_prompt=prompt)
    for attempt_no in range(1, 4):  # ≤3 次(重试 ≤2)
        att = marketing_db.add_attempt(job_id=job["id"], attempt_no=attempt_no, status="running")
        res = await image_client.generate_image(prompt, size=size, resolution=resolution)
        if not res.get("ok"):
            # [返工 R7] 轮询失败≠安全拦截:safety_status 保持 passed,失败原因在 error_detail
            #(真正的内容安全拦截由 provider 审核/出口守卫另行标 blocked_*)
            marketing_db.finish_attempt(att["id"], status="failed",
                                        provider_cost_usd=res.get("cost_usd", 0.0),
                                        safety_status="passed",
                                        error_detail=res.get("error", ""))
            continue
        img_bytes = await image_client.download_image(res["image_url"])
        if not img_bytes:
            marketing_db.finish_attempt(att["id"], status="failed",
                                        provider_cost_usd=res.get("cost_usd", 0.0),
                                        error_detail="download_failed")
            continue
        stored = material_storage.save_material_image(owner_key, img_bytes, "poster.png")
        marketing_db.finish_attempt(att["id"], status="succeeded",
                                    provider_cost_usd=res.get("cost_usd", 0.0), safety_status="passed")
        stored["provider_url"] = res["image_url"]
        return stored
    return None


async def prepare_material_job(*, user_id: int, template_id: Optional[int], input_fields: dict,
                               material_kind: str = "poster", brand_id: Optional[int] = None,
                               owner_scope: str = "user", case_id: Optional[int] = None,
                               size: Optional[str] = None, resolution: Optional[str] = None,
                               competitors: Optional[list] = None, purpose: str = "") -> dict:
    """[返工 R4] 闸门+冻结段(请求内同步完成 · 扣费确定性)。
    blocked/failed 即终态;"generating" 则带 _ctx 交 execute_material_job(可后台)。"""
    template = marketing_db.get_template(template_id) if template_id else None
    if template:
        material_kind = template.get("material_kind", material_kind)
        size = size or template.get("default_size", "3:4")
        resolution = resolution or template.get("default_resolution", "1k")
    size = size or "3:4"
    resolution = resolution or "1k"
    feature_code = _resolve_feature_code(material_kind, resolution, template)

    # 平台(军师)物料必须挂 case_id 过审批
    if owner_scope == "platform" and not case_id:
        return {"status": "error", "error": "platform_material_needs_case"}

    # ② 日上限(零扣费拦截)
    daily = marketing_db.get_config_int("marketing.material.daily_limit", 20)
    if marketing_db.count_jobs_today(user_id) >= daily:
        return {"status": "blocked", "block_reason": "daily_limit", "limit": daily}

    # ① 生成前守卫(零扣费拦截)
    guard = pre_guard(input_fields, competitors=competitors)
    if not guard["passed"]:
        job = marketing_db.create_material_job(
            user_id=user_id, owner_scope=owner_scope, brand_id=brand_id, case_id=case_id,
            template_id=template_id, material_kind=material_kind, feature_code=feature_code,
            input_fields=input_fields, size=size, resolution=resolution)
        marketing_db.update_job(job["id"], status="blocked", block_reason=guard["block_reason"])
        return {"status": "blocked", "job_id": job["id"], "block_reason": guard["block_reason"],
                "flags": guard.get("flags")}

    # 建 job → freeze(守卫过了才冻)
    job = marketing_db.create_material_job(
        user_id=user_id, owner_scope=owner_scope, brand_id=brand_id, case_id=case_id,
        template_id=template_id, material_kind=material_kind, feature_code=feature_code,
        input_fields=input_fields, size=size, resolution=resolution)
    task_ref = f"mktg_factory_{job['id']}"
    marketing_db.update_job(job["id"], billing_ref=task_ref, status="generating")

    freeze_id = None
    try:
        from middleware.billing import freeze_points
        fr = await freeze_points(user_id=user_id, feature_code=feature_code,
                                 task_ref=task_ref, brand_id=brand_id, reason="营销物料工厂")
        freeze_id = fr.get("freeze_id")
        frozen_amount = fr.get("amount", 0)
    except Exception as e:  # noqa: BLE001 · 402 余额不足等
        marketing_db.update_job(job["id"], status="failed", error_summary=f"freeze_failed: {e}")
        return {"status": "failed", "job_id": job["id"], "error": f"freeze_failed: {str(e)[:100]}"}
    try:
        # [返工 R4/M-4] freeze 成功后的 job 回写若失败,不许留下"冻而不管"路径:
        # freeze_id 已在内存 ctx,结算仍会 commit/release;此处失败仅损失台账字段,吞掉继续。
        marketing_db.update_job(job["id"], freeze_id=freeze_id or 0, cost_points=frozen_amount)
    except Exception as e:  # noqa: BLE001
        logger.warning("[factory] freeze 后回写 job 失败(继续,结算不受影响)job=%s: %s", job["id"], e)

    owner_key = f"u{user_id}" if owner_scope == "user" else f"p{case_id or job['id']}"
    return {"status": "generating", "job_id": job["id"],
            "_ctx": {"job": job, "template": template, "input_fields": input_fields,
                     "material_kind": material_kind, "size": size, "resolution": resolution,
                     "competitors": competitors, "freeze_id": freeze_id,
                     "user_id": user_id, "owner_key": owner_key, "purpose": purpose}}


def _alert_commit_failed(job_id: int, err: Exception) -> None:
    """[返工 R4] commit_freeze 失败必须进告警管道(不许静默"靠对账"——
    freeze_sweeper 12h 后只会反向 release=平台白送,须人工介入)。fail-soft。"""
    try:
        from db import ai_ops_db
        ai_ops_db.upsert_alert("marketing_anomaly", severity="critical",
                               title="物料工厂 commit_freeze 失败(资产已交付未扣费)",
                               detail=f"job={job_id} · {str(err)[:200]} · sweeper 12h 后会自动退款,须在此之前人工 commit",
                               fingerprint=f"mktg_commit_failed:{job_id}",
                               payload={"job_id": job_id})
    except Exception as e:  # noqa: BLE001
        logger.warning("[factory] 写 commit 失败告警失败(降级): %s", e)


async def _settle_job(job_id: int, ctx: dict, assets: list, expected: int) -> dict:
    """[返工 R4] 结算必达。口径(R6-3 拍板):全部 slot 成功→commit;
    部分成功→release(整单免单 · 已成成品保留给用户,"做成才扣"字面兑现);全败→release+failed。
    billing 调用用 asyncio.shield + except BaseException:蓝绿重启 CancelledError 也尽力结算完
    (进程直接 kill 由 freeze_sweeper 12h 兜底,但兜底不是主路径)。"""
    import asyncio
    from middleware.billing import commit_freeze, release_freeze
    freeze_id, user_id = ctx.get("freeze_id"), ctx["user_id"]
    n = len(assets)
    if n >= expected and n > 0:
        try:
            if freeze_id:
                await asyncio.shield(commit_freeze(freeze_id=freeze_id,
                                                   reason="营销物料生成完成", user_id=user_id))
        except BaseException as e:  # noqa: BLE001 · 含 CancelledError
            logger.warning("[factory] commit_freeze 失败 job=%s: %s", job_id, e)
            _alert_commit_failed(job_id, e if isinstance(e, Exception) else RuntimeError(str(e)))
        marketing_db.update_job(job_id, status="succeeded")
        return {"status": "succeeded", "job_id": job_id, "assets": assets}
    if n > 0:
        try:
            if freeze_id:
                await asyncio.shield(release_freeze(freeze_id=freeze_id,
                                                    reason="套装部分生成成功,整单免单退费", user_id=user_id))
        except BaseException as e:  # noqa: BLE001
            logger.warning("[factory] 部分免单 release 失败 job=%s: %s", job_id, e)
        marketing_db.update_job(job_id, status="succeeded", error_summary="partial_free_release")
        return {"status": "succeeded", "job_id": job_id, "assets": assets, "partial_free": True}
    try:
        if freeze_id:
            await asyncio.shield(release_freeze(freeze_id=freeze_id,
                                                reason="营销物料生成失败退费", user_id=user_id))
    except BaseException as e:  # noqa: BLE001
        logger.warning("[factory] release_freeze 失败 job=%s: %s", job_id, e)
    marketing_db.update_job(job_id, status="failed", error_summary="all_attempts_failed")
    return {"status": "failed", "job_id": job_id, "error": "all_attempts_failed"}


async def execute_material_job(ctx: dict) -> dict:
    """[返工 R4] 生成+结算段(可 asyncio.create_task 后台跑)。任何路径必达 job 终态:
    生成段异常被吞 → finally 结算(commit/release)→ 不存在永挂 generating 的出口。"""
    job, template = ctx["job"], ctx["template"]
    input_fields, competitors = ctx["input_fields"], ctx["competitors"]
    material_kind, size, resolution = ctx["material_kind"], ctx["size"], ctx["resolution"]
    owner_key = ctx["owner_key"]
    assets: list = []
    expected = 1
    result: dict = {}
    try:
        if material_kind == "copy":
            text = await _gen_copy(input_fields, competitors, ctx.get("purpose", ""))
            if text:
                a = marketing_db.add_asset(job_id=job["id"], asset_kind="copy",
                                           content_text=text, is_final=True, rights_confirmed=0)
                assets.append(a)
        elif material_kind == "bundle":
            # V3 场景套装:多尺寸多 asset(同一 job)
            specs = (template or {}).get("bundle_spec_jsonb") or [
                {"slot": "moments", "size": "1:1"}, {"slot": "rollup", "size": "9:16"},
                {"slot": "invitation", "size": "3:4"}]
            expected = len(specs)
            for spec in specs:
                stored = await _gen_poster(job, template, input_fields, spec.get("size", size),
                                           resolution, owner_key)
                if stored:
                    a = marketing_db.add_asset(
                        job_id=job["id"], asset_kind="bundle_item", bundle_slot=spec.get("slot", ""),
                        url_provider=stored.get("provider_url", ""), url_stored=stored["public_url"],
                        thumbnail_url=stored.get("thumbnail_url", ""), width=stored["width"],
                        height=stored["height"], size_bytes=stored["size_bytes"],
                        sha256=stored["sha256"], is_final=True, rights_confirmed=0)
                    assets.append(a)
        else:  # poster (V2)
            stored = await _gen_poster(job, template, input_fields, size, resolution, owner_key)
            if stored:
                a = marketing_db.add_asset(
                    job_id=job["id"], asset_kind="poster", url_provider=stored.get("provider_url", ""),
                    url_stored=stored["public_url"], thumbnail_url=stored.get("thumbnail_url", ""),
                    width=stored["width"], height=stored["height"], size_bytes=stored["size_bytes"],
                    sha256=stored["sha256"], is_final=True, rights_confirmed=0)
                assets.append(a)
    except Exception as e:  # noqa: BLE001
        logger.warning("[factory] 生成异常 job=%s: %s", job["id"], e)
    finally:
        result = await _settle_job(job["id"], ctx, assets, expected)
    return result


async def run_material_job(*, user_id: int, template_id: Optional[int], input_fields: dict,
                           material_kind: str = "poster", brand_id: Optional[int] = None,
                           owner_scope: str = "user", case_id: Optional[int] = None,
                           size: Optional[str] = None, resolution: Optional[str] = None,
                           competitors: Optional[list] = None, purpose: str = "") -> dict:
    """物料工厂全链(prepare→execute 同步串行)。同步语义调用方/测试用;
    API 层用 prepare_material_job + asyncio.create_task(execute_material_job) 立即返回 job_id(R4)。"""
    prep = await prepare_material_job(
        user_id=user_id, template_id=template_id, input_fields=input_fields,
        material_kind=material_kind, brand_id=brand_id, owner_scope=owner_scope,
        case_id=case_id, size=size, resolution=resolution, competitors=competitors,
        purpose=purpose)
    if prep.get("status") != "generating":
        return prep
    return await execute_material_job(prep["_ctx"])


# ============================================================================
# 首版占位模板(§D.5 · 先放 3 条跑通链路;prompt_skeleton 由策划 CTO 用乔木 Mondo/金枪大叔标准替换)
# ============================================================================
_DEFAULT_TEMPLATES = [
    {
        "template_code": "mktg_moments_copy_v1", "scene_type": "moments", "material_kind": "copy",
        "name": "朋友圈文案(通用)", "feature_code": "mktg_moments_copy",
        "prompt_skeleton": "", "default_size": "1:1", "default_resolution": "1k",
        "style_tags": ["口语", "简洁"], "sort": 10,
    },
    # —— v1 骨架(2026-07-05 策划 CTO 按乔木 Mondo 蒸馏原则亲研:限色丝网印/显式留白文字区/
    #    符号化禁真人脸/纸纹质感;详见《海报提示词与LLM链路_研究_2026-07-05.md》§2)——
    {
        "template_code": "mktg_poster_salon_v1", "scene_type": "salon", "material_kind": "poster",
        "name": "线下沙龙会议海报", "feature_code": "mktg_poster_pro",
        "prompt_skeleton": ("A premium Chinese business salon poster, Mondo-inspired minimalist composition, "
                            "3-color screen print aesthetic: deep forest green, cream white, brand green #6CBE1E accents. "
                            "Centered symbolic motif: two elegant chairs facing each other under a soft spotlight, "
                            "symbolizing dialogue. Large clean negative space in upper third reserved for the main title, "
                            "refined editorial layout, subtle paper grain texture, understated luxury, no human faces."),
        "default_size": "3:4", "default_resolution": "1k", "style_tags": ["轻奢", "商务"], "sort": 20,
    },
    {
        "template_code": "mktg_poster_recharge_v1", "scene_type": "recharge_activity", "material_kind": "poster",
        "name": "充值活动海报", "feature_code": "mktg_poster_basic",
        "prompt_skeleton": ("A tasteful Chinese promotional poster for a top-up bonus campaign, "
                            "flat color-block screen print style: brand green #6CBE1E, warm cream, charcoal. "
                            "Central symbolic motif: a battery or energy cell filling with light, doubling glow effect, "
                            "upward momentum lines. Festive but restrained, no clutter, large clean area at top for headline, "
                            "geometric composition, halftone dot texture accents, modern retail poster."),
        "default_size": "3:4", "default_resolution": "1k", "style_tags": ["活动", "热闹"], "sort": 30,
    },
    {
        "template_code": "mktg_poster_knowledge_v1", "scene_type": "knowledge_card", "material_kind": "poster",
        "name": "知识卡海报", "feature_code": "mktg_poster_basic",
        "prompt_skeleton": ("A clean Chinese knowledge-card style poster, editorial magazine layout, "
                            "generous white background, thin grid lines, one bold brand-green #6CBE1E underline accent. "
                            "Minimal iconography: a single line-drawn lightbulb or open book icon in one corner. "
                            "Most of the canvas is calm negative space structured for text blocks, "
                            "precise typographic hierarchy, paper texture, zero decoration noise."),
        "default_size": "3:4", "default_resolution": "1k", "style_tags": ["干货", "极简"], "sort": 40,
    },
    {
        "template_code": "mktg_poster_opening_v1", "scene_type": "opening", "material_kind": "poster",
        "name": "开业 / 上新海报", "feature_code": "mktg_poster_pro",
        "prompt_skeleton": ("A celebratory Chinese grand-opening poster, bold geometric composition: "
                            "rising sun arc and ribbon shapes built from flat color blocks, "
                            "palette of brand green #6CBE1E, gold-yellow, cream white. "
                            "Confetti as minimal geometric dots, not photorealistic. Strong symmetry, "
                            "wide clean band across upper third for the main title, screen print aesthetic, "
                            "vintage print imperfection charm."),
        "default_size": "3:4", "default_resolution": "1k", "style_tags": ["庆典", "开业"], "sort": 50,
    },
    {
        "template_code": "mktg_poster_moments_v1", "scene_type": "moments", "material_kind": "poster",
        "name": "朋友圈配图(方形)", "feature_code": "mktg_poster_basic",
        "prompt_skeleton": ("A square social-feed promo card, ultra-minimal single focal point: "
                            "one bold symbolic object (megaphone, gift box, or upward arrow, pick one matching the offer) "
                            "as flat illustration in brand green #6CBE1E on cream background. "
                            "Oversized clean space for a short punchy headline, high contrast, "
                            "thumb-stopping simplicity, screen print texture, no small decorative text."),
        "default_size": "1:1", "default_resolution": "1k", "style_tags": ["朋友圈", "醒目"], "sort": 60,
    },
    {
        "template_code": "mktg_poster_invite_v1", "scene_type": "invitation", "material_kind": "poster",
        "name": "邀请函(竖版)", "feature_code": "mktg_poster_pro",
        "prompt_skeleton": ("An elegant vertical Chinese invitation poster, tall 9:16 composition, "
                            "art-deco inspired thin gold line frame on deep green background, "
                            "brand green #6CBE1E highlights. Centered vertical axis, symbolic motif: "
                            "an unfolding envelope or gate opening with light. Formal, ceremonial, quiet luxury, "
                            "large reserved zones for title, date and venue text lines, subtle grain."),
        "default_size": "9:16", "default_resolution": "1k", "style_tags": ["仪式感", "竖版"], "sort": 70,
    },
]


def seed_default_templates() -> int:
    n = 0
    for t in _DEFAULT_TEMPLATES:
        try:
            marketing_db.upsert_template(
                template_code=t["template_code"], scene_type=t["scene_type"], name=t["name"],
                material_kind=t["material_kind"], prompt_skeleton=t["prompt_skeleton"],
                default_size=t["default_size"], default_resolution=t["default_resolution"],
                style_tags=t.get("style_tags", []), feature_code=t["feature_code"], sort=t["sort"],
                seed_mode=True)  # [返工 R2] 重启不覆盖运营停用/策划替换过的模板
            n += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("[factory] seed template %s 失败: %s", t["template_code"], e)
    return n
