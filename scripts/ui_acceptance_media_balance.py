#!/usr/bin/env python
"""媒体平衡前端验收实测（工单追加验收 1-12）。

不是静态检查：真起 Playwright，在 375px / 1440px 两种屏宽、明暗两套主题下走完整链路，
量对比度、量触摸目标 boundingBox、量相邻间距，逐步截图。

**为什么自己打桩而不用沙盒**：沙盒演示数据要靠教程 stage 推进，而 OnboardingContext
启动时会重写 `omnirank_onboarding_state`，stage 停在 idle → 项目/文章列表恒空。
这里直接在网络层按各接口真实返回形状打桩，页面跑的仍是真实代码路径，
复审方一条命令即可复跑。

用法:
    cd frontend && VITE_DEV_PORT=5230 npm run dev
    python scripts/ui_acceptance_media_balance.py --base http://127.0.0.1:5230
"""
from __future__ import annotations

import argparse
import json
import os
import sys

OUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "docs", "AI-CONTEXT", "ui-acceptance-2026-07-29",
)

QUOTE_ID = 999001

AUTH_ME = {
    "id": 9001, "username": "qa_agent", "display_name": "验收代理", "is_admin": False,
    "is_active": 1, "must_change_password": 0,
    "roles": [{"id": 2, "name": "agent", "display_name": "服务商"}],
    # hasModule() 判的是 `module:action` 前缀（AuthContext:817），不是裸模块名。
    "permissions": ["writing:view", "writing:edit", "publish:view", "publish:edit",
                    "brands:view", "brands:edit", "monitoring:view", "quotes:view"],
    "client_brand_ids": [1], "permission_version": 1,
    "agent_level": 1, "operating_for_agent": False, "operator_context": None,
    "team_context": None,
}

PROJECTS = {"success": True, "projects": [{
    "id": 1, "quote_ids": [QUOTE_ID], "brand_id": 1, "brand_name": "一路顺风出行服务",
    "industry": "装修", "keyword_count": 3, "total_required_articles": 6,
    "monthly_price": 3000, "writing_status": "completed", "confirmed_at": "2026-07-01T00:00:00Z",
}]}

ARTICLES = {"success": True, "articles": [{
    "id": 31001, "topic_id": 30001, "article_id": 31001,
    "title": "深圳装修公司哪家靠谱 · 2026 选型指南", "keyword": "深圳装修公司哪家靠谱",
    "article_style": "guide", "status": "completed", "is_optimize": False,
    "publication_eligible": True,
}]}

# 媒体名刻意分两类：含主干关键词(搜狐/网易) 与 不含的(汽车之家/土巴兔/太平洋)，
# 这样勾选后「大平台 / 行业网站」两个计数都能被分别验证会动。
MEDIA = [
    {"id": 50001, "media_id": 50001, "media_name": "搜狐网新闻（官方）", "price": 320,
     "our_price_yuan": 320, "our_price_points": 41600, "resource_type_name": "门户内频道",
     "portal_media": "搜狐", "inclusion_rate": "85%", "area": "全国", "geo_rank": 3,
     "geo_rank_platform": "豆包,DeepSeek", "authority_media": 1, "media_type": "media"},
    {"id": 50002, "media_id": 50002, "media_name": "网易新闻房产（GEO可发）", "price": 520,
     "our_price_yuan": 520, "our_price_points": 67600, "resource_type_name": "门户内频道",
     "portal_media": "网易", "inclusion_rate": "92%", "area": "全国", "geo_rank": 4,
     "geo_rank_platform": "豆包,Kimi,DeepSeek", "authority_media": 1, "media_type": "media"},
    {"id": 50003, "media_id": 50003, "media_name": "汽车之家（每日车讯汇）", "price": 280,
     "our_price_yuan": 280, "our_price_points": 36400, "resource_type_name": "行业垂直",
     "portal_media": "垂直媒体", "inclusion_rate": "78%", "area": "全国", "geo_rank": 2,
     "geo_rank_platform": "豆包", "authority_media": 0, "media_type": "media"},
    {"id": 50004, "media_id": 50004, "media_name": "土巴兔(家居资讯频道)", "price": 350,
     "our_price_yuan": 350, "our_price_points": 45500, "resource_type_name": "行业垂直",
     "portal_media": "垂直媒体", "inclusion_rate": "80%", "area": "全国", "geo_rank": 2,
     "geo_rank_platform": "Kimi", "authority_media": 0, "media_type": "media"},
    {"id": 50005, "media_id": 50005, "media_name": "太平洋家居网", "price": 380,
     "our_price_yuan": 380, "our_price_points": 49400, "resource_type_name": "行业垂直",
     "portal_media": "垂直媒体", "inclusion_rate": "82%", "area": "全国", "geo_rank": 3,
     "geo_rank_platform": "豆包,DeepSeek", "authority_media": 0, "media_type": "media"},
]

