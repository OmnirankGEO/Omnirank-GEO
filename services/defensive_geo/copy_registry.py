"""对外文案单一 SSOT(规格 §0.5.5 U-1 / U-2 / U-10 @ spec e710be6c2)。

用户基准 = **40 岁非技术销售**。她不需要理解任何机制就能走完整条旅程。

为什么是「registry」而不是「前端写死」
------------------------------------
U-1:同一概念全站唯一叫法,**内部枚举裸串上屏 = 验收红**。
U-10:对外文案 census 从 copy registry **机械导出**。

所以这里是**数据 + 一道门**,不是一份词表文档:
  · ``user_label(kind, code)`` 拿不到译文就抛 —— 前端不得自造;
  · ``assert_public_copy_clean(text)`` 扫内部词,命中即抛 —— 裸枚举上不了屏;
  · ``census()`` 导出全量,判据拿它当分母对账,不手抄。

🔴 内部词表(``_INTERNAL_TERMS``)是**结构锚**,不是词表洁癖:
   它防的是「后端图省事把 enum 直接塞进 label」。判的是**形态**
   (「这串东西是不是内部标识符」),不是「哪些词不好听」——
   补一个词漏三个词那种判法本仓明令禁止。
"""

from __future__ import annotations

import re
from typing import Any

#: 改任何一条译文都必须升版:旧前端缓存了旧文案,版本号是唯一能对上的锚。
# [合流 2026-08-24 Review-CTO] v3(F/G/E 累积)与 v2(包H 追加 U-5/U-6/U-9/Z-3)
# 在此汇成 v4:合并后的词表与两个父版本都不同,按「改任何一条译文都必须升版」升。
# [门八第二发现 ④ 2026-08-30] v5:改了 ``insufficient_points`` 那一句
# (原文承诺「题单不会丢」,实测她去充值回来草稿全没了)。按上面那条规矩升版。
# [P2-RUNTIME-1 2026-08-31] v6:新增 ``progress_channel_degraded``。按上面那条规矩升版。
# [#58 2026-09-04] v7:新增 ``idempotency_key_missing`` 与动作 ``refresh_and_retry``。
# [#62 2026-09-05] v8:新增 ``question_plan_superseded``（confirm 时题单已被改版）。
# [#63 2026-09-05] v9:新增 ``five_card_summary_unavailable``（五卡投影为空时的回落说明）。
# [#97 2026-09-05] v10:新增动作 ``confirm_run_preview``。
#   🔴 open(待确认)态原来给的是 ``wait``（「稍等，正在体检」），
#   同一份响应里 canConfirm 又是 true —— 「你可以确认」和「请等待」同时说。
#   两档处置相反:一个要她点确认,一个要她别动。压成一档时她只能猜。
#   🔴 它替代的是「渲染五张空壳、每张写『暂无结论』」——
#   「这一项没测到」与「整份没有五卡投影」处置相反:前者继续看别的卡,
#   后者这个视图根本不适用,该直接给她完整报告。压成一档她就只剩五个空壳和零出口。
#   🔴 与 ``question_plan_not_runnable`` **刻意分开**：那条是「你一开始就选了旧版」，
#   这条是「你手上这份 preview 本来有效，是在你决定期间题单被改了」——
#   处境不同，她要做的事也不同（前者去最新版重新发起，后者刷新重算价再确认），
#   而且这条必须带「没有扣除任何算力」：她刚点了确认，第一反应是钱有没有被划走。
#   🔴 我一度判定「新增不算改、不必升版」,理由是新增不会让旧前端缓存的句子失效。
#   **反证就在上一行**:v6 正是为「新增 progress_channel_degraded」升的。
#   ⇒ 规则文本写的是「改任何一条译文」,而**本文件自己的先例**把「新增」也算在内 ——
#      规则怎么解释,看它历史上被怎么用,不看字面。
COPY_REGISTRY_VERSION = "defensive-geo-copy-v11"


# ══════════════════════════════════════════════════════════════════════════
# U-2 状态词全量补译。**译文逐字取自规格 §0.5.5 U-2**,不是我自己编的。
# ══════════════════════════════════════════════════════════════════════════
_RUN_STATE: dict[str, str] = {
    "queued": "排队中",
    "running": "正在体检",
    "settlement_pending": "结果已出，费用结算中（无需操作）",
    "completed": "已完成",
    "needs_action": "需要你处理一下",
    "failed": "这次没能完成",
    "cancelled": "已取消",
    # 🔴 U-2 特别点名这一条:必须同时说清「不会多扣」,否则销售看到
    #    「隔离」两个字第一反应是「我的钱是不是没了」。
    "quarantined": "结果待平台核实，费用已冻结、不会多扣（无需操作）",
}

