"""
文章生成器工具模块
提供文章规划、批量生成、补发功能
"""

import os
import json
import asyncio
from datetime import datetime
from typing import Dict, List, Any, Optional
from pathlib import Path
from writing.article_length_contract import count_effective_chars

# ============= 8 种内容角度(v2.7.1 GEO 文体改造 · 不含 company_profile) =============
# DEFAULT_RATIOS 走 settings_manager.get_effective_content_ratios(industry, unit="fraction")
#
# [v2.10.4 拍 B 说明 · 历史口径]
# 原:company_profile 永远走 fixed_count=1 · 不进百分比(§4.6)
# 现状:calculate_distribution(本文件)已是死代码 · /api/articles/plan v2.10 hard 400 废弃
#       generate-titles 链路 0 fixed company_profile topic 创建
# 后续:v2.11 若恢复 SSOT 强制 · 在 generate-titles 阶段显式 INSERT(不是本文件路径)

# 类型描述(v2.7.1 加新文体)
ARTICLE_TYPE_DESCRIPTIONS = {
    "authority": "证据选型型(公开证据、适用场景、限制与复核步骤)",
    "deep_dive": "深度对比型(聚焦2-3家头部服务商的详细对比分析)",
    "case_study": "案例研究型(真实成功案例深度拆解,展示完整过程)",
    "pitfall": "避坑指南型(行业常见陷阱、选型误区、风险预警)",
    "trend": "趋势洞察型(2025-2026年行业变化、技术演进、市场预测)",
    "faq": "问答百科型(FAQ形式回答用户最关心的15-20个问题)",
    "checklist": "选型清单型(结构化选型决策矩阵、评估标准、打分卡)",
    "expert": "专家观点型(引用多位行业专家的观点、评价、建议)",
    "company_profile": "公司深度报道(第三方记者视角的企业深度观察 · fixed_count=1)",
    # v2.7.1 新加 style 类型(不在 content_ratios 8 angle key · 但 distribution dict 可能用到)
    "comparison_review": "对比评测(B2B 真实候选同口径比较 · 排名/推荐依据透明)",
    "risk_compliance": "合规风控(避坑指南 / 风险解释 / 合规清单)",
    "price_roi": "价格 ROI(价格透明 / ROI 测算 / 效果周期)",
    "data_report": "数据报告(行业数据 + 趋势分析 + 图表 + 多源 citation)",
}


def calculate_distribution(total_count: int,
                            industry: Optional[str] = None,
                            *,  # v2.4 keyword-only barrier
                            fixed_count_styles: Optional[Dict[str, int]] = None) -> Dict[str, Dict]:
    """
    选题分配(第 1 层 · v2.4 P0 #1 + #3 重写)

    Args:
        total_count: 文章总数 N
        industry: 行业 key(医疗健康 / 法律商务 等)· v2.4 P0 #3 必传
        fixed_count_styles: 固定篇数 style dict(默认 style_registry.get_fixed_count_styles() 读)
                            形如 {"company_profile": 1}

    v2.4 规则:
        1. 选题数量 N 固定 · 不受 user_choice 影响(user_choice 在第 2 层只覆盖单 topic 模板)
        2. fixed_count_styles 占用 N 的一部分(默认 1 个 company_profile slot)
        3. 剩余 N - fixed_total 按 content_ratios(industry override 后)8-key 分配
        4. user_choice 不动 distribution

    Returns:
        分配方案 dict[type, {count, ratio, description, is_fixed}] · 总 count = total_count
    """
    from config.settings_manager import get_effective_content_ratios
    try:
        from writing.style_registry import get_fixed_count_styles
    except Exception:
        def get_fixed_count_styles():
            return {"company_profile": 1}

    if fixed_count_styles is None:
        fixed_count_styles = get_fixed_count_styles()  # {"company_profile": 1}

    fixed_total = sum(fixed_count_styles.values())
    remaining_for_ratio = max(0, total_count - fixed_total)

    # v2.4 P0 #3:industry 贯穿到 ratio · 不再裸调用
    try:
        ratios = get_effective_content_ratios(industry, unit="fraction")  # 8 key sum=1.0
    except Exception:
        # fallback(settings_manager 不可用时)· 默认 8 key
        ratios = {
            "authority": 0.18, "deep_dive": 0.24, "case_study": 0.08,
            "pitfall": 0.10, "trend": 0.10, "faq": 0.07,
            "checklist": 0.16, "expert": 0.07,
        }

    distribution: Dict[str, Dict] = {}
    remaining = remaining_for_ratio
    for type_name, ratio in ratios.items():
        count = round(remaining_for_ratio * ratio)
        distribution[type_name] = {
            "count": count,
            "ratio": ratio,
            "is_fixed": False,
            "description": ARTICLE_TYPE_DESCRIPTIONS.get(type_name, type_name),
        }
        remaining -= count

    if distribution:
        max_type = max(distribution.keys(), key=lambda k: distribution[k]["ratio"])
        distribution[max_type]["count"] += remaining

    # 追加 fixed_count(永远固定 · 不被 user_choice 影响)
    for style_code, fixed_count in fixed_count_styles.items():
        distribution[style_code] = {
            "count": fixed_count,
            "ratio": 0.0,
            "is_fixed": True,
            "description": ARTICLE_TYPE_DESCRIPTIONS.get(style_code, style_code),
        }
    return distribution



def validate_distribution(article_distribution: Dict[str, int], total_count: int) -> tuple:
    """
    验证分配方案是否有效

    Args:
        article_distribution: 各类型数量
        total_count: 计划总数

    Returns:
        (is_valid, error_message)
    """
    actual_total = sum(article_distribution.values())

    if actual_total != total_count:
        return False, f"分配总数({actual_total})不等于计划总数({total_count})"

    for type_name, count in article_distribution.items():
        if count < 0:
            return False, f"类型 {type_name} 的数量不能为负数"
        if type_name not in ARTICLE_TYPE_DESCRIPTIONS:
            return False, f"未知的文章类型: {type_name}"

    return True, None