TRUNK = {"window_days": 180, "total_citations": 5238, "degraded_reason": "", "domains": [
    {"domain": "sohu.com", "citation_count": 1969, "strength_label": "引用强",
     "role_label": "大平台", "self_serve": False, "inventory_keyword": "搜狐"},
    {"domain": "163.com", "citation_count": 1483, "strength_label": "引用强",
     "role_label": "大平台", "self_serve": False, "inventory_keyword": "网易"},
    {"domain": "cnblogs.com", "citation_count": 961, "strength_label": "引用中",
     "role_label": "技术社区", "self_serve": True, "inventory_keyword": "博客园"},
    {"domain": "zhihu.com", "citation_count": 619, "strength_label": "引用中",
     "role_label": "问答和公众号", "self_serve": True, "inventory_keyword": "知乎"},
    {"domain": "toutiao.com", "citation_count": 425, "strength_label": "引用中",
     "role_label": "大平台", "self_serve": False, "inventory_keyword": "头条"},
]}

COMBO = {
    "advisory": True, "trunk_slots": 2, "vertical_slots": 3,
    "reason": "客户问「深圳装修公司哪家靠谱」这类问题时，AI 最常引用 to8to.com 27%、"
              "sohu.com 25%、cnblogs.com 14%（近 180 天共 131 次引用）",
    "slots": [],
    "substitutions": [{"domain": "cnblogs.com",
                       "substitution_note": "AI 常引用的 cnblogs.com（技术社区）我们暂时买不到发布位，已换成同类的 CSDN"}],
    "self_serve_offers": [{"domain": "zhihu.com", "self_serve_action": {
        "id": "self_serve_publish", "label": "用自己的知乎账号发（内容我们出）",
        "target": "publish_center_self_serve"}}],
}

RECOMMEND = {
    "status": "success", "success": True, "matched_industry": "装修",
    "media_generic": [m for m in MEDIA if any(k in m["media_name"] for k in ("搜狐", "网易"))],
    "media_vertical": [m for m in MEDIA if not any(k in m["media_name"] for k in ("搜狐", "网易"))],
    "wemedia_vertical": [], "wemedia_generic": [],
    "citation_ranking_signal": "observed_citation_v2g",
    "citation_weight_summary": None, "channel_advisories": [],
    "ai_citation_trunk": TRUNK, "combination_plan": COMBO,
}

