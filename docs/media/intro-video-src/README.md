# 介绍视频源码

README 开头那段像素风介绍视频《王老板的 AI 上榜之旅》的源码。画面、背景音乐和音效全部由代码生成。

- `index.html` / `film.js`:画面与分镜时间轴(GSAP)。每一帧只由时间 t 决定,所以逐帧截图就能得到稳定的视频。
- `sprites.js`:像素角色,用字符网格画成。
- `audio.py`:按时间轴里记录的音效触发点,合成 8-bit 背景音乐与音效,并导出旁白 SRT。
- `ui/`:视频里「电视」中出现的系统界面截图(演示数据)。

## 重新渲染

需要 Node 18+、Python 3(numpy)、ffmpeg。

```bash
npm install
npx playwright install chromium      # 已有 Chromium 时可改为设置 CHROMIUM_PATH
npm run preview                      # 抽几帧截图到 prev/,先检查画面
npm run render                       # 输出 out/v_subs.mp4、out/v_nosubs.mp4、out/audio.wav、out/narration.srt
ffmpeg -i out/v_subs.mp4 -i out/audio.wav -c:v copy -c:a aac -b:a 192k -shortest out/omnirank-intro.mp4
```

改旁白:直接改 `film.js` 里的 `nar('文字', 开始秒, 结束秒)`。

## 授权

像素字体是 [Fusion Pixel Font](https://github.com/TakWolf/fusion-pixel-font)(SIL OFL 1.1)。动画库是 [GSAP](https://gsap.com)(免费的标准许可)。
