"""重新生成本包的两份真 PDF 夹具(一次性手跑,不在测试里跑)。用法:CHROME=<chrome.exe> python make_fixtures.py

- text_image.pdf:两页,中文文字层;第 1 页一张约 20KB 的 JPEG(> 4KB 图标阈值)、一张 1KB 以下的小图标(应被阈值滤掉)
- scan.pdf:一页,只有一张图(把字画在图里)⇒ 没有文字层,只能整页渲染后识图
内容只有测试文字,不含任何真实客户资料。由 headless Chrome 打印,即真实浏览器产出的 PDF。
"""
import os
import random
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
CHROME = os.environ["CHROME"]


def _photo(path: Path) -> None:
    rnd = random.Random(20261001)
    img = Image.new("RGB", (360, 240))
    px = img.load()
    for x in range(360):
        for y in range(240):
            px[x, y] = ((x * 7 + rnd.randint(0, 60)) % 256, (y * 5 + rnd.randint(0, 60)) % 256, rnd.randint(80, 200))
    img.save(path, "JPEG", quality=70)


def _icon(path: Path) -> None:
    Image.new("RGB", (8, 8), (200, 30, 30)).save(path, "PNG")


def _scan(path: Path) -> None:
    img = Image.new("RGB", (800, 300), "white")
    d = ImageDraw.Draw(img)
    for i, y in enumerate(range(40, 260, 40)):
        d.rectangle([40, y, 40 + 600 - i * 60, y + 18], fill="black")
    img.save(path, "PNG")


def _print(html: str, out: Path, work: Path) -> None:
    src = work / (out.stem + ".html")
    src.write_text(html, encoding="utf-8")
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                    f"--print-to-pdf={out}", src.as_uri()], check=True, capture_output=True, timeout=60)


def main() -> None:
    with tempfile.TemporaryDirectory() as t:
        work = Path(t)
        _photo(work / "photo.jpg")
        _icon(work / "icon.png")
        _scan(work / "scan.png")
        _print("""<!doctype html><meta charset="utf-8"><body style="font-family:'Microsoft YaHei',sans-serif">
<h1>引路测试文档</h1><p>第一页:这是一份用于解析回归的测试文档,内容不含任何真实客户资料。</p>
<p><img src="photo.jpg" width="360" height="240"> <img src="icon.png" width="8" height="8"></p>
<p style="page-break-before:always">第二页:提取文字时页序必须保持,第二页的这句话应当排在第一页之后。</p></body>""",
               HERE / "text_image.pdf", work)
        _print('<!doctype html><meta charset="utf-8"><body style="margin:0"><img src="scan.png" width="700"></body>',
               HERE / "scan.pdf", work)


if __name__ == "__main__":
    main()