MEASURE_JS = r"""
() => {
  // Tailwind v4 的调色板算出来是 oklch(...)，正则解析不了。
  // 用 canvas 让浏览器自己把任意 CSS 颜色归一化成 rgba 像素，任何色彩空间都吃得下。
  const _cv = document.createElement('canvas'); _cv.width = _cv.height = 1;
  const _cx = _cv.getContext('2d', { willReadFrequently: true });
  const parseRGB = (s) => {
    const v = String(s || '').trim();
    if (!v || v === 'transparent') return { r: 0, g: 0, b: 0, a: 0 };
    const m = v.match(/rgba?\(([^)]+)\)/);
    if (m) {
      const p = m[1].split(/[ ,\/]+/).filter(Boolean).map(x => parseFloat(x));
      if (p.length >= 3 && p.every(n => !Number.isNaN(n)))
        return { r: p[0], g: p[1], b: p[2], a: p.length > 3 ? p[3] : 1 };
    }
    try {
      _cx.clearRect(0, 0, 1, 1);
      _cx.fillStyle = '#000';
      _cx.fillStyle = v;
      _cx.fillRect(0, 0, 1, 1);
      const d = _cx.getImageData(0, 0, 1, 1).data;
      return { r: d[0], g: d[1], b: d[2], a: d[3] / 255 };
    } catch { return { r: 0, g: 0, b: 0, a: 1 }; }
  };
  const lin = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
  const lum = (c) => 0.2126 * lin(c.r) + 0.7152 * lin(c.g) + 0.0722 * lin(c.b);
  const over = (fg, bg) => ({
    r: fg.r * fg.a + bg.r * (1 - fg.a), g: fg.g * fg.a + bg.g * (1 - fg.a),
    b: fg.b * fg.a + bg.b * (1 - fg.a), a: 1 });
  const effBg = (el) => {
    let node = el, acc = null;
    while (node) {
      const bg = parseRGB(getComputedStyle(node).backgroundColor);
      if (bg && bg.a > 0) { acc = acc ? over(acc, bg) : bg; if (acc.a >= 0.999) return acc; }
      node = node.parentElement;
    }
    return acc || { r: 255, g: 255, b: 255, a: 1 };
  };
  const ratio = (fg, bg) => {
    const a = lum(fg), b = lum(bg);
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
  };
  const isLarge = (cs) => {
    const px = parseFloat(cs.fontSize), w = parseInt(cs.fontWeight) || 400;
    return px >= 18.66 || (px >= 14 && w >= 700);
  };
  const sel = [
    ['T1 标题', '[data-testid="t1-trunk"] > div:nth-child(1)'],
    ['T1 说明句', '[data-testid="t1-trunk"] > div:nth-child(2)'],
    ['T1 网站名', '[data-testid="t1-badge"] > span:nth-child(1)'],
    ['T1 引用次数', '[data-testid="t1-badge"] > span:nth-child(2)'],
    ['T2 标题', '[data-testid="t2-combo"] > div:nth-child(1)'],
    ['T2 依据句', '[data-testid="t2-combo"] > div:nth-child(2)'],
    ['T2 建议值', '[data-testid="t2-suggest"] > span:nth-child(2)'],
    ['T2 已选值', '[data-testid="t2-current"] > span:nth-child(2)'],
    ['T2 替代提示', '[data-testid="t2-combo"] .text-amber-700'],
  ];
  const contrast = [];
  for (const [name, q] of sel) {
    const el = document.querySelector(q);
    if (!el) { contrast.push({ name, found: false }); continue; }
    const cs = getComputedStyle(el), fg = parseRGB(cs.color), bg = effBg(el);
    const large = isLarge(cs), r = ratio(over(fg, bg), bg);
    contrast.push({ name, found: true, text: (el.textContent || '').trim().slice(0, 30),
      color: cs.color, background: `rgb(${Math.round(bg.r)}, ${Math.round(bg.g)}, ${Math.round(bg.b)})`,
      fontSizePx: parseFloat(cs.fontSize), fontWeight: cs.fontWeight, largeText: large,
      ratio: Math.round(r * 100) / 100, required: large ? 3.0 : 4.5, pass: r >= (large ? 3.0 : 4.5) });
  }
  const fontSizes = [];
  document.querySelectorAll('[data-testid="t1-trunk"] *, [data-testid="t2-combo"] *').forEach(el => {
    const own = Array.from(el.childNodes).some(n => n.nodeType === 3 && n.textContent.trim());
    if (!own) return;
    fontSizes.push({ px: parseFloat(getComputedStyle(el).fontSize),
                     text: (el.textContent || '').trim().slice(0, 24) });
  });
  const clickables = [];
  document.querySelectorAll('button, [role="checkbox"], a[href], input, select, .cursor-pointer').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;
    if (r.bottom < 0 || r.top > window.innerHeight) return;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none') return;
    clickables.push({ tag: el.tagName.toLowerCase(),
      label: (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 24),
      w: Math.round(r.width * 10) / 10, h: Math.round(r.height * 10) / 10,
      x: Math.round(r.x), y: Math.round(r.y), ok: r.width >= 44 && r.height >= 44 });
  });
  let minGap = null, minGapPair = null;
  const sorted = clickables.slice().sort((a, b) => a.y - b.y || a.x - b.x);
  for (let i = 0; i < sorted.length - 1; i++) {
    const a = sorted[i], b = sorted[i + 1];
    if (Math.abs(a.y - b.y) > 8) continue;
    const gap = b.x - (a.x + a.w);
    if (gap < 0) continue;
    if (minGap === null || gap < minGap) { minGap = gap; minGapPair = [a.label, b.label]; }
  }
  const interactiveInNewBlocks = document.querySelectorAll(
    '[data-testid="t1-trunk"] button, [data-testid="t1-trunk"] a, [data-testid="t1-trunk"] .cursor-pointer,' +
    '[data-testid="t2-combo"] button, [data-testid="t2-combo"] a, [data-testid="t2-combo"] .cursor-pointer').length;
  const bar = document.querySelector('.sticky.bottom-0');
  let occluded = null;
  if (bar) {
    const br = bar.getBoundingClientRect();
    occluded = { barTop: Math.round(br.top), barHeight: Math.round(br.height),
                 viewportH: window.innerHeight, gapBelowBar: Math.round(window.innerHeight - br.bottom) };
  }
  return { contrast, fontSizes, clickables, minGap, minGapPair, interactiveInNewBlocks, occluded,
    horizontalOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
    scrollWidth: document.documentElement.scrollWidth, clientWidth: document.documentElement.clientWidth,
    t2Current: (document.querySelector('[data-testid="t2-current"]')?.textContent || '').trim(),
    t2Suggest: (document.querySelector('[data-testid="t2-suggest"]')?.textContent || '').trim(),
    t1Present: !!document.querySelector('[data-testid="t1-trunk"]'),
    t2Present: !!document.querySelector('[data-testid="t2-combo"]'),
    t1Text: (document.querySelector('[data-testid="t1-trunk"]')?.innerText || '').trim(),
    t2Text: (document.querySelector('[data-testid="t2-combo"]')?.innerText || '').trim() };
}
"""


