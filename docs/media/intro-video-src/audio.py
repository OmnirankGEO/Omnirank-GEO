"""合成 8-bit 背景音乐 + 音效,按 out/meta.json 的时间点混音,输出 out/audio.wav。"""
import json, wave, os
import numpy as np

SR = 44100
HERE = os.path.dirname(os.path.abspath(__file__))
meta = json.load(open(os.path.join(HERE, 'out/meta.json')))
DUR = meta['dur'] + 1.0
N = int(DUR * SR)
rng = np.random.default_rng(3)

def t_(d): return np.arange(int(d * SR)) / SR
def hz(m): return 440.0 * 2 ** ((m - 69) / 12)
def pulse(f, d, duty=0.5):
    ph = (np.cumsum(np.full(int(d * SR), f) if np.isscalar(f) else f) / SR) % 1.0
    return np.where(ph < duty, 1.0, -1.0)
def tri(f, d):
    ph = (np.cumsum(np.full(int(d * SR), f)) / SR) % 1.0
    return 4 * np.abs(ph - 0.5) - 1
def noise(d):
    # 8-bit 风格:采样保持的噪声
    n = int(d * SR); step = 4
    v = rng.uniform(-1, 1, n // step + 1)
    return np.repeat(v, step)[:n]
def env(x, a=0.005, r=0.08, hold=None):
    n = len(x); e = np.ones(n)
    na = max(1, int(a * SR)); e[:na] = np.linspace(0, 1, na)
    nr = max(1, int(r * SR))
    if hold is None: e[-nr:] *= np.linspace(1, 0, nr)
    else: e *= np.exp(-np.arange(n) / (hold * SR))
    return x * e
def sweep(f0, f1, d, kind='pulse', duty=0.5):
    f = np.geomspace(f0, f1, int(d * SR))
    return pulse(f, d, duty) if kind == 'pulse' else (np.where(((np.cumsum(f) / SR) % 1) < .5, 1, -1))
def seq(notes, step, duty=0.5, rel=0.03):
    out = []
    for m in notes:
        out.append(env(pulse(hz(m), step, duty), 0.002, rel) if m else np.zeros(int(step * SR)))
    return np.concatenate(out)

# ---------------- 音效 ----------------
SFX = {
    'blip': env(pulse(hz(84), 0.03, 0.5), 0.001, 0.02) * 0.25,
    'coin': np.concatenate([env(pulse(hz(83), 0.06, .5), .001, .01), env(pulse(hz(88), 0.22, .5), .001, hold=0.08)]) * 0.45,
    'select': np.concatenate([env(pulse(hz(76), 0.05, .25), .001, .01), env(pulse(hz(83), 0.09, .25), .001, .05)]) * 0.4,
    'powerup': seq([72, 76, 79, 84, 88], 0.06, 0.25) * 0.4,
    'levelup': np.concatenate([seq([72, 76, 79, 84], 0.09, .5), env(pulse(hz(88), 0.5, .5), .001, hold=0.25)]) * 0.5,
    'stage': seq([67, 72, 76, 79, 0, 84], 0.07, .5) * 0.4,
    'jump': env(sweep(300, 900, 0.16, duty=.5), .001, .05) * 0.35,
    'land': env(noise(0.08) * 0.8 + sweep(200, 60, 0.08), .001, .06) * 0.5,
    'slam': env(noise(0.35), .001, hold=0.09) * 0.7 + env(sweep(160, 40, 0.35), .001, hold=0.12) * 0.6,
    'hit': env(noise(0.18), .001, hold=0.05) * 0.6 + env(sweep(500, 120, 0.18), .001, .1) * 0.4,
    'hurt': env(sweep(600, 150, 0.35, duty=.25), .001, .1) * 0.45,
    'miss': np.concatenate([env(pulse(hz(60), 0.18, .5), .001, .04), env(pulse(hz(55), 0.18, .5), .001, .04), env(pulse(hz(50), 0.4, .5), .001, .2)]) * 0.4,
    'error': np.concatenate([env(pulse(hz(52), 0.1, .5), .001, .02), env(pulse(hz(52), 0.16, .5), .001, .06)]) * 0.35,
    'down': env(pulse(hz(64), 0.05, .25), .001, .03) * 0.25,
    'throw': env(noise(0.25) * np.linspace(0.2, 1, int(.25 * SR)), .01, .1) * 0.3,
    'wipe': env(noise(0.4) * np.linspace(1, 0.1, int(.4 * SR)), .02, .2) * 0.18,
    'wind': env(noise(2.0) * (0.5 + 0.5 * np.sin(np.linspace(0, 9, int(2 * SR)))), .3, .6) * 0.18,
    'plop': env(sweep(700, 200, 0.12), .001, .06) * 0.4,
    'door': np.concatenate([env(pulse(hz(79), 0.05, .5), .001, .01), env(pulse(hz(84), 0.12, .5), .001, .08)]) * 0.25,
    'orb': env(sweep(200, 800, 0.5, duty=.125) * 0.6 + sweep(203, 806, 0.5, duty=.5) * 0.3, .05, .2) * 0.3,
    'tick': env(pulse(hz(91), 0.04, .5), .001, .02) * 0.3,
    'charge': env(sweep(150, 1200, 1.6, duty=.25), .05, .2) * 0.25,
    'hammer': env(noise(0.12), .001, hold=0.03) * 0.6 + env(pulse(hz(96), 0.12, .5), .001, hold=0.04) * 0.25,
    'slash': env(noise(0.2) * np.linspace(1, 0, int(.2 * SR)) , .001, .1) * 0.45,
    'stack': seq([60, 62, 64, 65, 67, 69, 71, 72], 0.08, .25) * 0.25,
    'pop': env(sweep(400, 1200, 0.06), .001, .03) * 0.25,
}
sfx_track = np.zeros(N)
for ev in meta['sfx']:
    s = SFX.get(ev['name'])
    if s is None: print('unknown sfx', ev['name']); continue
    i = int(ev['t'] * SR); j = min(N, i + len(s)); sfx_track[i:j] += s[:j - i]

# ---------------- 背景音乐 ----------------
BPM = 120; beat = 60 / BPM; s16 = beat / 4; bar = beat * 4
chords = [[48, 52, 55], [45, 48, 52], [41, 45, 48], [43, 47, 50]]   # C Am F G
mel_hook = [76, 0, 79, 76, 72, 0, 74, 76, 77, 0, 76, 74, 72, 0, 0, 0,
            76, 0, 79, 81, 79, 0, 76, 0, 74, 0, 72, 74, 76, 0, 0, 0]
def section(kind):
    """kind: hook | story | story2 | end -> 4 小节(8 秒)"""
    out = np.zeros(int(4 * bar * SR))
    for b, ch in enumerate(chords):
        base = int(b * bar * SR)
        # bass:8 分音符,根音/八度交替
        for k in range(8):
            m = ch[0] - 12 + (12 if k % 2 else 0)
            x = env(tri(hz(m), beat / 2 * 0.9), .002, .02) * (0.32 if kind != 'story' else 0.26)
            i = base + int(k * beat / 2 * SR); out[i:i + len(x)] += x
        # arp:16 分音符琶音
        arp = [ch[0] + 24, ch[1] + 24, ch[2] + 24, ch[1] + 24]
        for k in range(16):
            if kind == 'story' and k % 2: continue
            x = env(pulse(hz(arp[k % 4]), s16 * 0.8, 0.125), .001, .03) * (0.07 if kind.startswith('story') else 0.09)
            i = base + int(k * s16 * SR); out[i:i + len(x)] += x
        # drums
        for k in range(8):
            i = base + int(k * beat / 2 * SR)
            hat = env(noise(0.04), .001, hold=0.012) * (0.10 if kind != 'story' else 0.06)
            out[i:i + len(hat)] += hat
        for k in range(4):
            i = base + int(k * beat * SR)
            if k % 2 == 0 or kind in ('hook', 'end'):
                kick = env(sweep(150, 45, 0.14, duty=.5) * 0.5 + 0, .001, hold=0.05) * (0.35 if kind != 'story' else 0.22)
                out[i:i + len(kick)] += kick
            if k % 2 == 1 and kind in ('hook', 'end', 'story2'):
                sn = env(noise(0.12), .001, hold=0.04) * (0.22 if kind != 'story2' else 0.12)
                out[i:i + len(sn)] += sn
    if kind in ('hook', 'end'):
        lead = seq(mel_hook, s16 * 2, 0.5, 0.05) * 0.13
        out[:len(lead)] += lead[:len(out)]
    return out

plan = [(0, 'hook'), (8, 'hook')]
t = 16
while t < 176:
    plan.append((t, 'story2' if (t // 8) % 4 == 3 else 'story')); t += 8
plan += [(176, 'end'), (184, 'end'), (192, 'end')]
bgm = np.zeros(N)
cache = {}
for st, kind in plan:
    sec = cache.setdefault(kind, section(kind))
    i = int(st * SR); j = min(N, i + len(sec)); bgm[i:j] += sec[:j - i]
# 关卡切换处(像素转场)稍微收一下;结尾淡出
fade = np.ones(N); fo = int(3 * SR); fade[-fo:] = np.linspace(1, 0, fo)
bgm *= fade
# 旁白段落时 BGM 压低一点,给配音留空间
duck = np.ones(N)
for nline in meta['narr']:
    a, b = int(nline['t0'] * SR), int(nline['t1'] * SR); duck[a:b] = 0.7
k = int(0.15 * SR); duck = np.convolve(duck, np.ones(k) / k, mode='same')
bgm *= duck
# 讲故事段(12–172 秒)整体再压低,给配音留空间;开场和结尾保持
g = np.ones(N); a0, a1 = int(12 * SR), int(172 * SR); g[a0:a1] = 0.6
k = int(0.8 * SR); g = np.convolve(g, np.ones(k) / k, mode='same'); g[:k] = 1; g[-k:] = g[-k - 1]
bgm *= g

mix = bgm * 0.55 + sfx_track * 0.6
peak = np.max(np.abs(mix)); mix = mix / peak * 0.89
# 简单立体声:音效居中,BGM 略做 Haas 加宽
d = int(0.012 * SR)
L = mix; R = np.concatenate([np.zeros(d), bgm[:-d] * 0.55 / peak * 0.89]) + sfx_track * 0.6 / peak * 0.89
st = np.stack([L, R], axis=1)
pcm = (np.clip(st, -1, 1) * 32767).astype('<i2')
with wave.open(os.path.join(HERE, 'out/audio.wav'), 'wb') as w:
    w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm.tobytes())

# 同时输出纯 BGM+音效 与 SRT
def ts(x):
    h = int(x // 3600); m = int(x % 3600 // 60); s = x % 60
    return f'{h:02d}:{m:02d}:{int(s):02d},{int(round((s - int(s)) * 1000)):03d}'
with open(os.path.join(HERE, 'out/narration.srt'), 'w', encoding='utf-8') as f:
    for i, n in enumerate(meta['narr'], 1):
        f.write(f"{i}\n{ts(n['t0'])} --> {ts(n['t1'])}\n{n['text']}\n\n")
print('ok', len(meta['sfx']), 'sfx,', len(meta['narr']), 'lines, peak', round(float(peak), 3))
