# 介绍视频源码

| 目录 | 视频 | 成片 |
|---|---|---|
| `explainer/` | 4 分钟 MG 讲解版(README 首页) | `docs/media/omnirank-intro.mp4` |
| `pixel/` | 3 分钟像素小剧场《王老板的 AI 上榜之旅》 | `docs/media/omnirank-pixel.mp4` |

画面用 GSAP 时间轴写成,每一帧只由时间决定,逐帧截图后编码成视频。背景音乐和音效由代码合成(`tools/bgm_soft.py`、`pixel/audio.py`),没有使用第三方素材。

## 改旁白、重新配音

1. 改 `explainer/narration.json` 或 `pixel/narration.json`。每句的 `t` 是它要对齐的画面时刻(时间轴秒)。
2. 删掉 `out/<版本>/voice/` 里对应段落的 `.mp3` 和 `.request.json`,没改的段落会直接复用,不会重复调用接口。
3. 运行:

```bash
npm install                                   # gsap、字体、playwright
export VOLCANO_TTS_API_KEY=...                # 火山引擎声音复刻的 Key,只放环境变量
python3 tools/dub.py explainer                # 或 pixel
```

`dub.py` 会:调 `tools/cloud_tts.py` 合成配音,在句间停顿处切成单句,把每句对齐到画面;配音比画面长时只放慢那一拍的中间段;渲染画面;人声出现时压低 BGM;统一响度到 -16 LUFS 后输出到 `out/`。

- `--fake`:不调接口,用提示音占位,用于检查流程。
- `--preview 3,60,120`:只截这几秒的画面。
- `--skip-render`:画面已渲染过时只重做音频。
- `--geo "G E O"`:如果 GEO 读音不对,可以只改配音输入的写法,字幕不受影响。

需要 Node 18+、Python 3(numpy、scipy)、ffmpeg。

## 授权

像素字体是 [Fusion Pixel Font](https://github.com/TakWolf/fusion-pixel-font)(SIL OFL 1.1),讲解版字体是 Noto Sans SC(SIL OFL 1.1),动画库是 [GSAP](https://gsap.com)。
