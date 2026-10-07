"""给视频配音 + 配 BGM,输出成片。

    python3 dub.py explainer            # MG 讲解版
    python3 dub.py pixel                # 像素小剧场版
    python3 dub.py explainer --fake     # 不调接口,用提示音占位,检查流程

流程:
  1. 按 narration.json 的分段调 cloud_tts.py 合成配音(已生成的段落直接复用,不重复付费);
  2. 在句间停顿处把每段配音切成单句;
  3. 每句对齐到画面里对应内容出现的时刻;配音比画面长时,只放慢这一拍的中间段,
     动画开头和转场保持原速;
  4. 渲染画面(render.cjs),合成 BGM/音效,人声出现时压低音乐,最后统一响度并封装。
需要:Node + playwright、Python 3(numpy、scipy)、ffmpeg;真配音需要环境变量 VOLCANO_TTS_API_KEY。
"""
import argparse, json, os, re, subprocess, sys, wave
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SR = 48000
sys.path.insert(0, HERE)


def sh(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def dur_of(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', path],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip())


def read_wav(path):
    with wave.open(path) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), '<i2').astype(np.float32) / 32768
        if w.getnchannels() == 2:
            x = x.reshape(-1, 2).mean(axis=1)
        assert w.getframerate() == SR
    return x


def write_wav(path, x):
    x = np.clip(x, -1, 1)
    st = x if x.ndim == 2 else np.stack([x, x], axis=1)
    with wave.open(path, 'wb') as w:
        w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((st * 32767).astype('<i2').tobytes())


def tts_text(text, geo):
    # 「GEO」容易被读成「GE～O」,只改配音输入,字幕保持原样
    t = re.sub(r'GEO', geo, text)
    t = re.sub(r'SEO', 'S E O', t)
    return t


def synth(seg_dir, sid, text, fake, lines):
    mp3 = os.path.join(seg_dir, f'{sid}.mp3')
    rec = mp3 + '.request.json'
    if os.path.exists(mp3):
        return mp3
    if os.path.exists(rec):
        raise SystemExit(f'{rec} 存在但没有音频:上次请求结果不明,先检查再决定是否重试。')
    if fake:
        # 占位:每句一段提示音,句间 0.45 秒停顿,语速按每秒 4.7 字估
        parts = []
        for ln in lines:
            d = max(0.6, len(ln['text']) / 4.7)
            t = np.arange(int(d * SR)) / SR
            parts.append(0.12 * np.sin(2 * np.pi * 220 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t)))
            parts.append(np.zeros(int(0.45 * SR)))
        wav = mp3[:-4] + '.fake.wav'
        write_wav(wav, np.concatenate(parts))
        sh(['ffmpeg', '-v', 'error', '-y', '-i', wav, '-c:a', 'libmp3lame', '-b:a', '128k', mp3])
        return mp3
    txt = os.path.join(seg_dir, f'{sid}.txt')
    open(txt, 'w', encoding='utf-8').write(text)
    sh([sys.executable, os.path.join(HERE, 'cloud_tts.py'), txt, mp3])
    return mp3


def silences(wav):
    p = subprocess.run(['ffmpeg', '-v', 'info', '-i', wav, '-af', 'silencedetect=noise=-38dB:d=0.16', '-f', 'null', '-'],
                       capture_output=True, text=True)
    st = [float(m) for m in re.findall(r'silence_start: ([\d.]+)', p.stderr)]
    en = [float(m) for m in re.findall(r'silence_end: ([\d.]+)', p.stderr)]
    return list(zip(st, en))


def spoken_len(text):
    """估算念出来的长度:一个英文单词约等于 2 个汉字,空格不算。"""
    return len(re.sub(r'[A-Za-z]+', 'xx', text.replace(' ', '')))


def sub_pieces(text, max_len):
    """长句字幕按标点拆成几段,每段尽量不超过 max_len 字,避免折行。"""
    if spoken_len(text) <= max_len:
        return [text]
    parts = re.findall(r'[^，。；：？！、—]+[，。；：？！、—]*', text)
    out, cur = [], ''
    for p in parts:
        if cur and spoken_len(cur + p) > max_len:
            out.append(cur); cur = p
        else:
            cur += p
    if cur:
        out.append(cur)
    return [o.rstrip('，、；：') for o in out]


