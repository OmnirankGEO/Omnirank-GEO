"""简单算术图形验证码（SVG，无需Pillow）"""
import random
import uuid
import time
import base64


# 内存降级缓存（仅在 Redis 不可用时使用 — WORKERS=4 多 worker 会导致状态丢失）
_captcha_store: dict = {}  # captcha_id -> {"answer": "8", "expires": timestamp}

REDIS_KEY_PREFIX = "captcha:"
CAPTCHA_TTL = 300  # 5 分钟


def generate_captcha() -> dict:
    """生成算术验证码，返回 {"captcha_id": "uuid", "image": "data:image/svg+xml;base64,..."}"""
    a = random.randint(1, 20)
    b = random.randint(1, 20)
    op = random.choice(['+', '-'])

    if op == '-' and a < b:
        a, b = b, a

    answer = str(a + b) if op == '+' else str(a - b)
    question = f"{a} {op} {b} = ?"

    svg = _generate_svg(question)
    encoded = base64.b64encode(svg.encode()).decode()

    captcha_id = str(uuid.uuid4())
    # 优先 Redis（多 worker 共享），降级到内存
    try:
        from cache.redis_client import redis_set
        if not redis_set(f"{REDIS_KEY_PREFIX}{captcha_id}", answer, ex=CAPTCHA_TTL):
            _captcha_store[captcha_id] = {"answer": answer, "expires": time.time() + CAPTCHA_TTL}
    except Exception:
        _captcha_store[captcha_id] = {"answer": answer, "expires": time.time() + CAPTCHA_TTL}

    return {
        "captcha_id": captcha_id,
        "image": f"data:image/svg+xml;base64,{encoded}",
    }


def verify_captcha(captcha_id: str, answer: str) -> bool:
    """校验图形验证码（一次性，用完即删）"""
    if not captcha_id or not answer:
        return False

    expected = None
    # 优先 Redis
    try:
        from cache.redis_client import redis_get, redis_delete
        expected = redis_get(f"{REDIS_KEY_PREFIX}{captcha_id}")
        if expected is not None:
            redis_delete(f"{REDIS_KEY_PREFIX}{captcha_id}")
    except Exception:
        expected = None

    # 降级查内存
    if expected is None:
        record = _captcha_store.pop(captcha_id, None)
        if not record or time.time() > record["expires"]:
            return False
        expected = record["answer"]

    return expected == answer.strip()


def _generate_svg(text: str) -> str:
    """生成带干扰线的 SVG 验证码图片"""
    width, height = 160, 50

    lines = ""
    for _ in range(4):
        x1, y1 = random.randint(0, width), random.randint(0, height)
        x2, y2 = random.randint(0, width), random.randint(0, height)
        color = f"rgb({random.randint(100,200)},{random.randint(100,200)},{random.randint(100,200)})"
        lines += f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="1"/>'

    dots = ""
    for _ in range(20):
        cx, cy = random.randint(0, width), random.randint(0, height)
        color = f"rgb({random.randint(100,200)},{random.randint(100,200)},{random.randint(100,200)})"
        dots += f'<circle cx="{cx}" cy="{cy}" r="1.5" fill="{color}"/>'

    chars = ""
    for i, ch in enumerate(text):
        x = 15 + i * 18
        y = 30 + random.randint(-5, 5)
        rotate = random.randint(-15, 15)
        color = f"rgb({random.randint(30,80)},{random.randint(30,80)},{random.randint(30,80)})"
        chars += f'<text x="{x}" y="{y}" font-size="22" font-family="Arial" fill="{color}" transform="rotate({rotate},{x},{y})">{ch}</text>'

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">
        <rect width="100%" height="100%" fill="#f0f0f0"/>
        {lines}{dots}{chars}
    </svg>'''


def cleanup_expired():
    """清理过期验证码（定时调用）"""
    now = time.time()
    expired = [k for k, v in _captcha_store.items() if now > v["expires"]]
    for k in expired:
        del _captcha_store[k]