_FUNDING_STATE: dict[str, str] = {
    "frozen": "算力已冻结（本次体检预留）",
    "committed": "算力已扣除",
    "released": "算力已退回",
    "pending_reconciliation": "正在向平台核实结果，费用已冻结",
    "quarantined": "费用待平台核实，已冻结、不会多扣",
    "exempt_recorded": "本次不从你的算力扣除",
}

#: preview 生命周期。**不是** settlement 状态(§3.5 禁第二套 enum),
#: 所以译文刻意不用「已结算/已完成」这类钱味词。
_PREVIEW_LIFECYCLE: dict[str, str] = {
    "open": "待确认",
    "expired": "已过期，需要重新发起",
    "consumed": "已确认，体检已开始",
}

_MODE: dict[str, str] = {
    # U-1 裁定名。废除的别名(「主动获客」「主动推荐」「一起做」)不在表里 ——
    # 不在表里就取不到,取不到就上不了屏。
    "defensive": "先守住品牌",
    "offensive": "主动抢推荐",
    "hybrid": "两条线一起看",
}

_FUNDING_POLICY: dict[str, str] = {
    "personal_wallet": "从你的算力扣",
    "organization_budget": "走团队预算",
    "admin_platform_ledger": "平台承担",
    "sponsor_platform_ledger": "平台赞助承担",
}

_APPROVAL_STATE: dict[str, str] = {
    "not_required": "无需审批",
    "required": "需要负责人批准",
    "approved": "负责人已批准",
    "rejected": "负责人未批准",
}

#: typed nextAction 的按钮文案。铁律(§0.5.6):任何阻塞必须自带解决方案 ——
#: 所以每个 reasonCode 都必须在这里找得到一句「点哪」。
_ACTION_LABEL: dict[str, str] = {
    "wait": "稍等，正在体检",
    "confirm_run_preview": "确认并开始体检",
    "view_result": "查看体检结果",
    "create_diagnosis_preview": "重新发起体检",
    "new_preview": "重新发起体检",
    "edit_question_plan": "少测几个问题",
    "reduce_plan": "少测几个问题",
    "top_up": "去充值算力",
    "request_approval": "请负责人批准",
    "request_budget_approval": "申请追加团队预算",
    "contact_owner": "联系团队负责人",
    "contact_support": "联系平台客服",
    "view_existing_command": "查看已开始的这次体检",
    "review_identity": "确认一下是不是你的品牌",
    "change_plan": "调整题单",
    "continue_editing": "继续编辑题单",
    "create_new_revision": "在这一版基础上改",
    "view_only": "只看这一版",
    "review_question_plan": "去看这份题单",
    # ── 返修③ ──────────────────────────────────────────────────────
    "sign_in": "去登录",
    # ── 发布面(窗C · WP5+WP6 · §15.7)────────────────────────────────
    # 🔴 全部按 §0.5.5 U-1「同一概念全站唯一叫法」+ 40 岁非技术销售基准写:
    #    每一句都回答「点了会发生什么」,不出现 snapshot / command / slot /
    #    revision / override 这类工程词。
    "view_status": "看这次发布进行到哪了",
    "review_new_snapshot": "去看新的媒体方案",
    "review_adjustment_options": "看看有哪些办法",
    "review_replacement_policy": "看看能不能补发一篇",
    "verify_outcome": "去核实发布结果",
    # ── 工单 E3-2 · 广告法修复的两条 typed 下一步 ──────────────────────
    "retry_legal_repair": "再改一次",
    "legal_repair_pick_again": "重新选要改的那一段",
    # ── 工单 E3-4 · P1-10 老链监测 ────────────────────────────────────
    "view_legacy_monitoring": "去看这次监测的结果",
    "confirm_publish_decision": "确认这个媒体方案",
    "override_publish_decision": "换一家媒体",
    "cancel": "先不选这家媒体",
    "retry_child": "重新发一次",
    "repair_legal_passage": "修好这一句再发",
    "new_customer_snapshot": "重新出一份报价给客户确认",
    "reduce_provider_optional_scope": "少投一点",
    "handoff_owner": "联系平台处理这一篇",
    # ── 门二 G5:原来"没有下一步"的那几个 code 补上 ──────────────────
    # 铁律(§0.5.6):任何阻塞必须自带解决方案。没有下一步的错误 = 死路。
    "fix_input": "返回修改",
    # 🔴 [#58 2026-09-04] 缺 Idempotency-Key 用它,不用 fix_input。
    #    "返回修改" 会让她去改自己的输入 —— 而她的输入没有任何问题,
    #    少的是前端该自动带上的那个标识。指错方向的动作比没有动作更糟。
    "refresh_and_retry": "刷新页面再试一次",
    "back_to_list": "返回列表",
    "retry_later": "稍后再试一次",
    # 门二 G6:平台直营账号发起时的出口。不是"联系客服" ——
    # 她手边就有可用的服务商账号,直接说换哪个,比让她去等客服快得多。
    "switch_to_agent_account": "换服务商账号发起",
    # ── 包E:执行预算/通道未就绪时的出口 ──────────────────────────────
    # 「稍后再试一次」已经有了(retry_later),但这两格她还可以做一件更有用的事:
    # 回到这一单看看整体进度,而不是干等。
    "view_order_progress": "看看这一单的进度",
    # ── 包H · U-5 交付待办清单 ──────────────────────────────────────
    # 🔴 12~50 轮确认没有聚合页 = 验收红(U-5 逐字)。这两句是**入口**文案:
    #    她在别处看到「还有 N 项等你确认」时,点的就是这个。
    "open_delivery_todo": "去看这一单还有哪些要确认",
    "confirm_next_pending": "确认下一项",
    # ── 包H · U-6 回环自动化 ────────────────────────────────────────
    # 「价格没变,直接确认」是 U-6 逐字点名的那句突出提示。它是**按钮文案**
    # 而不是提示条:过期重建之后她最需要的是"我可以直接按下去"这个确定性。
    "confirm_unchanged_price": "价格没变，直接确认",
    "review_new_price": "看看新价格再确认",
    "open_approved_confirm": "去确认这一单",
    # ── 包H · Z-3.1 第四出口「AI 联网补齐」────────────────────────────
    # 🔴 复用现役 autofill_brand 能力。文案不写价格数字 ——
    #    多少算力由服务端实时签发(G-6:数字禁进代码/文案)。
    "ai_autofill_facts": "让 AI 联网帮你查",
    "confirm_ai_facts": "就用这些资料",
    "edit_facts_myself": "我自己填",
    "cancel_ai_autofill": "先不查",
    # 行业缺失时的显眼入口(Owner 2026-08-24:防守模式行业不强制,
    # 但 AI 要尽量填全)。与 ai_autofill_facts 分开是因为**落点不同**:
    # 这一颗只补一个字段,回到原来那一屏;上面那颗补全套。
    "ai_fill_industry": "让 AI 帮你查行业",
    # ── 包H · Z-3.3 广告法一键修复 ──────────────────────────────────
    # 🔴 规格明令禁止实现成纯手工文本框 —— 所以第一颗按钮是「看 AI 改好的」,
    #    不是「去编辑」。手工微调是**第二步**,不是唯一出口。
    "view_legal_repair_candidates": "看 AI 改好的版本",
    "apply_legal_repair": "用这一句，重新确认",
    "edit_legal_repair": "再改一下这句话",
    # [工单 C-5] 落库之后的下一步。"用这一句"现在**真的**改了正文,
    # 所以她的下一步不再是"回写作页自己贴一遍",而是直接回去重新确认。
    "retry_publish_confirm": "回去重新确认这一篇",
    # ── 包H · U-9 发给客户统一面板 ───────────────────────────────────
    "open_customer_links": "发给客户",
    "copy_customer_link": "复制链接和话术",
    "reissue_customer_link": "重新签发一个新链接",
}