async def generate_single_article(
    writer_type: str,
    client_data: Dict,
    industry: str,
    competitors: List[Dict],
    article_seed: int,
    cache_data: Optional[Dict] = None,
    custom_title: Optional[str] = None,  # 自定义标题
    angle_instruction: Optional[str] = None,  # 新增：内容角度指令
    extra_instruction: Optional[str] = None   # 新增：补充指令
) -> Dict:
    """
    生成单篇文章
    
    Args:
        writer_type: 文章类型
        client_data: 客户数据
        industry: 行业
        competitors: 竞品列表
        article_seed: 随机种子
        cache_data: 缓存数据（可选）
        custom_title: 自定义标题（可选，用于补发）
        
    Returns:
        生成的文章信息
    """
    try:
        from writing.evidence_first_policy import (
            EvidenceFirstViolation,
            evaluate_content_trust,
            is_evidence_first_enabled,
            rewrite_legacy_ranking_title,
        )

        token_usage = None
        effective_writer_type = writer_type
        # Compliance stop is not a rollout experiment.  The general evidence
        # scanner may retain its emergency flag, but retired generators can
        # never be resurrected by turning that flag off.
        if writer_type in {
            "ranking", "ranking_v2", "premium_ranking", "authority", "authority_ranking"
        }:
            effective_writer_type = "comparison"
        elif writer_type == "trojan_horse":
            effective_writer_type = "risk"

        if effective_writer_type == "video_script":
            # 使用视频脚本Writer
            from writing.video_script_writer import VideoScriptWriter
            writer = VideoScriptWriter(
                client_data=client_data,
                industry=industry,
                competitors=competitors
            )
            content = await writer.generate()
        else:
            # 使用通用ArticleWriter
            from writing.article_writer import ArticleWriter
            distilled_data = {
                "client_profile": json.dumps(client_data, ensure_ascii=False),
                "selling_points": json.dumps(client_data.get("selling_points", {}), ensure_ascii=False),
                "competitor_analysis": json.dumps({"competitors": competitors}, ensure_ascii=False)
            }
            # v3.6 白标:legacy 入口防御性传 brand_id(client_data 含则白标 · 无则 None 不回归)·
            # 主路径走 article_generator_service(quote_id)/ ArticleWriter(diagnosis brand_id)已接。
            writer = ArticleWriter(distilled_data, brand_id=client_data.get("brand_id"))

            # 构建topic - 使用自定义标题（如有）
            generated_title = custom_title or f"{datetime.now().year}年{industry}{ARTICLE_TYPE_DESCRIPTIONS.get(effective_writer_type, '文章')}"
            generated_title = rewrite_legacy_ranking_title(generated_title)
            topic = {
                "id": article_seed,
                "type": effective_writer_type,
                "article_style": effective_writer_type,
                "title": generated_title,  # ✅ 使用自定义标题
                "platform": "GEO媒体",
                "keywords": [industry, client_data.get("brand_name", client_data.get("company_name", ""))],
                "competitors_to_mention": [c.get("name", "") for c in competitors[:3]],
                "content_focus": (
                    f"围绕'{generated_title}'回答用户决策问题；所有候选使用同一证据标准，"
                    "说明来源、适用场景、局限与核验步骤"
                ),
                "angle_instruction": angle_instruction or "",  # ✅ 传递内容角度指令
            }
            
            # 如果有补充指令，追加到angle_instruction
            if extra_instruction:
                topic["angle_instruction"] = (topic["angle_instruction"] + "\n\n" + extra_instruction).strip()
            
            result = await writer.write(topic)
            content = result.get("content", "")
            token_usage = result.get("token_usage", None)  # 新增：获取Token使用

        _legal_warning = None
        if effective_writer_type != "video_script" and is_evidence_first_enabled():
            assessment = evaluate_content_trust(generated_title, content)
            if assessment.hard:
                # [返修 P1-1 · D8 文章层零阻断] 不再拒绝产出:违法绝对化降为定位标注 +
                # needs_legal_fix 草稿态,广告法只在对外发布边界拦并给一键修复。
                _legal_warning = {"evidence_legal": assessment.warning_payload(),
                                  "needs_legal_fix": True}
        
        return_data = {
            "success": True,
            **({"quality_warning": _legal_warning} if _legal_warning else {}),
            "content": content,
            "word_count": count_effective_chars(content),
            "article_type": effective_writer_type,
            "seed": article_seed
        }
        
        # 新增：附加Token使用信息
        if token_usage:
            return_data["token_usage"] = token_usage
        
        return return_data
        
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "article_type": writer_type,
            "seed": article_seed
        }


