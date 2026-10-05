// 🔴 [工单 2026-08-06 §4] 浏览器支持底线的**机器闸** —— 只开 compat/compat 一条规则。
//
// 存在的理由:2026-07-01 全站扫过一次原生 confirm/alert,靠的是人跑 grep;没有闸,
// 新代码照样加,半年后又是 91 处(§1 就是这么复发的)。**只写进文档的底线,读过也会踩。**
//
// 为什么不是一份完整的 lint 配置:仓库此前根本没有 eslint 配置(`"lint": "eslint ."`
// 是条死脚本)。一次性给一个几千文件的前端上全量 lint 会淹没真正的信号,
// 也不是本工单的范围。这里**只**判一件事:代码里用的浏览器 API 有没有超出
// package.json 里 `browserslist` 声明的底线。底线本身是 Owner 拍的,见 package.json。
//
// 判别力自证:`npm run verify:browser-baseline -- --selftest` 会植入一个故意超标的
// API(Object.groupBy)并要求本闸报红 —— 只跑现有代码 = 恒绿,零判别力。
import compat from 'eslint-plugin-compat';
import tsParser from '@typescript-eslint/parser';

export default [
  {
    files: ['src/**/*.{ts,tsx,js,jsx,mjs}'],
    // 🔴 这些目录不是发给浏览器的运行时代码,不受底线约束:
    ignores: [
      'src/**/*.test.{ts,tsx}',
      'src/**/__tests__/**',
      'src/**/fixtures/**',
      'src/sandbox/**',
    ],
    languageOptions: {
      parser: tsParser,
      ecmaVersion: 2024,
      sourceType: 'module',
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    settings: {
      // 不写 browsers = 读 package.json 的 browserslist(唯一 SSOT,不在这里复制一份)
      //
      // 🔴 lintAllEsApis 必须开:关掉它,插件只判 DOM/BOM API,**ES 内建全不判**。
      //    自证当场证明了这一点 —— 关掉时故意植入的 `Object.groupBy`(Chrome 117 起)
      //    一声不吭。那正是本闸最该拦的东西:新写的代码用了太新的语言 API。
      //    我一度为了压 Report/Notification 的误报把它关了,等于把闸的一半判别力也关了;
      //    误报应该用下面的 polyfills 精确豁免,不是靠关总闸。
      lintAllEsApis: true,
      polyfills: [
        // 🔴 这里的每一条都是**关掉闸的一部分**,所以必须写清为什么,并配一条反向锁。
        //
        // 'Report' / 'Notification' —— **纯误报**,不是真的用了这两个 Web API。
        //   eslint-plugin-compat 会把局部标识符按全局名匹配:本仓有大量
        //   `const report = ...` / `notification.is_read`(服务端下发的站内信对象),
        //   于是报出 136 处 "Report is not supported in Safari 15.4" 和 12 处 Notification。
        //   实测全站没有任何一处 `new Notification(` / `Notification.requestPermission` /
        //   `ReportingObserver`。
        //   🔴 豁免会让闸对这两个**真 API** 也失明,所以配了反向锁:
        //      scripts/verify-browser-baseline.mjs 会断言这些真调用形态一处都不许出现。
        //      哪天真要用 Web Notification,那条锁会先红,逼人回来重新判定。
        'Report',
        'Notification',
      ],
    },
    // 🔴 忽略文件内的 eslint 行内注释:本仓有大量 `// eslint-disable-next-line react-hooks/...`
    // 之类指向**没被本配置加载**的规则,eslint 会把它们全报成 "Definition for rule not found"。
    // 那些噪声会淹没本闸唯一关心的信号。顺带的好处:没人能用一行 disable 注释把底线闸关掉。
    linterOptions: { noInlineConfig: true, reportUnusedDisableDirectives: 'off' },
    plugins: { compat },
    rules: { 'compat/compat': 'error' },
  },
];