#: 阻塞原因的人话解释。U-2 明确:``IDEMPOTENCY_CONFLICT`` 前端静默处理、永不上屏,
#: 因此它**刻意不在表里** —— 取不到就上不了屏,这比写一句"请勿重复提交"更硬。
_REASON_EXPLANATION: dict[str, str] = {
    "approval_required": "这次体检需要团队负责人先批准，批准后就能开始。",
    # 🔴 [#157 · v11] 题面超限。**带占位符的模板,唯一一份**。
    #    A 的 CTA 要在提交前就禁用并显示同一句 —— 后端内联 f-string 的话,
    #    前端只能照抄一遍,而两份文案漂开时表现是「弹窗说 100 字、按钮说别的」,
    #    两边各自看都正常。跨层同源就走这个注册表(它有前端镜像 +
    #    build 链 verify + presign copy_version 三道),不另造第二套。
    #    占位符先例见 quote.quoted 的 {amount}。
    "question_too_long": (
        "第 {ordinal} 题太长了：{chars} 字，上限 {limit} 字。"
        "「{excerpt}…」—— 请把这道题改短一点再确认。"
        "（常见原因：客户名很长，系统按名字拼出来的题就超了。）"
    ),
    "approval_rejected": "负责人这次没有批准。你可以联系他了解原因，或者调整题单后再申请。",
    # 🔴 [门八第二发现 ④] 原文是「充值后可以直接继续，题单不会丢」。实测:她点去 /wallet
    #    再回来,模式与已填的问题**全部重置**(那份草稿只活在 NewDiagnosis 的组件 state 里,
    #    没有任何持久化;服务端 question_plans 确实留了一行,但页面没有任何入口能把它取回来)。
    #    Owner 铁律「不承诺做不到的事」⇒ 改成她回来之后真的能做到的那一件,
    #    并保留她最担心的那件事的答案:不会因为重来一次而被扣两次。
    "insufficient_points": "你的算力不够这次体检。充值之后回来重新发起一次即可，不会重复扣算力。",
    # 🔴 [P2-RUNTIME-1 2026-08-31] 实时通道(Redis)失联、而 run 行说这次体检还在跑时,
    #    进度端点回的那句话。三条约束:不承诺时间(说不出来)、不说死原因
    #    (说死一次说错,她以后不会再信任何一句)、明示钱没事(这条路径资金相邻)。
    "progress_channel_degraded": "这次体检还在进行中。实时进度暂时看不到，稍后会自动接上；不会因此多扣算力。",
    # U-2 逐字裁定
    "preview_expired": "刚才那一步已过期，请重新发起；没有扣除任何算力。",
    "already_consumed": "这次体检已经开始了，点下面可以看进度。",
    "policy_unavailable": "这条付费方式暂时用不了，帮你转给平台客服处理。",
    # 🔴 [工单 V5-B · Codex fix-of-fix3 P2-NEW-5] 与上一条**刻意分开**。
    #
    #    上一条那句「帮你转给平台客服处理」配的是 ``contact_support`` 动作,
    #    现役五个 ``POLICY_UNAVAILABLE`` 出口里有四个确实是那个意思
    #    (价目读不出来 / 算不出价 / 资金投影不合法)—— 那几处不动。
    #
    #    但 confirm 里「她预览之后这一项被停用了」那一个出口给的是
    #    ``new_preview``。同一句话配两种动作,屏幕上就会同时出现
    #    「帮你转给平台客服处理」和一颗写着「重新发起体检」的按钮 ——
    #    她读到的是"等人处理",看到的是"自己再来一次",两条指令互相打架。
    #
    #    措辞上刻意**不把原因说死**:这一支的触发条件是「锁不到 is_active 的价目行」,
    #    最常见的是被停用,但不是唯一可能。所以只说"现在暂时不能开始",
    #    不说"它已经被下架了"——说死一次说错,她以后不会再信任何一句。
    "pricing_deactivated_after_preview": (
        "这一项现在暂时不能开始。重新发起一次就能继续；没有扣除任何算力。"
    ),
    "question_plan_not_runnable": "这份题单已经有更新的版本了。在最新那一版上再发起体检就行。",
    "question_plan_superseded": "这份题单在你确认之前又更新了一版。刷新页面会按最新那一版重新算一次价，再确认即可；这次没有扣除任何算力。",
    "five_card_summary_unavailable": "摘要卡片这次没能生成，下面是完整报告，内容一条都不少。",
    "snapshot_changed": "价格或题单在你确认前变了，已经帮你重新算好，请再确认一次。",
    # ── 门二 G5:补齐"曾经只回一个裸信封"的那几个 ────────────────────
    # 它们过去只返回 {"code": ..., "retryable": ...} —— 她看到的是一片空白,
    # 然后只能去猜。§15.8 每一行都有"用户下一步",这几行以前是空的。
    # ── [2026-09-02] 题单校验:每条规则一句人话 ──────────────────────
    # 以前所有 PlanIdentityError 都落下面那句通用 validation_failed ——
    # 她看得到"有一处填得不对",却不知道是哪一处、也没有下一步可点。
    # 开发原则「提示二选一」:要么有明细 + 有修复动作,要么不显示。
    # 🔴 措辞不用「defensive/offensive/mode_side」这类工程词,
    #    用她自己的说法:「客户会问的」/「客户会搜的」。
    "plan_empty": "这份题单还一道题都没有。先加一道你想测的问题,再开始体检。",
    "plan_hybrid_needs_both_sides": (
        "混合体检要两类问题各至少一道:客户会问的 + 客户会搜的。"
        "现在有一类是空的,补一道就能继续。"
    ),
    "plan_side_mismatch": (
        "这次选的体检类型只测一类问题,题单里却混进了另一类。"
        "把多出来的那几道删掉,或者改成混合体检。"
    ),
    "plan_question_blank": "有一道题还是空的。把它填上,或者删掉这一行,就能继续。",
    "plan_identity": (
        "题单的编号对不上了(可能是刚才删改时漏了一步)。"
        "回去重新生成一次题单就能继续;没有扣除任何算力。"
    ),
    "validation_failed": "这次提交的内容有一处填得不对，返回改一下就能继续；没有扣除任何算力。",
    # 🔴 [#58 2026-09-04] 与 validation_failed 分开的理由:
    #    「你忘了带标识」与「你的内容填得不对」原来给的是**同一句话**,
    #    她照那句去检查自己填的内容,而问题根本不在那里 ——
    #    一句具体但指错方向的提示,比笼统的提示更糟(它让人停止寻找)。
    #    这个标识是前端自动带的,不是她填的,所以动作是「刷新重试」不是「返回修改」。
    "idempotency_key_missing": "这次请求少了一个防重复提交的标识，刷新页面再试一次即可；没有扣除任何算力。",
    "not_found": "没找到这条记录，它可能已经被删除，或者不在当前账号名下。",
    "forbidden": "当前账号没有这项操作的权限。",
    "internal_error": "系统这次没能处理完，已经记录下来了；没有扣除任何算力，稍后再试一次。",
    # ── 门二 G6:平台直营账号发起 ────────────────────────────────────
    # 逐字用返修单给的那句(她能直接照做,不含任何工程词)。
    "platform_direct_unsupported": "平台直营账号暂不支持发起，请用服务商账号。",
    # ── 返修③ P0-2:选了我们不跑的检索平台 ──────────────────────────
    # 不报"白名单""platformKey"这类词;她要知道的只有两件事:
    # 哪里不对、钱有没有动。具体是哪几个平台由 details 之外的 publicExplanation
    # 说不出口(那是闭集),所以这句话必须自己站得住。
    "platform_not_available": "你选的检索平台里有我们暂时不支持的，换成可选的平台就能继续；没有扣除任何算力。",
    # ── 返修③ P1-5:没登录 ──────────────────────────────────────────
    # 过去这里返回的是 {"code": "NOT_AUTHENTICATED"} 裸信封 —— 前端拿不到
    # 可渲染的句子,只能自己编一个。
    "not_authenticated": "请先登录再继续，登录后这一页会自动打开。",
    # ── 终审 P0-1:发布链客户入口关闭(2026-08-23)──────────────────
    # 🔴 关的不是"出了故障",而是"这个功能还没做完就被接上了客户入口"。
    #    她之前的体验是:确认发布 → 算力被冻住 → 什么都没发生 →
    #    12 小时后钱莫名其妙退回来。宁可现在明说"还没开放",
    #    也不能让她付了钱去等一个不存在的执行器。
    #    文案两件事必须都在:①这是"还没开放"不是"你操作错了";②钱没动。
    "publish_entry_closed": "发布这一步还没有开放，暂时不能确认；没有扣除任何算力。",
    # (原「监测进度入口关闭」格与其注释已随包F「进度重开」删除;
    #  合流时包E 侧带回的同格已按 F 的收口意图不予复活 —— Review-CTO 合流记录)
    # ── 包E:执行预算还没准备好(2026-08-24)───────────────────────────
    # 🔴 这一格过去是**裸 500**(终审 P1-2「preview 对任何输入必 500」的真根因:
    #    provider 执行预算快照全仓零签发者 ⇒ check_and_lock_budget 必抛 ⇒
    #    兜底 except 转受控 500)。500 让她以为系统坏了,而实际是
    #    「这一单的准备工作还差一步」。所以必须是一句人话 + 一个出口。
    "execution_budget_not_ready": "这一单的准备工作还差一步，稍等一下再来确认；没有扣除任何算力。",
    # ── 包E:外发通道暂时不可用 ────────────────────────────────────────
    # 说「暂时」是因为它真的是暂时的(通道恢复后队列会自己继续),
    # 不写供应商名、不写配置项(POR-14 内部参数禁泄)。
    "publish_channel_unavailable": "发布通道暂时不可用，稍后再试；没有扣除任何算力。",
    # (「monitoring_progress_closed」在包H 侧随 base 带回,合流同样不予复活 ——
    #  包F「进度重开」已删该格,见上方合流记录。)
    # ── 包H · U-6 回环自动化 ────────────────────────────────────────
    # 🔴 preview 过期后前端**自动**重建,所以这两句的主语是"已经帮你…",
    #    不是"请你重新…"。她不需要"自己想起来"任何事(U-6 标题逐字)。
    "preview_rebuilt_price_unchanged":
        "刚才那一步已过期，已经帮你重新算过：价格没变，直接确认就行。",
    "preview_rebuilt_price_changed":
        "刚才那一步已过期，已经帮你重新算过；这次的价格有变化，请再确认一次。",
    # Z-4 边界:这条只进服务商工作台操作日志,**不进**客户 token/Portal 时间线。
    # 文案本身也按"后台已经处理完"写 —— 她读完不需要做任何事。
    "media_replaced_same_tier":
        "原媒体临时没档期，已经自动换成同角色、同预算的媒体；费用和交付承诺都没变，你不用做任何事。",
    # 真降档才回她重签。U-6 逐字给了这句话术,一字不改。
    "media_downgraded_needs_reconfirm":
        "原媒体临时没档期，费用已退回；换同级媒体请再确认一次。",
    # U-6 逐字的广告法话术。这一句同时承担三件事:说清哪里不对、说清钱、给出口。
    "legal_rule_hit_repairable":
        "这句话可能违反广告法（已标出位置），没有扣费；点这里修好这一句，重新确认即可发布。",
    # ── 包H · Z-3.1 第四出口 ────────────────────────────────────────
    # 🔴 不写"缺资料就没法继续" —— 现役元指令是**永远不中断**。
    #    所以这句话的重点是"有三条路都能走下去",而不是"你被拦住了"。
    "brand_facts_missing":
        "还差几项公司资料。可以让 AI 联网帮你查全套，也可以自己填；查之前会先告诉你要花多少算力。",
    "ai_autofill_needs_confirm":
        "这些是 AI 查到的，还没有生效；你确认之后才会写进这家公司的资料。",
    # Owner 2026-08-24:防守体检不强制行业。这句必须同时说清"不填也能做"
    # 和"填了更准",否则她会以为我们在偷偷降低标准。
    "industry_optional_defensive":
        "先守住品牌这条线不填所属行业也能做；填上结果会更准，可以让 AI 帮你查。",
    # ── 包H · U-9 发给客户 ──────────────────────────────────────────
    "customer_link_expired":
        "这个链接已经过期了，点一下就能重新签发一个新的发给客户；不额外扣算力。",
    "customer_link_revoked":
        "这个链接已经收回了，点一下可以重新签发一个新的发给客户；不额外扣算力。",
    # 🔴 [工单 V4-C · C-2] 原文结尾是「做完会自动出现在这里。」—— 那是一句
    #    **承诺**。它的前提是"有人在做前一步、做完系统会自己接上",而这一点
    #    我们保证不了(监测快照那一类就永远等不到,已 scoped-out)。
    #    改成只陈述**当前事实**,不预告系统行为。
    "customer_link_not_ready":
        "这一类链接现在还没有；要等前一步做完才会有。",
    # ── 包H · Z-3.3 广告法一键修复(工单 C-5:落库之后那一句)──────────
    # 🔴 只说**做到的那件事**:这一句已经写进稿子了。
    #    不写"现在可以发了"—— 发不发得出去要看重新确认那一跳的结果,
    #    我们这一步只保证她选的句子真的落了下去。
    "legal_repair_applied":
        "这一句已经改进稿子里了，回去重新确认一次就能继续；这一步没有扣算力。",
    # ── 工单 E3-2(Codex 二审 P1-F7 / P2)·「没改成」与「改哪一处说不清」──
    # 🔴 两句都必须先说**稿子有没有动**:她最怕的是"点了一下，现在稿子成什么样
    #    我不知道"。两句都以"一个字都没动"开头,不是客套。
    # 🔴 不出现 事务 / 回滚 / hash / 机审 / SAVEPOINT 这类工程词。
    "legal_repair_not_applied":
        "这一次没有改成，稿子一个字都没动；稍等一下再点一次就行。",
    # ── 工单 E3-4 · P1-10 ·「这次监测是老链跑的」────────────────────────
    # 🔴 必须先说**监测本身没事**:她最怕的是"我客户的监测是不是坏了"。
    #    不说"新版/旧版""v2"这类内部版本词 —— 对她只有一件事有意义:
    #    结果在另一个页面看得到。
    "monitoring_legacy_run":
        "这次监测是用原来的方式跑的，进度这一栏读不出来；结果本身没问题，"
        "在监测页面就能看到。",
    "legal_repair_ambiguous":
        "这句原话在稿子里出现了不止一处，我们不猜要改哪一处，稿子一个字都没动；"
        "请把要改的那一段选得更完整一些，再点一次。",
    # [工单 V3-C · C-5 · Codex 三审 P2-2] 这一条与上面那条的区别是**原因**,
    # 不是严重程度:上面是"定位不了",这一条是"这句话本身落不下去"
    # (空句 / 仍命中目录 / 与原句一模一样 / 原话已不在正文里)。
    # 两者都**不是**"我们的服务不可用" —— 自动重试永远不会成功。
    "legal_repair_input_rejected":
        "这一句没法落到稿子里，稿子一个字都没动；请改一下要替换的句子，或者"
        "重新选要改的那一段，再点一次。",
    # ── 包H · U-5 交付待办 ──────────────────────────────────────────
    # 「资金仍逐项」这半句必须在:她看到"一页确认完"时最怕的就是
    # "会不会一按就把整单的钱都扣了"。
    "delivery_todo_intro":
        "这一单要确认的媒体方案都在这里；每一项各自确认、各自计费，不会一次性全扣。",
    "delivery_todo_all_done":
        "这一单没有等你确认的项目了。",
}

