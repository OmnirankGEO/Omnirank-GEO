"""pdf_reader 多线程探针(WO_319 · Review 10-02 NO-GO 的复现与回归)。由 test_pdf_reader.py 在子进程里起:
PDFium 并发出事时可能是原生崩溃,放子进程里只带走子进程。

用法:python threads_probe.py <线程数> <每线程轮数> [nolock]
  nolock = 测试内打补丁把 pdf_reader 的锁换成空锁(牙证:去锁必须读到错误或不一致)
每轮用对外的整份文档级函数做三件事:取文字、取文字 + 内嵌图(read)、整页渲染;结果与单线程先算好的逐项比对。
最后一行打印 JSON:{"errors": 出错次数, "wrong": 结果不一致次数, "sample": 前 3 条错误}。
"""
import contextlib
import json
import os
import sys
import threading
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, ROOT)
from services import pdf_reader  # noqa: E402

n_threads, iters = int(sys.argv[1]), int(sys.argv[2])
if len(sys.argv) > 3 and sys.argv[3] == "nolock":
    pdf_reader._LOCK = contextlib.nullcontext()

TEXT_PDF = open(os.path.join(HERE, "text_image.pdf"), "rb").read()
SCAN_PDF = open(os.path.join(HERE, "scan.pdf"), "rb").read()
SCALE = 1.0  # 渲染倍率只影响耗时,不影响是否并发进 PDFium


def snapshot(data):
    c = pdf_reader.read(data, min_image_bytes=0)
    return (pdf_reader.page_texts(data), [(i.page_index, i.ext, len(i.data)) for i in c.images],
            [(i, len(png) > 0) for i, png in pdf_reader.render_pages_png(data, scale=SCALE)])


want = {k: snapshot(v) for k, v in (("text", TEXT_PDF), ("scan", SCAN_PDF))}
errors, wrong = [], [0]


def work(k):
    for i in range(iters):
        name, data = ("scan", SCAN_PDF) if (i + k) % 2 else ("text", TEXT_PDF)
        try:
            if snapshot(data) != want[name]:
                wrong[0] += 1
        except Exception:
            errors.append(traceback.format_exc(limit=1).strip().splitlines()[-1][:200])


ts = [threading.Thread(target=work, args=(k,)) for k in range(n_threads)]
for t in ts:
    t.start()
for t in ts:
    t.join()
print(json.dumps({"errors": len(errors), "wrong": wrong[0], "sample": errors[:3]}, ensure_ascii=False))
