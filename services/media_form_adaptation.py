"""P1-3 · 媒体形态分档 + 署名适配(2026-08-14)。

研究定稿(§B 渠道口吻)定位的缺口:publication_profile(生成时)与媒体分档
(下单选媒时)「从不相遇」,渠道渲染只做联系方式软化,**无署名/口吻适配**。
本模块把 `media_domain_directory.media_form` 四档接进发布渲染:

  portal_site      门户主站(真编辑媒体)   → 联系方式 policy 压到最严 none,不加署名
  vertical_media   垂直媒体(编辑属性)     → 同上
  platform_account 平台号(自发布频道)     → policy 不加严;**禁止任何编辑归属形态署名**
  self_site        自站(企业自有站点)     → 加诚实署名(是自己的站,署名 = 结构对称)
  ''/未分档                              → 一切不变(O1:目录缺失走既有降级)

🔴 分档判据(工单红线):**任何一档**的署名段都不得产出「据XX报道」「本报讯」
「XX 讯」「记者」类编辑归属形态 —— 平台号内容伪装编辑报道 = 假冒媒体归属
(Owner 亲裁类别②,R3-A1 降档裁定同源)。这一条由 `_EDITORIAL_ATTRIBUTION`
反向锁死,变异点:往 byline 模板塞「据XX报道」→ 锁转红。

分类:O1 —— 形态取不到/未分档时零行为变化,绝不阻断发布主链。
"""
from __future__ import annotations

import re
from typing import Final, Iterable

FORM_PORTAL_SITE: Final = "portal_site"
FORM_PLATFORM_ACCOUNT: Final = "platform_account"
FORM_VERTICAL_MEDIA: Final = "vertical_media"
FORM_SELF_SITE: Final = "self_site"
VALID_MEDIA_FORMS: Final = frozenset({
    FORM_PORTAL_SITE, FORM_PLATFORM_ACCOUNT, FORM_VERTICAL_MEDIA, FORM_SELF_SITE,
})

#: 编辑属性档:联系方式 policy 压到最严(编辑媒体不允许导流尾巴,与 strict
#: profile 的「不输出导流话术」同一方向,但作用在**渠道**轴而非**生成**轴)。
_EDITORIAL_FORMS: Final = frozenset({FORM_PORTAL_SITE, FORM_VERTICAL_MEDIA})

#: 编辑归属形态(署名段的绝对禁区,四档一致)。
_EDITORIAL_ATTRIBUTION: Final = re.compile(r"据.{0,12}报道|本报讯|[^\s]{2,8}讯[：:）)]|记者")


def normalize_media_form(raw: object) -> str:
    """归一形态档;空/非法 → ``""``(未分档 = 一切不变)。"""
    key = str(raw or "").strip().lower()
    return key if key in VALID_MEDIA_FORMS else ""


def floor_contact_policy(policy: str, media_forms: Iterable[str]) -> str:
    """编辑属性媒体在场 → 联系方式 policy 压到 'none'(取最严,只收紧不放宽)。

    未分档/空集合 → 原 policy 原样返回(O1)。"""
    forms = {normalize_media_form(f) for f in (media_forms or [])}
    if forms & _EDITORIAL_FORMS:
        return "none"
    return policy


def media_form_byline(media_forms: Iterable[str], brand_name: str) -> str:
    """按形态档产出署名段(可直接 append 到发布正文末尾;空串 = 不加)。

    - **只有 self_site 产出署名**:企业自站署自己的名是诚实且结构对称的
      (真实自站内容都有署名;缺署名才是结构不对称的 tell);
    - platform_account / 编辑档 / 未分档一律空串 —— fail-closed:
      给不出诚实署名就不署,绝不产出伪装编辑归属的形态;
    - 混合下单(含任何非 self_site 档)→ 空串:同一份正文发多形态媒体时
      按最保守的档走,与 strictest_policy 同一方向。
    """
    brand = str(brand_name or "").strip()
    forms = {normalize_media_form(f) for f in (media_forms or []) if str(f or "").strip()}
    if not brand or not forms or forms != {FORM_SELF_SITE}:
        return ""
    byline = f"—— 本文由 {brand} 发布"
    # 结构性自证:署名模板永不落入编辑归属形态(判据与测试同源,双保险)。
    if _EDITORIAL_ATTRIBUTION.search(byline):  # pragma: no cover - 模板被改坏才会进
        return ""
    return byline