# ══════════════════════════════════════════════════════════════════════════
# 🔴 发布域的**同名不同译**(窗C · 2026-08-21)
# ══════════════════════════════════════════════════════════════════════════
# 上面的 ``_RUN_STATE`` / ``_FUNDING_STATE`` 是**诊断域**的译文,里面逐字带
# 「体检」二字(U-1 裁定的对外主名)。发布链复用它们会出现一个具体的坏结果:
#
#   销售在**媒体发布**确认页按下确认后,U-4 那句「钱动了没有」渲染的是
#   ``funding_state.frozen`` ⇒ 她读到「算力已冻结(本次**体检**预留)」;
#   状态页 running 档读到「正在**体检**」。
#   —— 我们对着刚花钱的销售说错了她买的是什么。
#
# (2026-08-21 由前端窗的机械 census 抓到:含「体检」的条目共 11 条,
#  其中 2 条直接落在发布三页上。)
#
# 修法不是把诊断域的词改中性 —— 那会让体检页失去它的主名。
# 而是**按域拆**:同一个状态码在两个域有两套译文,调用方按自己的域取。
# U-1「同一概念全站唯一叫法」说的是**概念**唯一,不是字符串唯一;
# 「发布进行中」和「体检进行中」本来就是两个概念。
_PUBLISH_RUN_STATE: dict[str, str] = {
    "queued": "排队中，马上开始发布",
    "running": "正在发布",
    "accepted": "已受理，正在安排发布",
    "settlement_pending": "结果已出，费用结算中（无需操作）",
    "completed": "已完成",
    "needs_action": "需要你处理一下",
    "failed": "这次没能发出去",
    "cancelled": "已取消",
    "quarantined": "结果待平台核实，费用已冻结、不会多扣（无需操作）",
}