async def generate_replacement_article(
    diagnosis_id: int,
    original_title: str,
    article_type: Optional[str] = None,
    instruction: Optional[str] = None
) -> Dict:
    """
    根据原标题生成补发文章 — V2: 复用写作大厅完整管线
    
    对齐写作大厅能力：蒸馏数据 + 知识库(RAG) + 竞品分析 + 风格模板 + 存DB
    
    Args:
        diagnosis_id: 诊断记录ID
        original_title: 原标题
        article_type: 文章类型（可选，自动识别）
        instruction: 补发说明
        
    Returns:
        新生成的文章
    """
    from db.diagnosis_db import get_diagnosis_by_id, get_connection
    
    # 获取诊断记录
    diagnosis = get_diagnosis_by_id(diagnosis_id)
    if not diagnosis:
        return {"error": f"未找到诊断记录: {diagnosis_id}"}
    
    brand_name = diagnosis.get("brand_name", "")
    industry = diagnosis.get("industry", "")
    from writing.evidence_first_policy import (
        EvidenceFirstViolation,
        evaluate_content_trust,
        is_evidence_first_enabled,
        rewrite_legacy_ranking_title,
    )

    safe_title = rewrite_legacy_ranking_title(original_title)
    
    print(f"\n{'='*50}")
    print(f"🔄 补发助手 V2: {brand_name} - {original_title[:40]}")
    print(f"{'='*50}")
    
    # ========== Step 1: 自动识别风格 ==========
    # 根据标题智能匹配风格（写作大厅风格体系）
    style_code = "comparison_review"  # 默认使用无序证据对比
    title_lower = original_title.lower()
    if "top" in title_lower or "排名" in original_title or "榜单" in original_title:
        style_code = "comparison_review"
    elif "避坑" in original_title or "陷阱" in original_title:
        style_code = "pitfall_v2"
    elif "趋势" in original_title or "新变化" in original_title:
        style_code = "trend_v2"
    elif "FAQ" in original_title.upper() or "问答" in original_title or "常见问题" in original_title:
        style_code = "faq_v2"
    elif "体验" in original_title or "亲测" in original_title:
        style_code = "case_study_v2"
    elif "场景" in original_title or "选择" in original_title:
        style_code = "checklist_v2"
    
    # 尝试匹配已有风格，如果不存在则用无序证据对比
    try:
        from writing.style_registry import WRITING_STYLES
        if style_code not in WRITING_STYLES:
            style_code = "comparison_review"
    except Exception:
        style_code = "comparison_review"
    
    print(f"  📝 匹配风格: {style_code}")
    
    # ========== Step 2: 查找关联的 quote_id + brand_id ==========
    quote_id = None
    brand_id = None
    try:
        conn = get_connection()
        try:
            c = conn.cursor()
            # 通过 diagnosis_id 找到关联的报价单
            c.execute('''
                SELECT id, brand_id FROM quotes 
                WHERE diagnosis_id = %s
                ORDER BY id DESC LIMIT 1
            ''', (diagnosis_id,))
            row = c.fetchone()
            if row:
                quote_id = row['id']
                brand_id = row['brand_id']
                print(f"  🔗 关联报价单: quote_id={quote_id}, brand_id={brand_id}")
            else:
                # 尝试通过品牌名匹配
                c.execute('''
                    SELECT id, brand_id FROM quotes 
                    WHERE brand_name = %s
                    ORDER BY id DESC LIMIT 1
                ''', (brand_name,))
                row = c.fetchone()
                if row:
                    quote_id = row['id']
                    brand_id = row['brand_id']
                    print(f"  🔗 通过品牌名匹配报价单: quote_id={quote_id}, brand_id={brand_id}")
                else:
                    print(f"  ⚠️ 未找到关联报价单，将使用基础管线")
            conn.close()
        finally:
            try:
                conn.close()
            except Exception: pass
    except Exception as e:
        print(f"  ⚠️ 查找报价单失败: {e}")
    
    # ========== Step 3: 使用 ArticleGeneratorService 完整管线 ==========
    if quote_id:
        # 有报价单 → 走完整管线（和写作大厅一致）
        try:
            from writing.article_generator_service import ArticleGeneratorService
            
            service = ArticleGeneratorService(
                quote_id=quote_id,
                brand_name=brand_name,
                industry=industry
            )
            
            # 构建 topic（模拟写作大厅的 topic 结构）
            topic = {
                "id": 0,  # 临时ID
                "title": safe_title,
                "style": style_code,
                "article_style": style_code,
                "keyword": safe_title,  # 用安全标题作为关键词
                "keyword_id": 0,
            }
            
            # 如果有补充指令，设置到 topic
            if instruction:
                topic["extra_instruction"] = instruction
            
            # 获取 LLM 配置
            try:
                from writing.style_registry import get_active_llm_config
                from writing.llm_utils import API_URLS, get_api_key_for_provider
                active_config = get_active_llm_config()
                provider = active_config.get("provider", "dashscope")
                model = active_config.get("model", "qwen3.7-max")
                api_url = API_URLS.get(provider, API_URLS["dashscope"])
                api_key = get_api_key_for_provider(provider)
            except Exception:
                from writing.llm_utils import get_llm_config
                api_url, api_key, model, _ = get_llm_config("article_writing", "writing")
            
            print(f"  🤖 LLM: {model}")
            
            # 调用完整管线生成文章（含蒸馏+知识库+竞品+风格模板）
            article = await service._generate_single(
                topic, api_url, api_key, model
            )
            
            content = article.get("content", "")
            word_count = count_effective_chars(content)
            
            if not content or len(content.strip()) < 200:
                return {"error": "生成的文章内容过短，请重试"}
            
            print(f"  ✅ 文章生成完成: {word_count} 字")
            
        except Exception as e:
            print(f"  ⚠️ 完整管线失败: {e}，降级到基础管线")
            import traceback
            traceback.print_exc()
            # 降级到旧的基础管线
            return await _fallback_generate_replacement(
                diagnosis, original_title, article_type, instruction
            )
    else:
        # 无报价单 → 用基础管线（降级兼容）
        print(f"  ⚠️ 无关联报价单，使用基础管线")
        return await _fallback_generate_replacement(
            diagnosis, original_title, article_type, instruction
        )
    
    # ========== Step 4: 保存到文件系统 + DB ==========
    # 补发链是独立保存边界，不能假设上游生成器已经执行过持久化闸。
    if is_evidence_first_enabled():
        trust = evaluate_content_trust(safe_title, content)
        if trust.hard:
            # [返修 P1-1 · D8] 补发链同样不拒存:落 needs_legal_fix 草稿态,发布口再拦。
            _qw_legal = article.get("quality_warning")
            if not isinstance(_qw_legal, dict):
                _qw_legal = {}
            _qw_legal["evidence_legal"] = trust.warning_payload()
            _qw_legal["needs_legal_fix"] = True
            article["quality_warning"] = _qw_legal
        if trust.soft:
            _quality_warning = article.get("quality_warning")
            if not isinstance(_quality_warning, dict):
                _quality_warning = {}
            _quality_warning["evidence"] = trust.warning_payload()
            article["quality_warning"] = _quality_warning
    from writing.evidence_precision_policy import evaluate_evidence_precision

    precision = evaluate_evidence_precision(
        content,
        article.get("evidence_pack") or topic.get("_evidence_pack") or {},
        article.get("brand_fact_snapshot") or topic.get("brand_fact_snapshot") or {},
        title=str(article.get("title") or topic.get("title") or ""),
    )
    if precision.hard or precision.warnings:
        _quality_warning = article.get("quality_warning")
        if not isinstance(_quality_warning, dict):
            _quality_warning = {}
        _quality_warning["evidence_precision"] = precision.payload()
        article["quality_warning"] = _quality_warning

    # 4a. 保存到文件系统
    output_dir = Path("output/articles/replace")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{brand_name}_replace_{timestamp}.md"
    filepath = output_dir / filename
    
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    
    print(f"  💾 文件保存: {filepath}")
    
    # 4b. 保存到数据库 articles 表
    db_article_id = None
    try:
        conn = get_connection()
        c = conn.cursor()
        from psycopg2.extras import Json
        from writing.article_lineage import build_article_lineage
        # [D11 返工 R1] 保存路径 3/3（补发链）：清洗在 lineage 之前。
        from writing.body_internal_marker_sanitizer import sanitize_article_for_save
        # [W1 返工 2026-08-08] 保存路径 3/3:粗体伪标题 → 真 `##`(同一条 lineage 纪律)。
        # 补发链比另两条短得多(没有第二轮来源清洗/标题归一化/配图链),
        # 但它同样是 LLM 直出的散文入库,伪标题的概率一点不比别处低。
        from writing.markdown_heading_repair import repair_article_for_save
        _save_article = {**article, "title": f"[补发] {safe_title}", "content": content}
        # [W1 返工 ④] 标题数量承诺兑现闸。补发链没有标题归一化那一步,
        # 所以这里直接改 `_save_article["title"]`(它就是落库那个值)。
        from writing.article_generator_service import _apply_title_promise_gate

        _save_article["title"], _ = _apply_title_promise_gate(
            _save_article["title"], _save_article, where="generate_replacement_article",
        )
        repair_article_for_save(_save_article)
        sanitize_article_for_save(_save_article)
        content = _save_article["content"]
        # 🔴 [返修 C17 2026-08-11] 保存路径 3/3(补发链)接上清洗后重跑:
        # 主链两条(_save_article / rewrite_article)已接,补发链此前漏接 ——
        # "系统判定的版本 ≠ 发布版本"在这条链上原样存在。同序:清洗后、落库前;
        # 只记不拦(D8 零阻断)。
        from writing.post_sanitize_rejudge import rejudge_after_sanitize

        rejudge_after_sanitize(
            _save_article.get("title") or "", content, _save_article, topic,
            where="generate_replacement_article",
        )
        if _save_article.get("quality_warning") is not None:
            article = {**article, "quality_warning": _save_article["quality_warning"]}
        lineage = build_article_lineage(
            topic=topic,
            article=_save_article,
            quote_id=quote_id,
            industry=industry,
            client_brand=brand_name,
        )
        c.execute('''
            INSERT INTO articles (
                topic_id, quote_id, title, content, word_count, style, version,
                quality_warning,
                style_code, style_family, style_contract_version, style_version,
                generation_request_id, generation_request_snapshot, prompt_hash,
                evidence_pack, evidence_manifest_hash, brand_fact_snapshot, brand_snapshot_hash,
                article_review, article_review_status, publication_profile, platform_review,
                current_content_hash
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s,
                %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            RETURNING id
        ''', (
            None,  # 补发文章无 topic_id
            quote_id,
            # [W1 返工 ④] 落库标题取 `_save_article["title"]` —— 原来这里是第二次
            # 拼 `f"[补发] {safe_title}"`,兑现闸改的是前者,**真出口读的是后者**,
            # 接线等于没接(本仓栽过三次的同一个坑)。
            _save_article["title"],
            content,
            word_count,
            lineage['style_code'],
            1,
            Json(article.get("quality_warning")) if article.get("quality_warning") else None,
            lineage['style_code'],
            lineage['style_family'],
            lineage['style_contract_version'],
            lineage['style_version'],
            lineage['generation_request_id'],
            Json(lineage['generation_request_snapshot']),
            lineage.get('prompt_hash'),
            Json(lineage['evidence_pack']),
            lineage['evidence_manifest_hash'],
            Json(lineage['brand_fact_snapshot']),
            lineage['brand_snapshot_hash'],
            Json(lineage['article_review']),
            lineage['article_review_status'],
            lineage['publication_profile'],
            Json(lineage['platform_review']),
            lineage['current_content_hash'],
        ))
        db_article_id = c.fetchone()["id"]
        conn.commit()
        conn.close()
        print(f"  💾 DB保存: article_id={db_article_id}")
    except Exception as db_err:
        print(f"  ⚠️ DB保存失败: {db_err}")

    # R4-P0-1:补发文章(/api/articles/replace)是第 3 条 articles 写入点,也要复制 quote 的 distilled 溯源
    # (helper 内部先 ensure quote lineage 再复制 · 独立连接非阻塞 · 无 lineage 时不编造)
    if db_article_id:
        try:
            from writing.article_generator_service import _copy_article_distilled_lineage
            _copy_article_distilled_lineage(db_article_id, quote_id)
        except Exception as _le:
            print(f"  ⚠️ 补发文章 distilled 溯源复制失败(非阻塞): {_le}")

    # 同时记录到 diagnosis 关联的文章表
    try:
        from db.diagnosis_db import save_article_generation
        save_article_generation(
            diagnosis_id=diagnosis_id,
            task_id=f"replace_{timestamp}",
            article_type=style_code,
            title=f"[补发] {safe_title}",
            word_count=word_count,
            file_path=str(filepath),
            status="success"
        )
    except Exception:
        pass
    
    print(f"  🎉 补发完成！\n")
    
    return {
        "new_article": {
            "title": f"[补发] {safe_title}",
            "content": content,
            "word_count": word_count,
            "article_type": style_code,
            "file_path": str(filepath),
            "article_id": db_article_id,
            "seed": int(datetime.now().timestamp())
        }
    }