#: 灰度关闭时后端返回的样子：两块的数据都不下发(见 services/media_balance_gate.py)
RECOMMEND_GRAY_OFF = {
    **{k: v for k, v in RECOMMEND.items()
       if k not in ("ai_citation_trunk", "combination_plan")},
    "media_balance_enabled": False,
    "ai_citation_trunk": None,
    "combination_plan": None,
}

GRAY_OFF = False


def _json(body):
    return {"status": 200, "content_type": "application/json",
            "body": json.dumps(body, ensure_ascii=False)}


def _stub(route, request):
    u = request.url.split("?")[0]
    if "/api/auth/me" in u:
        return route.fulfill(**_json({"success": True, "user": AUTH_ME}))
    if "/api/writing/projects" in u:
        return route.fulfill(**_json(PROJECTS))
    if "/api/placement/articles/" in u:
        return route.fulfill(**_json(ARTICLES))
    if "/api/publish/media/recommend-v2" in u:
        return route.fulfill(**_json(RECOMMEND_GRAY_OFF if GRAY_OFF else RECOMMEND))
    if "/api/publish/media/filters" in u or "/api/meijiehezi/media/filters" in u:
        return route.fulfill(**_json({"success": True, "filters": {}, "categories": [], "platforms": []}))
    if "/api/publish/media" in u:
        return route.fulfill(**_json({"success": True, "media": MEDIA, "total": len(MEDIA),
                                      "page": 1, "limit": 50, "pages": 1}))
    if "/api/meijiehezi/media" in u:
        # 现役媒体表(代发 tab)走这个端点，不是 /api/publish/media
        return route.fulfill(**_json({"success": True, "status": "success", "media": MEDIA,
                                      "total": len(MEDIA), "page": 1, "pages": 1}))
    if "/api/meijiehezi/markup" in u:
        return route.fulfill(**_json({"success": True, "markup_ratio": 1.0}))
    if "/api/" in u:
        return route.fulfill(**_json({"success": True, "status": "success", "data": [],
                                      "items": [], "articles": [], "media": [], "total": 0}))
    return route.continue_()