_PUBLISH_FUNDING_STATE: dict[str, str] = {
    "frozen": "算力已冻结（本次发布预留）",
    "committed": "算力已扣除",
    "released": "算力已退回",
    "pending_reconciliation": "正在向平台核实发布结果，费用已冻结",
    "quarantined": "费用待平台核实，已冻结、不会多扣",
    "exempt_recorded": "本次不从你的算力扣除",
}

#: §11.2 四角色的对客/对服务商解释。裸枚举(``authority_anchor``)上屏 = U-1 红,
#: 而套等级词(「暂无结论」)是第二重错 —— 所以这一格必须有自己的译文。
_MEDIA_ROLE: dict[str, str] = {
    "authority_anchor": "权威背书",
    "strong_vertical": "行业垂直",
    "broad_discovery": "广覆盖",
    "official_owned": "品牌自有",
}

#: 🔴 小榜「先示预估、确认后开始」(Owner 2026-08-24)。
#:
#: 键 = 现役 ``quote_state`` 的五个取值,**一个新枚举都不造**
#: (见 ``services/defensive_geo/xiaobang/compute_estimate.py`` 里的理由)。
#:
#: 两条硬要求逐条兑现:
#:   · 有价那一档必须逐字含 Owner 给的句式「本次预计消耗你的算力 X，确认后开始」;
#:   · **报不出价的每一档都必须把钱说死** —— §0.5.5 U-2 的「没有扣除任何算力」
#:     这半句永远显式说。她看到"算不出来"的第一反应是"那我是不是已经被扣了",
#:     不回答这个问题的文案等于没写。判据逐档钉这半句。
_COMPUTE_ESTIMATE: dict[str, str] = {
    "quoted": "本次预计消耗你的算力 {amount}，确认后开始",
    "pending_domain_adapter":
        "还要先选定发到哪个投放账号，才能算出这次要用多少算力。现在没有扣除任何算力。",
    "unavailable":
        "这次要用多少算力暂时算不出来，先不显示数字。现在没有扣除任何算力。",
    "not_applicable": "这一步不消耗算力。",
    "expired":
        "刚才那个算力预估已经过期，重新核对一下就能继续。没有扣除任何算力。",
}