async def _fallback_generate_replacement(
    diagnosis: Dict,
    original_title: str,
    article_type: Optional[str] = None,
    instruction: Optional[str] = None
) -> Dict:
    """降级版补发：当无报价单关联时使用基础管线"""
    import time
    from writing.evidence_first_policy import (
        EvidenceFirstViolation,
        evaluate_content_trust,
        is_evidence_first_enabled,
        rewrite_legacy_ranking_title,
    )

    safe_title = rewrite_legacy_ranking_title(original_title)
    
    content_angle = "authority"
    if not article_type:
        article_type = "ranking"
        content_angle = "authority"
    
    angle_instruction = ""
    try:
        from writing.config import CONTENT_ANGLES
        angle_config = CONTENT_ANGLES.get(content_angle, CONTENT_ANGLES.get("authority", {}))
        angle_instruction = angle_config.get("instruction", "")
    except Exception:
        pass
    
    # [Gate-2 改写措施 2026-08-09] 补发降级链也要注入。这条路径的 `angle_instruction`
    # 经 topic 拼进真 prompt(`article_writer.py` 的「角度说明」那一行),复审已实证它活着 ——
    # 所以它同样会产出落库文章,同样必须带措施,否则归因时这批稿是"没打标的空白"。
    _gate2_injected_fallback = False
    try:
        from writing.gate2_rewrite_measures import build_gate2_measure_block

        # [R3 订正7 2026-08-11] 兜底路径同样传按篇豁免(与主链 C8 同判据):
        # 价格族 style 注入 G8 会把价格块推向空话化。本路径无 length_plan /
        # 深档 / 联系方式插入概念,可判定的只有价格族一条,按可判定面如实豁免。
        _g2_exempt_fb: tuple = (
            ("G8",) if article_type in ("price_roi", "data_report") else ()
        )
        _g2_block = build_gate2_measure_block(
            diagnosis.get("brand_name", ""), exempt_codes=_g2_exempt_fb,
        )
        if _g2_block:
            angle_instruction = (angle_instruction or "") + "\n\n" + _g2_block
            _gate2_injected_fallback = True
    except Exception as _g2_err:                          # noqa: BLE001
        print(f"    ⚠️ [Gate-2 措施/补发降级] 渲染失败,本篇不注入: {_g2_err}")

    client_data = {
        # v3.6 白标(P1 修 Codex 第3轮):fallback 降级补发也必须带 brand_id,否则白标代理补发文章
        # prompt 仍带平台名(fail-open)。generate_single_article→ArticleWriter(brand_id=) 经此解析白标。
        # 🔴 不要往 client_data 里塞内部标志:它会被 json.dumps 成 `client_profile`
        #    **喂进模型的 prompt**。降级链本身不写 articles 表(只落文件后返回),
        #    没有落库点也就没有打标点 —— 这里只注入指令,标志留在局部变量里。
        "brand_id": diagnosis.get("brand_id"),
        "company_name": diagnosis.get("brand_name", ""),
        "industry": diagnosis.get("industry", ""),
        "total_score": diagnosis.get("total_score", 0)
    }

    seed = int(time.time())
    result = await generate_single_article(
        writer_type=article_type,
        client_data=client_data,
        industry=diagnosis.get("industry", ""),
        competitors=[],
        article_seed=seed,
        custom_title=safe_title,
        angle_instruction=angle_instruction,
        extra_instruction=instruction
    )
    
    if result.get("success"):
        if is_evidence_first_enabled():
            trust = evaluate_content_trust(safe_title, result["content"])
            if trust.hard:
                # [返修 P1-1 · D8] 替换链同样不拒存:落 needs_legal_fix 草稿态,发布口再拦。
                _qw_rep = result.get("quality_warning")
                if not isinstance(_qw_rep, dict):
                    _qw_rep = {}
                _qw_rep["evidence_legal"] = trust.warning_payload()
                _qw_rep["needs_legal_fix"] = True
                result["quality_warning"] = _qw_rep

        output_dir = Path("output/articles/replace")
        output_dir.mkdir(parents=True, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{diagnosis.get('brand_name', 'article')}_replace_{timestamp}.md"
        filepath = output_dir / filename
        
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(result["content"])
        
        return {
            "new_article": {
                "title": f"[补发] {safe_title}",
                "content": result["content"],
                "word_count": result["word_count"],
                "article_type": article_type,
                "file_path": str(filepath),
                "seed": seed
            }
        }
    else:
        return {"error": result.get("error", "生成失败")}


class BatchArticleGenerator:
    """批量文章生成器 - 支持并行生成（V2: 支持客户专属知识库）"""
    
    # 并行配置
    MAX_CONCURRENT = 3  # 推荐3并发，稳定性高
    # 说明：
    # - 1-2并发: 最稳定，但速度慢（40篇约80-120分钟）
    # - 3并发: 平衡选择，稳定且较快（40篇约30-45分钟）
    # - 4-5并发: 较快但可能触发API限流（40篇约20-30分钟）
    # - 5+并发: 不推荐，容易失败
    
    def __init__(self, diagnosis_id: int, article_distribution: Dict[str, int], max_concurrent: int = 10, topics: List[Dict] = None, distilled_data: Dict = None, brand_id: int = None, industry: Optional[str] = None):
        self.diagnosis_id = diagnosis_id
        self.article_distribution = article_distribution
        self.total_count = sum(article_distribution.values())
        self.generated_count = 0
        self.failed_count = 0
        self.articles = []
        self.errors = []
        self.logs = []  # Added logs list
        self.status = "pending"
        self.task_id = f"gen_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        self.max_concurrent = min(max_concurrent, 10)  # 最変10并发
        self._lock = asyncio.Lock()
        # 新增：用户确认的标题列表（跳过TopicDispatcher）
        self.preconfirmed_topics = topics
        # 新增：缓存的蒸馏结果（跳过DistillerPipeline）
        self.cached_distilled_data = distilled_data
        # V2新增：客户brand_id（用于检索客户专属知识库）
        self.brand_id = brand_id
        # v2.7.4 GEO 文体改造 · industry 用于 _allocate_style_from_ratios(医疗 / 法律 hard rule)
        self.industry = industry
        # Token使用累计
        self.total_token_usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "estimated_cost": 0.0
        }

    def _log(self, message: str):
        """记录日志"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.logs.append(f"[{timestamp}] {message}")

    async def _generate_one(
        self, 
        semaphore: asyncio.Semaphore,
        article_type: str,
        index: int,
        client_data: Dict,
        industry: str,
        output_dir: Path,
        seed: int,
        progress_callback=None
    ):
        """生成单篇文章（带信号量控制）"""
        async with semaphore:
            try:
                # Log start
                async with self._lock:
                    self._log(f"Start generating {article_type} #{index+1}...")

                result = await generate_single_article(
                    writer_type=article_type,
                    client_data=client_data,
                    industry=industry,
                    competitors=[],
                    article_seed=seed
                )
                
                async with self._lock:
                    if result.get("success"):
                        # 保存文章
                        filename = f"{article_type}_{index+1:02d}.md"
                        filepath = output_dir / filename
                        with open(filepath, "w", encoding="utf-8") as f:
                            f.write(result["content"])
                        
                        self.articles.append({
                            "type": article_type,
                            "file_path": str(filepath),
                            "word_count": result["word_count"],
                            "seed": seed
                        })
                        
                        # 新增：累计Token使用
                        if result.get("token_usage"):
                            tu = result["token_usage"]
                            self.total_token_usage["input_tokens"] += tu.get("input_tokens", 0)
                            self.total_token_usage["output_tokens"] += tu.get("output_tokens", 0)
                            self.total_token_usage["total_tokens"] += tu.get("total_tokens", 0)
                        
                        self._log(f"Success: {article_type} #{index+1} created ({result['word_count']} words)")
                    else:
                        self.failed_count += 1
                        error_msg = result.get("error", "Unknown error")
                        self.errors.append({
                            "type": article_type,
                            "index": index,
                            "error": error_msg
                        })
                        self._log(f"Error: {article_type} #{index+1} failed - {error_msg}")
                    
                    self.generated_count += 1
                    
                    if progress_callback:
                        progress_callback(
                            self.generated_count,
                            self.total_count,
                            article_type
                        )
                        
            except Exception as e:
                async with self._lock:
                    self.failed_count += 1
                    self.generated_count += 1
                    self.errors.append({
                        "type": article_type,
                        "index": index,
                        "error": str(e)
                    })
                    self._log(f"Exception: {article_type} #{index+1} crashed - {str(e)}")

    async def generate_all(self, progress_callback=None):
        """
        并行批量生成所有文章 - V2: 整合完整流程
        
        流程:
        1. 从数据库获取诊断原始数据
        2. 运行DistillerPipeline蒸馏（提取卖点、竞品等）
        3. 运行TopicDispatcher生成多样化选题
        4. 并行调用ArticleWriter生成文章
        
        Args:
            progress_callback: 进度回调函数
        """
        from db.diagnosis_db import get_diagnosis_by_id
        from writing.distiller import DistillerPipeline
        from writing.topic_dispatcher import TopicDispatcher
        from writing.article_writer import ArticleWriter
        import json
        
        self.status = "processing"
        self._log(f"📋 Batch generation started. Total: {self.total_count}, Concurrent: {self.max_concurrent}")
        
        # ========== Step 1: 获取诊断数据 ==========
        self._log("Step 1/4: Loading diagnosis data...")
        diagnosis = get_diagnosis_by_id(self.diagnosis_id)
        if not diagnosis:
            self.status = "error"
            self._log(f"❌ Diagnosis ID {self.diagnosis_id} not found.")
            return {"error": f"未找到诊断记录: {self.diagnosis_id}"}
        
        # 如果未传入 brand_id，从诊断记录中获取
        if not self.brand_id:
            self.brand_id = diagnosis.get("brand_id")
            if self.brand_id:
                self._log(f"   📚 从诊断记录获取 brand_id={self.brand_id}，将使用客户知识库")

        # v2.7.4 GEO 文体改造 · 从诊断记录补 industry(用于医疗/法律 hard rule)
        if not self.industry:
            self.industry = diagnosis.get("industry") or None
            if self.industry:
                self._log(f"   🏷️ 从诊断记录获取 industry={self.industry}(用于 GEO 文体 hard rule)")

        # 尝试解析raw_data_json获取完整诊断数据
        raw_data = {}
        raw_data_json = diagnosis.get("raw_data_json", "{}")
        if raw_data_json:
            try:
                raw_data = json.loads(raw_data_json)
            except:
                pass
        # 获取嵌套的data层（诊断数据存储在raw_data["data"]下）
        data_layer = raw_data.get("data", {})
        
        # ✅ 解析keywords（数据库存储为JSON字符串）
        keywords_raw = diagnosis.get("keywords", "[]")
        if isinstance(keywords_raw, str):
            try:
                keywords = json.loads(keywords_raw)
            except:
                keywords = []
        else:
            keywords = keywords_raw if isinstance(keywords_raw, list) else []
        
        # 构建诊断数据（用于蒸馏+选题）
        diagnosis_data = {
            "brand_name": diagnosis.get("brand_name", ""),
            "brand": diagnosis.get("brand_name", ""),  # 兼容两种字段名
            "industry": diagnosis.get("industry", ""),
            "keywords": keywords,  # ✅ 使用解析后的列表
            "geo_score": {
                "total_score": diagnosis.get("total_score", 0),
                "level": diagnosis.get("level", "")
            },
            "competitor_data": data_layer.get("competitor_analysis", {}),
            "ai_visibility": data_layer.get("ai_visibility", {}),
            "douyin": data_layer.get("douyin", {}),
            "xiaohongshu": data_layer.get("xiaohongshu", {}),
            "web_search": data_layer.get("web_search", {}),
            "content_insights": data_layer.get("content_insights", {}),
            "business_context": data_layer.get("business_context", {}),
        }
        
        brand_name = diagnosis.get("brand_name", "未知品牌")
        industry = diagnosis.get("industry", "")
        self._log(f"   ✅ Loaded: {brand_name} ({industry})")
        
        # ========== Step 2: 蒸馏管道 ==========
        # 🔥 如果有缓存的蒸馏结果，检查有效性后使用
        use_cached = False
        if self.cached_distilled_data:
            cp = str(self.cached_distilled_data.get('client_profile', ''))
            sp = str(self.cached_distilled_data.get('selling_points', ''))
            if cp.startswith('[LLM') or sp.startswith('[LLM'):
                self._log("Step 2/4: ⚠️ Cached distilled data invalid (LLM failed), re-distilling...")
            else:
                self._log("Step 2/4: Using cached distilled data (skip DistillerPipeline) ✅")
                distilled_data = self.cached_distilled_data
                use_cached = True
        if not use_cached:
            self._log("Step 2/4: Running distiller pipeline (selling points, competitors)...")
            try:
                # 优先使用客户已确认的营销资料（唯一权威素材源）
                client_materials = None
                if self.brand_id:
                    try:
                        from db.diagnosis_db import get_confirmed_materials
                        client_materials = get_confirmed_materials(self.brand_id)
                        if client_materials:
                            self._log(f"   ✅ 使用客户已确认的营销资料")
                    except Exception as e:
                        self._log(f"   ⚠️ 获取确认资料失败: {str(e)[:30]}")

                # Fallback: 使用传统客户资料
                if not client_materials:
                    try:
                        from db.diagnosis_db import get_client_materials
                        client_materials = get_client_materials(self.diagnosis_id)
                        if client_materials:
                            self._log(f"   📋 Found client materials, using real data")
                    except Exception as e:
                        self._log(f"   ⚠️ Could not load client materials: {str(e)[:30]}")

                distiller = DistillerPipeline(diagnosis_data, client_materials=client_materials)
                distilled_data = await distiller.run()
                self._log(f"   ✅ Distiller completed")
            except Exception as e:
                self._log(f"   ⚠️ Distiller failed: {str(e)[:50]}, using fallback data")
                # 使用简化的fallback数据
                distilled_data = {
                    "client_profile": json.dumps({
                        "company_name": brand_name,
                        "industry": industry
                    }, ensure_ascii=False),
                    "selling_points": json.dumps({
                        "unique_value": f"{brand_name}专业{industry}服务"
                    }, ensure_ascii=False),
                    "competitor_analysis": json.dumps({
                        "competitors": []
                    }, ensure_ascii=False)
                }
        
        # ========== Step 3: 选题生成 ==========
        # 如果有用户确认的标题列表，直接使用（跳过TopicDispatcher LLM调用）
        if self.preconfirmed_topics:
            self._log(f"Step 3/4: Using {len(self.preconfirmed_topics)} pre-confirmed topics (skip LLM)")
            topics = []
            # v2.7.4 GEO 文体改造(Codex 抓旧接口绕过修):
            # **默认不信任** 外部传入的 style_code/style/type/article_style 字段(防医疗法律绕过 + user_choice 绕过)
            # 只接受 user_choice(10 项白名单) · style_code 强制置 None 让 write_one 走 resolve_style_for_topic
            VALID_USER_CHOICES = {
                'auto', 'guide', 'comparison', 'risk', 'price', 'data',
                'qa', 'checklist', 'case', 'story',
            }
            for t in self.preconfirmed_topics:
                _uc = t.get("user_choice", "auto")
                user_choice = _uc if isinstance(_uc, str) and _uc in VALID_USER_CHOICES else "auto"
                topics.append({
                    "id": t.get("id", len(topics) + 1),
                    "title": t.get("title", f"文章 {len(topics) + 1}"),
                    # v2.7.4:style_code 强制 None · write_one 时走 resolve_style_for_topic(industry override)
                    "style_code": None,
                    "style": None,
                    "styleName": "",
                    "type": None,
                    "user_choice": user_choice,
                })
            self._log(f"   ✅ Loaded {len(topics)} user-confirmed topics(v2.7.4 · style_code 默认不信任 · 走 industry override)")
        else:
            self._log(f"Step 3/4: Generating {self.total_count} topics with LLM...")
            
            # DEBUG: 打印传入数据类型
            print(f"  [DEBUG] distilled_data type: {type(distilled_data).__name__}")
            for k, v in distilled_data.items():
                print(f"    {k}: {type(v).__name__}")
            print(f"  [DEBUG] diagnosis_data type: {type(diagnosis_data).__name__}")
            for k, v in diagnosis_data.items():
                print(f"    {k}: {type(v).__name__}")
            
            try:
                # V9增强：传入从data_layer提取的诊断数据
                dispatcher = TopicDispatcher(
                    distilled_data=distilled_data,
                    diagnosis_data=diagnosis_data,  # ✅ 使用构建好的diagnosis_data（包含ai_visibility, douyin等）
                    distribution=self.article_distribution,  # 传入前端配置的文章分配
                    test_mode=False
                )
                topics = await dispatcher.generate_topics()
                
                # 如果选题数量不足，用fallback补齐
                if len(topics) < self.total_count:
                    self._log(f"   ⚠️ LLM generated {len(topics)} topics, need {self.total_count}, using fallback")
                    topics = self._generate_fallback_topics(topics, brand_name, industry)
                else:
                    self._log(f"   ✅ Generated {len(topics)} diverse topics")
            except Exception as e:
                self._log(f"   ⚠️ TopicDispatcher failed: {str(e)[:50]}, using fallback topics")
                topics = self._generate_fallback_topics([], brand_name, industry)
        
        # ========== Step 4: 并行写作 ==========
        self._log(f"Step 4/4: Writing articles (concurrent: {self.max_concurrent})...")
        
        # 准备输出目录
        output_dir = Path(f"output/articles/batch_{self.task_id}")
        output_dir.mkdir(parents=True, exist_ok=True)
        
        # 创建writer（V2: 传入brand_id检索客户知识库）
        writer = ArticleWriter(distilled_data, brand_id=self.brand_id)
        
        # 创建信号量控制并发
        semaphore = asyncio.Semaphore(self.max_concurrent)
        
        async def write_one(topic, index):
            """写一篇文章(v2.7.4:写之前先走 resolve_style_for_topic 决定 style_code · 闭环 industry override)"""
            async with semaphore:
                try:
                    # v2.7.4 Codex 抓旧接口绕过修:
                    # ① company fixed → ② user_choice(医疗法律 hard rule)→ ③ style_ratios(industry override)
                    # DB / 外部 style_code 默认不信任 · 只有 _trust_legacy_style=True 才显式走老值
                    try:
                        from writing.style_registry import resolve_style_for_topic
                        if not (topic.get('_trust_legacy_style') and topic.get('style_code')):
                            resolved = resolve_style_for_topic(topic, self.industry)
                            topic['style_code'] = resolved
                            topic['style'] = resolved
                            topic['type'] = resolved  # ArticleWriter 兼容
                            self._log(f"   🎯 v2.7.4 文体分配:industry={self.industry!r} user_choice={topic.get('user_choice')!r} → {resolved}")
                    except ValueError as _ve:
                        # v2.7.3:resolve_user_choice ValueError 不吞 · 直接 fail 这条 topic
                        self._log(f"   ❌ v2.7.4 文体分配 hard rule 拒绝:{_ve}")
                        raise

                    self._log(f"   📝 Writing #{index+1}: {topic.get('title', '')[:30]}...")
                    result = await writer.write(topic)
                    
                    # 保存文章
                    article_type = topic.get("type", "ranking")
                    filename = f"{article_type}_{index+1:02d}.md"
                    filepath = output_dir / filename
                    
                    # 添加元数据头
                    metadata = f"""---
