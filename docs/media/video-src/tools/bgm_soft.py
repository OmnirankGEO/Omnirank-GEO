"""MG 讲解版背景音乐:100 BPM,轻快的 pad + 拨弦琶音 + 轻鼓,全部代码合成。"""
import numpy as np
from scipy.signal import lfilter

SR = 48000


def _lp(x, fc):
    a = np.exp(-2 * np.pi * fc / SR)
    return lfilter([1 - a], [1, -a], x)


def _hz(m):
    return 440.0 * 2 ** ((m - 69) / 12)


def make(dur, seed=5):
    rng = np.random.default_rng(seed)
    n = int(dur * SR)
    L = np.zeros(n); R = np.zeros(n)
    bpm = 100; beat = 60 / bpm; bar = beat * 4
    prog = [[53, 57, 60, 64], [55, 59, 62, 65], [52, 55, 59, 62], [57, 60, 64, 67]]  # Fmaj7 G7 Em7 Am7
    t_bar = np.arange(int(bar * SR)) / SR
    nbars = int(np.ceil(dur / bar))
    for b in range(nbars):
        ch = prog[b % 4]
        i0 = int(b * bar * SR)
        seg = slice(i0, min(n, i0 + len(t_bar)))
        m = seg.stop - seg.start
        # pad:两层略微失谐的锯齿波,低通后慢起慢收
        pad = np.zeros(len(t_bar))
        for note in ch:
            f = _hz(note)
            for det in (-0.12, 0.12):
                ph = (t_bar * f * 2 ** (det / 12)) % 1.0
                pad += (2 * ph - 1)
        pad = _lp(pad, 1200) * 0.022
        envp = np.minimum(1, t_bar / 0.4) * np.minimum(1, (bar - t_bar) / 0.4)
        L[seg] += (pad * envp)[:m]; R[seg] += (pad * envp)[:m]
        # bass:每拍一个根音,正弦 + 少量二次谐波
        for k in range(4):
            j = i0 + int(k * beat * SR); tt = np.arange(int(beat * 0.9 * SR)) / SR
            f = _hz(ch[0] - 24)
            x = (np.sin(2 * np.pi * f * tt) + 0.25 * np.sin(4 * np.pi * f * tt)) * np.exp(-tt * 3) * 0.16
            e = max(j, min(n, j + len(x))); L[j:e] += x[:e - j]; R[j:e] += x[:e - j]
        # 拨弦琶音:8 分音符,左右轻微交替
        arp = [ch[0] + 12, ch[1] + 12, ch[2] + 12, ch[3] + 12, ch[2] + 12, ch[1] + 12, ch[3] + 12, ch[2] + 24]
        for k in range(8):
            j = i0 + int(k * beat / 2 * SR); tt = np.arange(int(0.6 * SR)) / SR
            f = _hz(arp[k])
            x = (np.sin(2 * np.pi * f * tt) + 0.3 * np.sin(4 * np.pi * f * tt)) * np.exp(-tt * 7) * 0.06
            pan = 0.35 if k % 2 else -0.35
            e = max(j, min(n, j + len(x)))
            L[j:e] += x[:e - j] * (1 - pan); R[j:e] += x[:e - j] * (1 + pan)
        # 鼓:kick 1/3 拍,clap 2/4 拍,shaker 16 分
        for k in range(4):
            j = i0 + int(k * beat * SR)
            if k in (0, 2):
                tt = np.arange(int(0.25 * SR)) / SR
                f = 50 + 90 * np.exp(-tt * 30)
                x = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-tt * 12) * 0.32
            else:
                tt = np.arange(int(0.18 * SR)) / SR
                x = _lp(rng.uniform(-1, 1, len(tt)), 3500) * np.exp(-tt * 25) * 0.16
            e = max(j, min(n, j + len(x))); L[j:e] += x[:e - j]; R[j:e] += x[:e - j]
        for k in range(16):
            j = i0 + int(k * beat / 4 * SR); tt = np.arange(int(0.05 * SR)) / SR
            x = rng.uniform(-1, 1, len(tt)) * np.exp(-tt * 90) * (0.035 if k % 2 else 0.05)
            x = x - _lp(x, 5000)
            e = max(j, min(n, j + len(x))); L[j:e] += x[:e - j] * 0.8; R[j:e] += x[:e - j] * 1.2
    fi, fo = int(1.5 * SR), int(4 * SR)
    g = np.ones(n); g[:fi] = np.linspace(0, 1, fi); g[-fo:] = np.linspace(1, 0, fo)
    return np.stack([L * g, R * g], axis=1)