#: 报不出价时的下一步。铁律(§0.5.6):任何阻塞必须自带解决方案 ——
#: 「算不出来」而没有下一步就是死路。``quoted`` 刻意**不在表里**:
#: 那一档的下一步就是页面上那个确认按钮,再给一句会变成两个按钮打架(U-3)。
_COMPUTE_ESTIMATE_NEXT_ACTION: dict[str, str] = {
    "pending_domain_adapter": "先选投放账号",
    "unavailable": "重新准备一次",
    "not_applicable": "直接继续",
    "expired": "重新核对",
}

#: 🔴 POR-13 漂移的用户面(Owner 2026-08-24 批)。
#:
#: 漂移命中**不静默拒单** —— 明示发生了什么,并按「内容/价格/权限」三档
#: 给**各自不同**的下一步(见 services/defensive_geo/xiaobang/drift_notice.py
#: 里为什么不能压成一句)。三档都逐字带「没有扣除任何算力」:
#: 她在确认页上被拦下来,第一反应就是"我是不是已经被扣了"。
_INTENT_DRIFT: dict[str, str] = {
    "content_changed":
        "这次要处理的内容有更新,和你刚才看到的不一样了。请重新核对一次再确认；没有扣除任何算力。",
    "price_changed":
        "内容没变，但这次要用的算力有变化。请看一下新的预估再确认；没有扣除任何算力。",
    "authority_changed":
        "你的权限或所属团队有变化，这次操作需要重新确认一次；没有扣除任何算力。",
}

