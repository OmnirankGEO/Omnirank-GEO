"""把 srt 压缩成按时间段归并的紧凑文本,去掉时间戳噪声。"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRT = ROOT / "docs" / "视频" / "4月20日.srt"
OUT = ROOT / "scripts" / "srt_compact.txt"

# 每 BUCKET_SEC 秒归并成一段
BUCKET_SEC = 120


def parse_srt(text):
    blocks = re.split(r"\n\n+", text.strip())
    for b in blocks:
        lines = b.strip().split("\n")
        if len(lines) < 3:
            continue
        m = re.match(r"(\d+):(\d+):(\d+),(\d+)\s*-->\s*(\d+):(\d+):(\d+),(\d+)", lines[1])
        if not m:
            continue
        h1, m1, s1, _, h2, m2, s2, _ = map(int, m.groups())
        start = h1 * 3600 + m1 * 60 + s1
        end = h2 * 3600 + m2 * 60 + s2
        content = " ".join(lines[2:]).strip()
        yield start, end, content


def fmt_time(sec):
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def main():
    text = SRT.read_text(encoding="utf-8")
    entries = list(parse_srt(text))
    print(f"Parsed {len(entries)} subtitle entries")
    if not entries:
        print("No entries")
        return

    total_sec = entries[-1][1]
    print(f"Total duration: {fmt_time(total_sec)}")

    # 按 BUCKET_SEC 归并
    buckets = {}
    for start, end, content in entries:
        bucket_key = start // BUCKET_SEC * BUCKET_SEC
        buckets.setdefault(bucket_key, []).append(content)

    lines = []
    for key in sorted(buckets.keys()):
        t_start = fmt_time(key)
        t_end = fmt_time(min(key + BUCKET_SEC, total_sec))
        merged = " ".join(buckets[key])
        # 压缩重复(老板会重复说"然后" "就是" 等)
        merged = re.sub(r"\s+", " ", merged).strip()
        lines.append(f"[{t_start}-{t_end}] {merged}")

    OUT.write_text("\n\n".join(lines), encoding="utf-8")
    chars = sum(len(l) for l in lines)
    print(f"Compressed to {len(lines)} buckets, {chars} chars")
    print(f"Saved to {OUT}")


if __name__ == "__main__":
    main()
