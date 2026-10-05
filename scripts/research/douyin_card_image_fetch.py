"""§6b · 下载被采纳图文帖的卡面图 + 拼接联系表(contact sheet)供视觉分类

产物**只落本地**(默认 .tmp_ro/cards/,已在 git exclude),不入仓 —— 生产盘 71% 的
同一条约束在本机同样适用:研究素材不进版本库。

为什么要拼联系表:246 张图逐张看不现实,拼成带序号的网格后,一屏能覆盖 20-25 张,
既能做形态分类,又能快速定位要放大细看的那几张。

用法:
    python scripts/research/douyin_card_image_fetch.py \
        --probe .tmp_ro/probe_result.json --out .tmp_ro/cards
    python scripts/research/douyin_card_image_fetch.py --out .tmp_ro/cards --sheets-only
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


async def _fetch_one(client: httpx.AsyncClient, sem: asyncio.Semaphore,
                     url: str, dest: Path) -> Dict[str, Any]:
    if dest.exists() and dest.stat().st_size > 0:
        return {"path": str(dest), "ok": True, "cached": True}
    async with sem:
        for attempt in range(3):
            try:
                r = await client.get(url, headers={"User-Agent": _UA,
                                                   "Referer": "https://www.douyin.com/"},
                                     timeout=45.0, follow_redirects=True)
                if r.status_code == 200 and r.content:
                    dest.write_bytes(r.content)
                    return {"path": str(dest), "ok": True, "bytes": len(r.content)}
                if r.status_code in (429, 500, 502, 503):
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                return {"path": str(dest), "ok": False, "error": f"HTTP {r.status_code}"}
            except Exception as exc:  # noqa: BLE001 - 单张失败不中断全批
                if attempt == 2:
                    return {"path": str(dest), "ok": False,
                            "error": f"{type(exc).__name__}: {str(exc)[:100]}"}
                await asyncio.sleep(1.5 * (attempt + 1))
    return {"path": str(dest), "ok": False, "error": "exhausted"}


async def download(probe: Path, out: Path, concurrency: int) -> List[Dict[str, Any]]:
    rows = json.loads(probe.read_text(encoding="utf-8"))
    posts = [r for r in rows if r.get("kind") == "image_post"]
    out.mkdir(parents=True, exist_ok=True)

    jobs, manifest = [], []
    for pi, post in enumerate(posts):
        for ci, url in enumerate(post.get("image_urls") or []):
            if not url:
                continue
            dest = out / f"p{pi:02d}_c{ci:02d}.jpg"
            jobs.append((url, dest))
            manifest.append({
                "post_idx": pi, "card_idx": ci, "file": dest.name,
                "aweme_id": post.get("aweme_id"),
                "industry_key": post.get("industry_key"),
                "image_count": post.get("image_count"),
                "title": (post.get("title_from_signal") or "")[:120],
                "desc": (post.get("desc") or "")[:200],
            })

    print(f"[fetch] 图文帖 {len(posts)} 条 · 待下载卡面 {len(jobs)} 张")
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *(_fetch_one(client, sem, u, d) for u, d in jobs))

    ok = sum(1 for r in results if r.get("ok"))
    for m, r in zip(manifest, results):
        m["ok"] = bool(r.get("ok"))
        m["error"] = r.get("error", "")
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[fetch] 成功 {ok}/{len(jobs)}")
    for r in results:
        if not r.get("ok"):
            print("    -", Path(r["path"]).name, r.get("error"))
    return manifest


def build_sheets(out: Path, cols: int = 5, rows: int = 5, cell: int = 300) -> int:
    """把卡面拼成带序号的联系表。序号 = pXX_cYY,和 manifest 对得上。"""
    from PIL import Image, ImageDraw

    # 🔴 抖音卡面实际下发的是 **HEIC**(content-type: image/heic),不是 jpg ——
    #    文件名后缀骗人。PIL 原生开不了,必须注册 pillow-heif 解码器。
    #    (试过把 URL 里的 .heic 换成 .jpeg/.webp → 403:x-signature 覆盖了扩展名,
    #     格式是签死的,只能本地解码。)
    #    pillow-heif 只装在本机研究环境,**不进 requirements.txt** —— 线上不需要它。
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except ImportError:
        print("    ! 未装 pillow-heif,HEIC 卡面将无法拼图:pip install pillow-heif")

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    sheet_dir = out / "sheets"
    sheet_dir.mkdir(exist_ok=True)
    label_h = 22

    def _open(f: Path):
        """能开就返回 RGB 图,开不了返回 None。

        🔴 27/246 张是 VVC(H.266)编码的 HEIF(magic `ftypvvic`),libheif 不支持 →
           这部分**读不出来**,不是下载失败。整贴丢失 8 条,其余 48 条完好。
        """
        try:
            with Image.open(f) as im:
                return im.convert("RGB")
        except Exception:  # noqa: BLE001
            return None

    # ── A. 按帖分组:每行一个帖子,卡片按原序 → 看组内叙事(首图/内容卡/尾卡)──
    by_post: Dict[int, List[dict]] = {}
    for m in manifest:
        if m.get("ok"):
            by_post.setdefault(m["post_idx"], []).append(m)
    for v in by_post.values():
        v.sort(key=lambda x: x["card_idx"])

    usable = {p: ms for p, ms in by_post.items()
              if any(_open(out / m["file"]) for m in ms[:1])}

    sheets = 0
    posts_sorted = sorted(usable.items())
    PER_SHEET = 6          # 每张联系表放 6 个帖子(6 行)
    MAXCOL = 9             # 最多展示前 9 张卡
    for s in range(0, len(posts_sorted), PER_SHEET):
        chunk = posts_sorted[s:s + PER_SHEET]
        sheet = Image.new("RGB", (MAXCOL * cell, len(chunk) * (cell + label_h)), "white")
        draw = ImageDraw.Draw(sheet)
        for r, (pidx, ms) in enumerate(chunk):
            y = r * (cell + label_h)
            draw.text((4, y + 4), f"p{pidx:02d} · {ms[0].get('industry_key','')} · "
                                  f"{len(ms)}张 · {ms[0].get('title','')[:38]}", fill="black")
            for c, m in enumerate(ms[:MAXCOL]):
                im = _open(out / m["file"])
                if im is None:
                    continue
                im.thumbnail((cell, cell))
                x = c * cell
                sheet.paste(im, (x + (cell - im.width) // 2,
                                 y + label_h + (cell - im.height) // 2))
                draw.text((x + 4, y + label_h + 2), f"c{m['card_idx']}", fill="red")
        p = sheet_dir / f"bypost_{sheets:02d}.jpg"
        sheet.save(p, quality=80)
        sheets += 1

    # ── B. 只看首图:一屏看全部首图 → 看首图钩子形态 ──
    firsts = [ms[0] for _p, ms in posts_sorted]
    fcols = 8
    frows = (len(firsts) + fcols - 1) // fcols
    fsheet = Image.new("RGB", (fcols * cell, frows * (cell + label_h)), "white")
    fdraw = ImageDraw.Draw(fsheet)
    for i, m in enumerate(firsts):
        im = _open(out / m["file"])
        if im is None:
            continue
        im.thumbnail((cell, cell))
        x = (i % fcols) * cell
        y = (i // fcols) * (cell + label_h)
        fsheet.paste(im, (x + (cell - im.width) // 2, y + label_h + (cell - im.height) // 2))
        fdraw.text((x + 4, y + 4), f"p{m['post_idx']:02d}", fill="black")
    fsheet.save(sheet_dir / "firstcards.jpg", quality=82)

    print(f"[sheets] 按帖联系表 {sheets} 张 + 首图总览 1 张 → {sheet_dir}")
    print(f"[sheets] 可用帖子 {len(posts_sorted)} 条")
    return sheets


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--probe", default=".tmp_ro/probe_result.json")
    p.add_argument("--out", default=".tmp_ro/cards")
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--sheets-only", action="store_true")
    args = p.parse_args()

    out = Path(args.out)
    if not args.sheets_only:
        asyncio.run(download(Path(args.probe), out, args.concurrency))
    build_sheets(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