def run(base_url: str) -> dict:
    from playwright.sync_api import sync_playwright

    os.makedirs(OUT_DIR, exist_ok=True)
    report: dict = {"base_url": base_url, "viewports": {}}

    with sync_playwright() as pw:
        exe = os.environ.get("PLAYWRIGHT_CHROMIUM") or os.path.expandvars(
            r"%LOCALAPPDATA%\ms-playwright\chromium-1208\chrome-win64\chrome.exe")
        browser = pw.chromium.launch(**({"executable_path": exe} if os.path.exists(exe) else {}))

        for vp_name, width, height in (("375px", 375, 812), ("1440px", 1440, 900)):
            vp: dict = {"themes": {}}
            for theme in ("dark", "light"):
                ctx = browser.new_context(viewport={"width": width, "height": height},
                                          device_scale_factor=2)
                ctx.route("**/*", _stub)
                ctx.add_init_script(
                    "localStorage.setItem('omnirank_token','qa-acceptance-token');"
                    f"localStorage.setItem('omnirank-theme','{theme}');"
                    "localStorage.removeItem('omnirank_sandbox_active');"
                )
                page = ctx.new_page()
                tag = f"{vp_name}-{theme}"
                log: list = []

                def step(desc, fn):
                    try:
                        fn()
                        log.append({"step": desc, "ok": True})
                        return True
                    except Exception as exc:
                        log.append({"step": desc, "ok": False, "err": str(exc)[:130]})
                        return False

                page.goto(f"{base_url}/publish", wait_until="domcontentloaded")
                page.wait_for_timeout(3500)
                page.screenshot(path=os.path.join(OUT_DIR, f"01_{tag}_landing.png"))

                # 关掉沙盒后首访会弹「欢迎使用 OmniRank 服务方工作台」，先选「我已经用过」进真实工作台
                step("⓪ 跳过新手弹窗",
                     lambda: page.get_by_text("我已经用过", exact=False).first.click(timeout=4000))
                page.wait_for_timeout(1500)
                step("① 切到代发",
                     lambda: page.get_by_role("button", name="代发").first.click(timeout=4000))
                page.wait_for_timeout(800)
                step("② 选项目", lambda: page.locator("select").first.select_option(index=1))
                page.wait_for_timeout(1500)
                # data-article-id 挂在外层容器上、不是可点元素，直接点标题文本更稳
                step("③ 选文章",
                     lambda: page.get_by_text("深圳装修公司哪家靠谱 · 2026 选型指南", exact=False).first.click(timeout=6000))
                page.wait_for_timeout(3000)
                page.screenshot(path=os.path.join(OUT_DIR, f"02_{tag}_article_picked.png"))

                before = page.evaluate(MEASURE_JS)

                def pick_media(name: str):
                    """桌面媒体表是 div 行(role=checkbox)、移动是卡片，两种布局都要勾得上。

                    定位方式：找到**含该媒体名且体积最小**的那个容器，再点它里面的
                    role=checkbox；派发的是真实 MouseEvent，会正常冒泡到 React 事件委托。
                    """
                    clicked = page.evaluate("""(name) => {
                      const nodes = Array.from(document.querySelectorAll('div, li, tr'))
                        .filter(el => (el.textContent || '').includes(name));
                      if (!nodes.length) return 'no-node';
                      nodes.sort((a, b) => (a.textContent||'').length - (b.textContent||'').length);
                      for (const n of nodes) {
                        let host = n;
                        for (let i = 0; i < 6 && host; i++) {
                          const box = host.querySelector('[role="checkbox"], input[type="checkbox"]');
                          if (box) { box.click(); return 'ok'; }
                          host = host.parentElement;
                        }
                      }
                      nodes[0].click();
                      return 'fallback-text-click';
                    }""", name)
                    if clicked == 'no-node':
                        raise RuntimeError(f"media row not found: {name}")

                ok1 = step("④ 勾搜狐(大平台)", lambda: pick_media("搜狐网新闻"))
                page.wait_for_timeout(900)
                after1 = page.evaluate(MEASURE_JS)
                ok2 = step("⑤ 勾汽车之家(行业网站)", lambda: pick_media("汽车之家"))
                page.wait_for_timeout(900)
                after2 = page.evaluate(MEASURE_JS)
                page.screenshot(path=os.path.join(OUT_DIR, f"03_{tag}_after_select.png"))

                step("⑥ 加入购物车",
                     lambda: page.get_by_role("button", name="加入购物车").first.click(timeout=5000))
                page.wait_for_timeout(1200)
                page.screenshot(path=os.path.join(OUT_DIR, f"04_{tag}_cart_added.png"))

                page.mouse.wheel(0, 6000)
                page.wait_for_timeout(700)
                bottom = page.evaluate(MEASURE_JS)
                page.screenshot(path=os.path.join(OUT_DIR, f"05_{tag}_scrolled_bottom.png"))

                step("⑦ 展开购物车",
                     lambda: page.get_by_text("购物车", exact=False).first.click(timeout=4000))
                page.wait_for_timeout(900)
                page.screenshot(path=os.path.join(OUT_DIR, f"06_{tag}_cart_open.png"))

                vp["themes"][theme] = {
                    "steps": log,
                    "reactivity": {
                        "t2_before": before.get("t2Current"),
                        "t2_after_big_platform": after1.get("t2Current"),
                        "t2_after_industry_site": after2.get("t2Current"),
                        "clicked_big_ok": ok1, "clicked_industry_ok": ok2,
                        "reactive": (before.get("t2Current") != after1.get("t2Current")
                                     and after1.get("t2Current") != after2.get("t2Current")),
                    },
                    "measure_after_select": after2,
                    "measure_at_bottom": {k: bottom.get(k) for k in
                                          ("occluded", "horizontalOverflow", "scrollWidth", "clientWidth")},
                }
                ctx.close()
            report["viewports"][vp_name] = vp
        browser.close()

    with open(os.path.join(OUT_DIR, "measurements.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:5230")
    ap.add_argument("--gray-off", action="store_true",
                    help="模拟灰度关闭(后端不下发两块数据),验证前端确实不渲染")
    args = ap.parse_args()
    global GRAY_OFF
    GRAY_OFF = args.gray_off
    report = run(args.base)
    for vp, data in report["viewports"].items():
        for theme, td in data["themes"].items():
            m = td["measure_after_select"]
            cf = [c for c in m["contrast"] if c.get("found") and not c["pass"]]
            small = [f for f in m["fontSizes"] if f["px"] < 12]
            tiny = [c for c in m["clickables"] if not c["ok"]]
            print(f"[{vp}/{theme}] T1={m['t1Present']} T2={m['t2Present']} "
                  f"reactive={td['reactivity']['reactive']} contrast_fail={len(cf)} "
                  f"font<12={len(small)} touch<44={len(tiny)} minGap={m['minGap']} "
                  f"hscroll={m['horizontalOverflow']} clickableInNewBlocks={m['interactiveInNewBlocks']}")
    print("raw ->", os.path.join(OUT_DIR, "measurements.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