#: 三档**各自不同**的下一步 —— 这正是 Owner 点名要区分的那一格。
_INTENT_DRIFT_NEXT_ACTION: dict[str, str] = {
    "content_changed": "看看有什么变化",
    "price_changed": "看新的算力预估",
    "authority_changed": "确认一下这个客户还归不归你",
}

_REGISTRY: dict[str, dict[str, str]] = {
    "run_state": _RUN_STATE,
    "funding_state": _FUNDING_STATE,
    "publish_run_state": _PUBLISH_RUN_STATE,
    "publish_funding_state": _PUBLISH_FUNDING_STATE,
    "compute_estimate": _COMPUTE_ESTIMATE,
    "intent_drift": _INTENT_DRIFT,
    "intent_drift_next_action": _INTENT_DRIFT_NEXT_ACTION,
    "compute_estimate_next_action": _COMPUTE_ESTIMATE_NEXT_ACTION,
    "media_role": _MEDIA_ROLE,
    "preview_lifecycle": _PREVIEW_LIFECYCLE,
    "mode": _MODE,
    "funding_policy": _FUNDING_POLICY,
    "approval_state": _APPROVAL_STATE,
    "action": _ACTION_LABEL,
    "reason": _REASON_EXPLANATION,
}


class CopyNotRegistered(KeyError):
    """没有登记译文。**抛,不回落成裸枚举** ——

    回落是最坏的选项:它让「忘了补译」表现成「界面上出现一个英文单词」,
    而那恰恰是 U-1 说的验收红,却没有任何东西会因此变红。
    """


