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
            e = min(n, j + len(x)); L[j:e] += x[:e - j]; R[j:e] += x[:e - j]
        # 拨弦琶音:8 分音符,左右轻微交替
        arp = [ch[0] + 12, ch[1] + 12, ch[2] + 12, ch[3] + 12, ch[2] + 12, ch[1] + 12, ch[3] + 12, ch[2] + 24]
        for k in range(8):
            j = i0 + int(k * beat / 2 * SR); tt = np.arange(int(0.6 * SR)) / SR
            f = _hz(arp[k])
            x = (np.sin(2 * np.pi * f * tt) + 0.3 * np.sin(4 * np.pi * f * tt)) * np.exp(-tt * 7) * 0.06
            pan = 0.35 if k % 2 else -0.35
            e = min(n, j + len(x))
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
            e = min(n, j + len(x)); L[j:e] += x[:e - j]; R[j:e] += x[:e - j]
        for k in range(16):
            j = i0 + int(k * beat / 4 * SR); tt = np.arange(int(0.05 * SR)) / SR
            x = rng.uniform(-1, 1, len(tt)) * np.exp(-tt * 90) * (0.035 if k % 2 else 0.05)
            x = x - _lp(x, 5000)
            e = min(n, j + len(x)); L[j:e] += x[:e - j] * 0.8; R[j:e] += x[:e - j] * 1.2
    fi, fo = int(1.5 * SR), int(4 * SR)
    g = np.ones(n); g[:fi] = np.linspace(0, 1, fi); g[-fo:] = np.linspace(1, 0, fo)
    return np.stack([L * g, R * g], axis=1)