def split_lines(x, sil, lines):
    """在句间停顿处切开:按字数估出每个句界的位置,取离它最近的停顿。"""
    total = len(x) / SR
    nz = np.nonzero(np.abs(x) > 0.01)[0]
    v0 = nz[0] / SR if len(nz) else 0; v1 = nz[-1] / SR if len(nz) else total
    chars = np.cumsum([spoken_len(l['text']) for l in lines]); chars = chars / chars[-1]
    cuts, used = [], set()
    for k in range(len(lines) - 1):
        exp = v0 + chars[k] * (v1 - v0)
        # 句末停顿通常比逗号长:离预估位置近、停顿又长的优先
        cands = [(abs((s + e) / 2 - exp) - 3.0 * (e - s), i) for i, (s, e) in enumerate(sil)
                 if i not in used and (s + e) / 2 > (cuts[-1] if cuts else 0) and abs((s + e) / 2 - exp) < 0.25 * (v1 - v0)]
        if cands:
            _, i = min(cands); used.add(i); s, e = sil[i]; cuts.append((s + e) / 2)
        else:
            cuts.append(exp)
    bounds = [0.0] + cuts + [total]
    chunks = []
    for i in range(len(lines)):
        a, b = int(bounds[i] * SR), int(bounds[i + 1] * SR)
        c = x[a:b]
        nzc = np.nonzero(np.abs(c) > 0.01)[0]
        lead = nzc[0] if len(nzc) else 0
        c = c[max(0, lead - int(0.03 * SR)):]           # 去掉句首空白
        nzc = np.nonzero(np.abs(c) > 0.01)[0]
        speech = (nzc[-1] / SR if len(nzc) else len(c) / SR)
        c = c[:int((speech + 0.12) * SR)]
        chunks.append((c, speech))
    return chunks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('page', choices=['explainer', 'pixel'])
    ap.add_argument('--fake', action='store_true')
    ap.add_argument('--geo', default='GEO', help='配音输入里 GEO 的写法(试听后原文读得最好,需要时可改成 "G E O")')
    ap.add_argument('--out', default=None)
    ap.add_argument('--gap', type=float, default=0.35, help='句与句之间的停顿(秒)')
    ap.add_argument('--gap-scene', type=float, default=0.6, help='换场景前的停顿(秒)')
    ap.add_argument('--max-speed', type=float, default=None, help='画面停留段最多加速几倍(默认取 narration.json 的 max_speed)')
    ap.add_argument('--end-hold', type=float, default=4.5, help='最后一句之后结尾卡至少停留几秒')
    ap.add_argument('--skip-render', action='store_true', help='画面已渲染过时只重做音频和封装')
    ap.add_argument('--preview', default=None, help='只渲染这些视频秒数的截图,逗号分隔')
    a = ap.parse_args()
    page_dir = os.path.join(ROOT, a.page)
    cfg = json.load(open(os.path.join(page_dir, 'narration.json'), encoding='utf-8'))
    out = a.out or os.path.join(ROOT, 'out', a.page + ('-fake' if a.fake else ''))
    seg_dir = os.path.join(out, 'voice'); os.makedirs(seg_dir, exist_ok=True)

    # 1+2 合成并切句
    lines_all = []
    for seg in cfg['segments']:
        text = ''.join(l['text'] for l in seg['lines'])
        mp3 = synth(seg_dir, seg['id'], tts_text(text, a.geo), a.fake, seg['lines'])
        wav = mp3[:-4] + '.48k.wav'
        sh(['ffmpeg', '-v', 'error', '-y', '-i', mp3, '-ar', str(SR), '-ac', '1', '-c:a', 'pcm_s16le', wav])
        x = read_wav(wav)
        for k, (ln, (chunk, speech)) in enumerate(zip(seg['lines'], split_lines(x, silences(wav), seg['lines']))):
            lines_all.append({**ln, 'audio': chunk, 'speech': speech, 'seg': seg['id'], 'last_in_seg': k == len(seg['lines']) - 1})
    lines_all.sort(key=lambda l: l['t'])

    # 3 时间映射:knots = [(视频秒, 时间轴秒)]
    D = cfg['duration']
    max_speed = a.max_speed or cfg.get('max_speed', 2.5)
    knots = [(0.0, 0.0)]
    tv = lines_all[0]['t']; knots.append((tv, lines_all[0]['t']))
    place = []
    for i, ln in enumerate(lines_all):
        t0 = ln['t']; t1 = lines_all[i + 1]['t'] if i + 1 < len(lines_all) else D
        tlen = t1 - t0
        # 画面跟着配音走:句间只留短停顿,换场景时多留一点
        gap = a.gap_scene if ln['last_in_seg'] else a.gap
        need = ln['speech'] + gap
        if i + 1 == len(lines_all):
            need = max(need, ln['speech'] + a.end_hold)   # 结尾卡(GitHub 地址)留足时间
        h = min(1.6, tlen * 0.4)          # 这一拍开头的入场动画保持原速
        r = min(0.6, (tlen - h) * 0.5)    # 结尾(转场)保持原速
        mid_t = max(0.0, tlen - h - r)
        mid_v = max(need - h - r, mid_t / max_speed, 0.0)   # 中间的停留段可压缩/拉长
        vlen = h + mid_v + r
        place.append((tv, ln))
        knots += [(tv + h, t0 + h), (tv + h + mid_v, t1 - r)]
        knots.append((tv + vlen, t1))
        tv += vlen
    total = tv
    stretch = total / D
    print(f'画面 {D:.1f}s → 配音后 {total:.1f}s(整体 {stretch:.2f} 倍)')

    def to_video(tt):
        for (v0, t0), (v1, t1) in zip(knots, knots[1:]):
            if tt <= t1:
                return v0 if t1 == t0 else v0 + (tt - t0) * (v1 - v0) / (t1 - t0)
        return knots[-1][0]

    subs = None
    if cfg['subtitles']:
        subs = []
        for v, ln in place:
            pieces = sub_pieces(ln['text'], cfg.get('sub_max', 24))
            w = [spoken_len(p) for p in pieces]; tot = sum(w); t = v
            for p, wi in zip(pieces, w):
                d = ln['speech'] * wi / tot
                subs.append({'v0': round(t, 3), 'v1': round(t + d + (0.15 if p is pieces[-1] else 0), 3), 'text': p})
                t += d
    mp = {'duration': round(total, 3), 'knots': knots, 'subs': subs, 'variants': cfg.get('variants') or (['subs', 'nosubs'] if cfg['subtitles'] else ['main'])}
    json.dump(mp, open(os.path.join(out, 'map.json'), 'w'), ensure_ascii=False)
    # SRT(按真实配音时间)
    def ts(x):
        return f'{int(x // 3600):02d}:{int(x % 3600 // 60):02d}:{int(x % 60):02d},{int(round((x % 1) * 1000)) % 1000:03d}'
    with open(os.path.join(out, 'narration.srt'), 'w', encoding='utf-8') as f:
        rows = subs or [{'v0': v, 'v1': v + ln['speech'] + 0.15, 'text': ln['text']} for v, ln in place]
        for i, r in enumerate(rows, 1):
            f.write(f"{i}\n{ts(r['v0'])} --> {ts(r['v1'])}\n{r['text']}\n\n")

    render = ['node', os.path.join(HERE, 'render.cjs'), page_dir, out, os.path.join(out, 'map.json')]
    if a.preview:
        sh(render + ['--preview', a.preview]); return

    # 4 画面
    variants_done = all(os.path.exists(os.path.join(out, f'video_{v}.mp4')) for v in mp['variants'])
    if not (a.skip_render and variants_done):
        sh(render)
    meta = json.load(open(os.path.join(out, 'meta.json')))
    n = int((total + 0.5) * SR)

    # 人声轨
    voice = np.zeros(n)
    for v, ln in place:
        i = int(v * SR); c = ln['audio']; e = min(n, i + len(c)); voice[i:e] += c[:e - i]
    rms = np.sqrt(np.mean(voice[np.abs(voice) > 0.01] ** 2)) if np.any(np.abs(voice) > 0.01) else 1
    voice *= 10 ** (-15 / 20) / rms        # 人声约 -15 dBFS RMS

    # BGM / 音效
    if a.page == 'pixel':
        mmeta = {'dur': total, 'narr': [], 'sfx': [{'t': round(to_video(s['t']), 3), 'name': s['name']} for s in meta['sfx']]}
        json.dump(mmeta, open(os.path.join(out, 'meta_mapped.json'), 'w'))
        env = {**os.environ, 'META': os.path.join(out, 'meta_mapped.json'), 'OUT': out, 'STEMS': '1'}
        sh([sys.executable, os.path.join(ROOT, 'pixel', 'audio.py')], env=env)
        def load(p):
            sh(['ffmpeg', '-v', 'error', '-y', '-i', p, '-ar', str(SR), '-ac', '1', p + '.48k.wav'])
            y = read_wav(p + '.48k.wav'); return np.pad(y, (0, max(0, n - len(y))))[:n]
        bgm = load(os.path.join(out, 'bgm.wav')); sfx = load(os.path.join(out, 'sfx.wav'))
        bgm_db, sfx_db = -25, -24
    else:
        import bgm_soft
        bgm = bgm_soft.make(n / SR).mean(axis=1)[:n]
        sfx = bgm_soft.soft_sfx([{'t': to_video(e['t']), 'name': e['name']} for e in meta['sfx']], n)
        bgm_db, sfx_db = -25, -27
    # BGM 拉平:按 2 秒窗口的音量做慢速自动增益,整首一样响(原曲开头结尾更响,中段会被压没)
    w = int(2 * SR)
    loud = np.sqrt(np.convolve(bgm ** 2, np.ones(w) / w, mode='same')) + 1e-6
    target = 10 ** (bgm_db / 20)
    agc = np.clip(target / loud, 0.2, 6.0)
    agc = np.convolve(agc, np.ones(w) / w, mode='same')
    bgm = bgm * agc
    # 音效:按统一电平归一,再软限幅,峰值不超过人声
    if sfx_db is not None and np.any(sfx):
        act = np.abs(sfx) > 1e-4
        srms = np.sqrt(np.mean(sfx[act] ** 2)) if np.any(act) else 1
        sfx = sfx * (10 ** (sfx_db / 20) / srms)
        lim = 10 ** (-12 / 20)
        sfx = lim * np.tanh(sfx / lim)

    # 人声出现时把 BGM 压低约 8 dB(平滑的侧链)
    env = np.abs(voice); k = int(0.25 * SR)
    env = np.convolve(env, np.ones(k) / k, mode='same')
    depth = cfg.get('duck', 0.45)            # 0.45 ≈ 人声时音乐压低约 5 dB
    duck = 1 - depth * np.clip(env / (env.max() * 0.15 + 1e-9), 0, 1)
    k2 = int(0.3 * SR); duck = np.convolve(duck, np.ones(k2) / k2, mode='same')
    sduck = 1 - 0.35 * np.clip(env / (env.max() * 0.15 + 1e-9), 0, 1)   # 人声时音效也让一点
    mix = voice + bgm * duck + sfx * sduck
    write_wav(os.path.join(out, 'mix_raw.wav'), mix / max(1.0, np.max(np.abs(mix)) / 0.95))
    for name, arr in (('voice', voice), ('bgm', bgm * duck), ('sfx', sfx * sduck)):
        write_wav(os.path.join(out, f'stem_{name}.wav'), arr)   # 分轨,方便检查音量

    # 5 统一响度并封装
    for v in mp['variants']:
        src = os.path.join(out, f'video_{v}.mp4')
        dst = os.path.join(out, f'{a.page}-{v}.mp4' if v != 'main' else f'{a.page}.mp4')
        sh(['ffmpeg', '-v', 'error', '-y', '-i', src, '-i', os.path.join(out, 'mix_raw.wav'),
            '-af', 'loudnorm=I=-16:TP=-1.5:LRA=11', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k', '-ar', '48000',
            '-shortest', '-movflags', '+faststart', dst])
        print('输出', dst)


if __name__ == '__main__':
    main()