def user_label(kind: str, code: str) -> str:
    """取人话。取不到就抛 —— 前端不得自造(U-2)。"""
    table = _REGISTRY.get(kind)
    if table is None:
        raise CopyNotRegistered(f"未知文案类别 {kind!r};合法 = {sorted(_REGISTRY)}")
    label = table.get(code)
    if not label:
        raise CopyNotRegistered(
            f"{kind}.{code!r} 没有登记对外文案({COPY_REGISTRY_VERSION})。"
            "内部枚举裸串上屏 = 验收红(§0.5.5 U-1);请先在 copy_registry 补译。"
        )
    return label


def try_user_label(kind: str, code: str) -> str | None:
    """明知可能没有译文时用(例如 U-2 规定永不上屏的 IDEMPOTENCY_CONFLICT)。"""
    return _REGISTRY.get(kind, {}).get(code) or None


# ══════════════════════════════════════════════════════════════════════════
# 内部词守卫 —— 判**形态**不判词表
# ══════════════════════════════════════════════════════════════════════════
#: 这些是确凿的内部标识符,出现在对客/对服务商可见文案里就是漏了。
#: 清单不求全 —— 求全是补一个漏三个。真正兜底的是下面的**形态**规则。
_INTERNAL_TERMS = (
    "run_status", "runState", "fundingState", "billing_mode", "preview_id",
    "canonical_hash", "idempotency", "snapshot_id", "plan_cell", "attempt_id",
    "H0-", "H0_", "SOV", "target_outcome", "brand_exposure",
    "diagnosis_runs", "defgeo_", "exempt_recorded", "pending_reconciliation",
)

#: 形态规则:``snake_case_identifier`` / ``SCREAMING_SNAKE`` / ``camelCaseIdent``
#: 出现在中文文案里,基本只可能是把枚举直接塞进了 label。
_SNAKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+){1,}\b")
_SCREAM = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+){1,}\b")


class PublicCopyLeak(ValueError):
    """对外文案里混进了内部标识符。fail-closed:不下发,而不是「过滤一下」。"""


def assert_public_copy_clean(text: object, *, field: str = "copy") -> str:
    """对外可见文案的出口门。非空 + 无内部词 + 无标识符形态。

    🔴 空也算红:``userLabel: ""`` 与「没有 userLabel」在前端是一回事,
       都会让她看到一个空白格,然后去猜。
    """
    if not isinstance(text, str) or not text.strip():
        raise PublicCopyLeak(f"{field} 为空 —— 空文案等于让用户自己猜(§0.5.5 U-2)")
    for term in _INTERNAL_TERMS:
        if term in text:
            raise PublicCopyLeak(f"{field} 含内部词 {term!r}:{text[:60]!r}")
    for pattern, why in ((_SNAKE, "snake_case 标识符"), (_SCREAM, "SCREAMING_SNAKE 枚举")):
        m = pattern.search(text)
        if m:
            raise PublicCopyLeak(
                f"{field} 含{why} {m.group(0)!r} —— 疑似把内部枚举直接塞进了文案:{text[:60]!r}"
            )
    return text


def census() -> dict[str, Any]:
    """U-10:对外文案 census 机械导出。判据拿它当分母,不手抄。"""
    return {
        "version": COPY_REGISTRY_VERSION,
        "kinds": sorted(_REGISTRY),
        "entries": {kind: dict(sorted(table.items())) for kind, table in sorted(_REGISTRY.items())},
        "total": sum(len(t) for t in _REGISTRY.values()),
        "internal_terms_guarded": len(_INTERNAL_TERMS),
    }