def soft_sfx(events, n):
    """讲解版的轻音效:whoosh 转场、pop 弹出、ding 强调、tick 打字、stamp 盖章、rise 上扬。"""
    rng = np.random.default_rng(11)
    out = np.zeros(n)

    def tone(f0, f1, d, decay, amp, harm=0.0):
        t = np.arange(int(d * SR)) / SR
        f = np.geomspace(f0, f1, len(t))
        ph = 2 * np.pi * np.cumsum(f) / SR
        return (np.sin(ph) + harm * np.sin(2 * ph)) * np.exp(-t * decay) * amp

    def noise(d, fc_from, fc_to, amp, attack=0.04):
        t = np.arange(int(d * SR)) / SR
        x = rng.uniform(-1, 1, len(t))
        x = _lp(x, fc_to) - _lp(x, fc_from)
        env = np.minimum(1, t / attack) * np.exp(-t * 4)
        return x * env * amp

    lib = {
        'whoosh': noise(0.45, 300, 4000, 0.35, attack=0.12),
        'pop': tone(500, 900, 0.09, 40, 0.30),
        'ding': tone(1320, 1320, 0.6, 7, 0.22, harm=0.3) + np.pad(tone(1980, 1980, 0.5, 9, 0.08), (0, int(0.1 * SR))),
        'tick': tone(2200, 2200, 0.03, 120, 0.12),
        'stamp': tone(120, 60, 0.25, 18, 0.55) + np.pad(noise(0.12, 200, 2500, 0.4, attack=0.002), (0, int(0.25 * SR) - int(0.12 * SR))),
        'rise': tone(300, 900, 0.5, 3, 0.12),
        'boom': tone(90, 38, 0.9, 5, 0.6) + np.pad(noise(0.35, 80, 1800, 0.5, attack=0.003), (0, int(0.9 * SR) - int(0.35 * SR))),
        'sparkle': sum(np.pad(tone(f, f, 0.35, 14, 0.07), (k * int(0.045 * SR), (5 - k) * int(0.045 * SR)))
                       for k, f in enumerate((2093, 2637, 3136, 3951, 4186, 5274))),
    }
    for ev in events:
        s = lib.get(ev['name'])
        if s is None:
            continue
        i = int(ev['t'] * SR)
        e = max(i, min(n, i + len(s)))
        out[i:e] += s[:e - i]
    return out


def make_tech(dur, seed=7):
    """发布会风 BGM:118 BPM 四拍底鼓、侧链泵动的小调和弦垫、反拍镲、低音八分音符。"""
    rng = np.random.default_rng(seed)
    n = int(dur * SR)
    L = np.zeros(n); R = np.zeros(n)
    bpm = 118; beat = 60 / bpm; bar = beat * 4
    prog = [[45, 52, 57, 60, 64], [41, 48, 53, 57, 60], [43, 50, 55, 59, 62], [40, 47, 52, 55, 59]]  # Am F G Em
    nbars = int(np.ceil(dur / bar)) + 1
    t = np.arange(n) / SR
    # 侧链:每拍开头压下去再弹起
    pump = 1 - 0.75 * np.exp(-((t % beat) / 0.09))
    for b in range(nbars):
        ch = prog[(b // 2) % 4] if b >= 2 else prog[0]
        i0 = int(b * bar * SR); i1 = min(n, int((b + 1) * bar * SR))
        if i0 >= n:
            break
        tt = np.arange(i1 - i0) / SR
        pad = np.zeros(len(tt))
        for note in ch[1:]:
            f = _hz(note + 12)
            for det in (-0.1, 0.0, 0.1):
                ph = (tt * f * 2 ** (det / 12)) % 1.0
                pad += 2 * ph - 1
        pad = _lp(pad, 1600 + 600 * (b % 2)) * 0.016
        L[i0:i1] += pad * pump[i0:i1]; R[i0:i1] += pad * pump[i0:i1]
        for k in range(8):   # 低音八分
            j = i0 + int(k * beat / 2 * SR); x_t = np.arange(int(beat / 2 * 0.85 * SR)) / SR
            f = _hz(ch[0] - 12)
            x = _lp(2 * ((x_t * f) % 1.0) - 1, 500) * np.exp(-x_t * 6) * (0.18 if k % 2 else 0.12)
            e = max(j, min(n, j + len(x))); L[j:e] += x[:e - j]; R[j:e] += x[:e - j]
        for k in range(4):
            j = i0 + int(k * beat * SR)
            x_t = np.arange(int(0.3 * SR)) / SR
            f = 45 + 110 * np.exp(-x_t * 35)
            x = np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-x_t * 9) * 0.42
            e = max(j, min(n, j + len(x))); L[j:e] += x[:e - j]; R[j:e] += x[:e - j]
            j2 = j + int(beat / 2 * SR); h_t = np.arange(int(0.08 * SR)) / SR
            h = rng.uniform(-1, 1, len(h_t)); h = (h - _lp(h, 7000)) * np.exp(-h_t * 50) * 0.09
            e = max(j2, min(n, j2 + len(h))); L[j2:e] += h[:e - j2] * 0.8; R[j2:e] += h[:e - j2] * 1.2
            if k in (1, 3):
                c_t = np.arange(int(0.2 * SR)) / SR
                c = _lp(rng.uniform(-1, 1, len(c_t)), 4000) * np.exp(-c_t * 22) * 0.12
                e = max(j, min(n, j + len(c))); L[j:e] += c[:e - j]; R[j:e] += c[:e - j]
        for k in range(16):   # 高音琶音点缀
            if (k * 7 + b) % 3:
                continue
            j = i0 + int(k * beat / 4 * SR); a_t = np.arange(int(0.25 * SR)) / SR
            f = _hz(ch[1 + k % 4] + 24)
            x = np.sin(2 * np.pi * f * a_t) * np.exp(-a_t * 14) * 0.035
            pan = 0.4 if k % 2 else -0.4
            e = max(j, min(n, j + len(x))); L[j:e] += x[:e - j] * (1 - pan); R[j:e] += x[:e - j] * (1 + pan)
    fi, fo = int(0.05 * SR), int(3 * SR)
    g = np.ones(n); g[:fi] = np.linspace(0, 1, fi); g[-fo:] = np.linspace(1, 0, fo)
    return np.stack([L * g, R * g], axis=1)