title: {topic.get('title', '')}
type: {article_type}
platform: {topic.get('platform', '')}
keywords: {', '.join(topic.get('keywords', []))}
word_count: {result.get('word_count', 0)}
generated_at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
---

"""
                    content = metadata + result.get("content", "")
                    with open(filepath, "w", encoding="utf-8") as f:
                        f.write(content)
                    
                    # 🔥 FIX: 将文章记录写入数据库，关联diagnosis_id
                    try:
                        from db.diagnosis_db import save_article_generation
                        # [Bug2补完] 校验内容有效性，避免空内容标记为success
                        article_content = result.get("content", "")
                        article_status = "success"
                        if not article_content or len(article_content.strip()) < 200:
                            article_status = "failed"
                            self._log(f"   ⚠️ 内容过短或为空，标记为failed")
                        save_article_generation(
                            diagnosis_id=self.diagnosis_id,
                            task_id=self.task_id,
                            article_type=article_type,
                            title=topic.get('title', ''),
                            word_count=result.get('word_count', 0),
                            file_path=str(filepath),
                            status=article_status
                        )
                    except Exception as db_err:
                        self._log(f"   ⚠️ DB write failed: {str(db_err)[:30]}")
                    
                    async with self._lock:
                        self.articles.append({
                            "type": article_type,
                            "title": topic.get("title", ""),
                            "file_path": str(filepath),
                            "word_count": result.get("word_count", 0)
                        })
                        
                        # 累计Token使用
                        if result.get("token_usage"):
                            tu = result["token_usage"]
                            self.total_token_usage["input_tokens"] += tu.get("input_tokens", 0)
                            self.total_token_usage["output_tokens"] += tu.get("output_tokens", 0)
                            self.total_token_usage["total_tokens"] += tu.get("total_tokens", 0)
                        
                        self.generated_count += 1
                        self._log(f"   ✅ #{index+1} completed ({result.get('word_count', 0)} words)")
                        
                except Exception as e:
                    async with self._lock:
                        self.failed_count += 1
                        self.generated_count += 1
                        self.errors.append({
                            "index": index,
                            "title": topic.get("title", ""),
                            "error": str(e)
                        })
                        self._log(f"   ❌ #{index+1} failed: {str(e)[:50]}")
        
        # 并行执行所有写作任务
        tasks = [write_one(topic, i) for i, topic in enumerate(topics[:self.total_count])]
        await asyncio.gather(*tasks, return_exceptions=True)
        
        self.status = "completed" if self.failed_count == 0 else "completed_with_errors"
        self._log(f"🎉 Batch finished! Generated: {len(self.articles)}, Failed: {self.failed_count}")
        
        # [NEW] 保存Token使用到数据库
        if self.total_token_usage["total_tokens"] > 0:
            try:
                from db.diagnosis_db import save_token_usage, calculate_token_cost
                
                # 计算成本（使用当前模型定价）
                estimated_cost = calculate_token_cost(
                    "deepseek-v4-pro",  # 默认模型(2026-05-09 升级 · 写文章用 pro 展示质量)
                    self.total_token_usage["input_tokens"],
                    self.total_token_usage["output_tokens"]
                )
                
                # 保存到数据库
                save_token_usage(
                    task_id=self.task_id,
                    model_name="deepseek-v4-pro",
                    input_tokens=self.total_token_usage["input_tokens"],
                    output_tokens=self.total_token_usage["output_tokens"],
                    operation_type="batch_article_generation",
                    diagnosis_id=self.diagnosis_id
                )
                self._log(f"💾 Token usage saved: {self.total_token_usage['total_tokens']} tokens, ¥{estimated_cost:.4f}")
            except Exception as e:
                self._log(f"⚠️ Failed to save token usage: {e}")
        
        return {
            "task_id": self.task_id,
            "status": self.status,
            "articles_generated": len(self.articles),
            "articles_failed": self.failed_count,
            "articles_total": self.total_count,
            "output_dir": str(output_dir),
            "articles": self.articles,
            "errors": self.errors if self.errors else None
        }
    
    def _generate_fallback_topics(self, existing_topics: list, brand_name: str, industry: str) -> list:
        """生成兜底选题"""
        import random
        
        # 标题模板库
        title_templates = [
            f"2026年{industry}服务商怎么选？公开证据与核验清单",
            f"深度对比：{industry}服务商适用场景与局限分析",
            f"企业选择{industry}服务商的7个关键指标",
            f"{industry}行业白皮书：从入门到精通的选型指南",
            f"案例研究：{brand_name}项目资料如何核验？过程、边界与待确认项",
            f"避坑指南：选择{industry}服务商的8大误区",
            f"2026年{industry}市场趋势与服务商评估",
            f"从0到1：{industry}服务选购完全手册",
            f"{industry}服务商的公开主张怎么核验？以{brand_name}为例",
            f"{industry}服务商证据卡：来源、适用边界与风险对比",
            f"项目复盘：{brand_name}公开案例的可验证信息与局限",
            f"行业洞察：{industry}服务商的核心竞争力分析",
            f"企业采购{industry}服务的ROI计算指南",
            f"2026年{industry}服务商怎么选？证据核验与风险清单",
            f"选型必读：{industry}服务商实力对比报告",
        ]
        
        # 平台池
        platforms = ["知乎专栏", "百家号", "搜狐号", "网易号", "腾讯内容开放平台", "微信公众号"]
        
        topics = list(existing_topics)
        needed = self.total_count - len(topics)
        
        for i in range(needed):
            idx = len(topics) + 1
            template_idx = i % len(title_templates)
            topics.append({
                "id": idx,
                "type": "comparison",
                "title": title_templates[template_idx],
                "platform": random.choice(platforms),
                "keywords": [industry, brand_name],
                "competitors_to_mention": []
            })
        
        return topics


# 存储正在运行的任务
running_tasks: Dict[str, BatchArticleGenerator] = {}


async def start_batch_generation(
    diagnosis_id: int,
    article_distribution: Dict[str, int],
    topics: List[Dict] = None,
    distilled_data: Dict = None,
    brand_id: int = None,  # V2新增：客户brand_id用于检索知识库
    industry: Optional[str] = None,  # v2.7.4 GEO 文体改造 · 用于医疗/法律 hard rule
) -> Dict:
    """
    启动批量生成任务

    Args:
        diagnosis_id: 诊断记录ID
        article_distribution: 文章分配
        topics: 用户确认的标题列表（可选，跳过TopicDispatcher）
        distilled_data: 缓存的蒸馏结果（可选，跳过DistillerPipeline）
        brand_id: 客户brand_id（V2新增，用于检索客户专属知识库）
        industry: 行业 key(v2.7.4 · 医疗 / 法律 hard rule 必传 · 缺失时从诊断记录补)

    Returns:
        任务信息
    """
    generator = BatchArticleGenerator(
        diagnosis_id,
        article_distribution,
        topics=topics,
        distilled_data=distilled_data,
        brand_id=brand_id,  # V2新增：传入brand_id
        industry=industry,  # v2.7.4
    )
    running_tasks[generator.task_id] = generator
    
    # 异步启动生成
    asyncio.create_task(generator.generate_all())
    
    return {
        "task_id": generator.task_id,
        "status": "processing",
        "articles_generated": 0,
        "articles_total": generator.total_count,
        "progress_url": f"/api/articles/progress/{generator.task_id}"
    }


def get_task_progress(task_id: str) -> Dict:
    """获取任务进度"""
    generator = running_tasks.get(task_id)
    if not generator:
        return {"error": f"未找到任务: {task_id}"}
    
    # 计算预估费用
    from db.diagnosis_db import calculate_token_cost
    token_usage = generator.total_token_usage.copy()
    token_usage["estimated_cost"] = calculate_token_cost(
        "deepseek-v4-pro",  # 默认模型(2026-05-09 升级 · 写文章用 pro 展示质量)
        token_usage["input_tokens"],
        token_usage["output_tokens"]
    )
    
    return {
        "task_id": task_id,
        "status": generator.status,
        "articles_generated": generator.generated_count,
        "articles_total": generator.total_count,
        "logs": generator.logs,  # Return logs
        "articles": generator.articles if generator.status in ["completed", "completed_with_errors"] else [],
        "token_usage": token_usage  # 新增：Token使用统计
    }


# ============= 分级文章生成 [Phase 4] =============

async def generate_tiered_article(
    topic: Dict,
    client_data: Dict,
    output_dir: Optional[Path] = None
) -> Dict:
    """
    [Phase 4] 根据词级生成文章
    
    Args:
        topic: 选题信息（含tier, keyword, type等）
        client_data: 客户信息
        output_dir: 输出目录
        
    Returns:
        生成结果
    """
    tier = topic.get("tier", "tier3")
    keyword = topic.get("keyword", "")
    article_type = topic.get("type", "ranking")
    title = topic.get("title", "")
    
    # AI友好的写作风格（按词级 + 文章类型）
    # 核心原则：开头直接回答、结构清晰、有具体数据
    TIER_WRITING_STYLES = {
        "tier1": {
            "style": "authoritative_ai_friendly",
            "tone": "专业权威、数据支撑、明确结论",
            "length": "1500-2000字",
            "ai_hook": "开头100字必须直接给出核心答案/排名，让AI可以引用",
            "requirements": [
                "开头直接给答案（如：经过实测，XX是最推荐的）",
                "使用表格对比（AI更容易提取结构化信息）",
                "引用具体数字/数据",
                "结尾有明确总结"
            ]
        },
        "tier2": {
            "style": "local_ai_friendly",
            "tone": "本地化、具体实用",
            "length": "1200-1500字",
            "ai_hook": "开头明确地域关键词，如'在深圳做XX...'",
            "requirements": [
                "包含地域关键词",
                "本地案例或报价",
                "结构化列表",
                "简洁明了"
            ]
        },
        "tier3": {
            "style": "qa_ai_friendly",
            "tone": "直接回答、问题导向",
            "length": "800-1200字",
            "ai_hook": "开头直接回答问题，不要'众所周知'式开头",
            "requirements": [
                "第一句话就回答问题",
                "有具体数字（如价格区间、时间周期）",
                "1/2/3步骤清晰",
                "避免长段落"
            ]
        }
    }
    
    # 文章类型特定要求（与tier配合使用）
    TYPE_PROMPTS = {
        "ranking": {
            "focus": "权威榜单对比",
            "structure": "开头结论 → TOP列表 → 详细对比 → 总结推荐",
            "must_have": ["明确排名", "对比表格", "推荐理由"]
        },
        "qa": {
            "focus": "直接回答问题",
            "structure": "开头答案 → 详细解释 → 注意事项 → 总结",
            "must_have": ["直接回答", "具体数字", "避坑建议"]
        },
        "case": {
            "focus": "真实案例分享",
            "structure": "背景 → 选择过程 → 实际效果 → 感受总结",
            "must_have": ["具体场景", "真实体验", "效果数据"]
        }
    }
    
    writing_style = TIER_WRITING_STYLES.get(tier, TIER_WRITING_STYLES["tier3"])
    
    try:
        from writing.article_writer import ArticleWriter
        
        # 构建增强的蒸馏数据
        distilled_data = {
            "client_profile": json.dumps({
                **client_data,
                "target_keyword": keyword,
                "keyword_tier": tier
            }, ensure_ascii=False),
            "selling_points": json.dumps(client_data.get("selling_points", {}), ensure_ascii=False),
            "competitor_analysis": json.dumps({"competitors": []}, ensure_ascii=False)
        }
        
        # [2026-06-02 P1 Codex] 分级/旧写作路径也传 brand_id,让 structured_knowledge 5 维画像注入生效
        # (client_data 含 brand_id 则注入,无则 None 不回归 · 对齐 generate_single_article L196)
        writer = ArticleWriter(distilled_data, brand_id=client_data.get("brand_id"))
        
        # 增强topic
        enhanced_topic = {
            **topic,
            "writing_style": writing_style["style"],
            "writing_requirements": writing_style["requirements"],
            "target_length": writing_style["length"]
        }
        
        result = await writer.write(enhanced_topic)
        content = result.get("content", "")
        
        # 保存文章
        if output_dir and content:
            output_dir.mkdir(parents=True, exist_ok=True)
            safe_keyword = keyword.replace("/", "_").replace("\\", "_")[:20]
            filename = f"{tier}_{article_type}_{safe_keyword}.md"
            filepath = output_dir / filename
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
            return {
                "success": True,
                "content": content,
                "word_count": count_effective_chars(content),
                "file_path": str(filepath),
                "topic": enhanced_topic
            }
        
        return {
            "success": True,
            "content": content,
            "word_count": count_effective_chars(content),
            "topic": enhanced_topic
        }
        
    except Exception as e:
        return {
            "success": False,
            "error": str(e),
            "topic": topic
        }


class TieredBatchGenerator:
    """
    [Phase 4] 分级批量文章生成器
    
    根据TieredTopicDispatcher生成的选题列表批量生成文章
    """
    
    MAX_CONCURRENT = 3
    
    def __init__(
        self,
        topics: List[Dict],
        client_data: Dict,
        package: str = "standard"
    ):
        self.topics = topics
        self.client_data = client_data
        self.package = package
        self.total_count = len(topics)
        self.generated_count = 0
        self.failed_count = 0
        self.articles = []
        self.errors = []
        self.status = "pending"
        self.task_id = f"tiered_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    
    async def generate_all(self, progress_callback=None) -> Dict:
        """批量生成所有文章"""
        self.status = "processing"
        
        output_dir = Path(f"output/articles/{self.task_id}")
        output_dir.mkdir(parents=True, exist_ok=True)
        
        semaphore = asyncio.Semaphore(self.MAX_CONCURRENT)
        
        async def generate_one(topic):
            async with semaphore:
                result = await generate_tiered_article(
                    topic=topic,
                    client_data=self.client_data,
                    output_dir=output_dir
                )
                
                if result.get("success"):
                    self.articles.append(result)
                else:
                    self.failed_count += 1
                    self.errors.append({
                        "topic": topic,
                        "error": result.get("error")
                    })
                
                self.generated_count += 1
                
                if progress_callback:
                    progress_callback(
                        self.generated_count,
                        self.total_count,
                        topic.get("keyword", "")
                    )
        
        await asyncio.gather(*[generate_one(t) for t in self.topics])
        
        self.status = "completed" if self.failed_count == 0 else "completed_with_errors"
        
        # 打印摘要
        tier_summary = {"tier1": 0, "tier2": 0, "tier3": 0}
        for article in self.articles:
            tier = article.get("topic", {}).get("tier", "tier3")
            tier_summary[tier] = tier_summary.get(tier, 0) + 1
        
        print(f"\n   ✅ 分级文章生成完成:")
        print(f"      成功: {len(self.articles)}篇 | 失败: {self.failed_count}篇")
        print(f"      一级词: {tier_summary['tier1']}篇 | 二级词: {tier_summary['tier2']}篇 | 三级词: {tier_summary['tier3']}篇")
        
        return {
            "task_id": self.task_id,
            "status": self.status,
            "articles_generated": len(self.articles),
            "articles_failed": self.failed_count,
            "tier_summary": tier_summary,
            "output_dir": str(output_dir),
            "articles": self.articles[:5],  # 只返回前5篇摘要
            "errors": self.errors[:3] if self.errors else None
        }
