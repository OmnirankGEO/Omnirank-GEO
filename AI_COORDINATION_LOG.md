# Codex 图文创作/发布联调协作记录

## 2026-09-20 — 完整共享发布账号市场

- Owner要求图文直接使用发布投放完整筛选能力，不保留简化Demo。基线deployment-reported `c0ebd2301f3099f4b7d7aae0cd65e27dfcafd8f6`。
- 唯一源码writer仍为`/root/gsd_debugger`，本工作树；主任务只写树外本地目录夹具、HTTP/PG/浏览器证据并在冻结后完整构建。
- 同包共享授权：Publishing/Writing抽取同一目录组件；`api/meijiehezi_api.py`、`db/meijiehezi_db.py`现有filters可选can_tuwen与同池facet，只读参数，不新增表/端点/计费。图文保留prepare-v2/preview/batch/commands，视频保留原发布实现。
- 复用现有资格条件、价目服务、L1分类和原始公开字段；不按内部成本在浏览器算价格。保护文件零diff，不部署、不push。
- 主任务真实浏览器补证旧command媒体名为空；授权`api/geo_image_note_api.py::_load_command`窄读侧补既有目录公开名字，订单历史名优先、目录名仅空值回退，原owner/attempt根边界不变。不改发单或计费；回执优先展示，失败换号后才重新显示市场。

## 2026-09-20 — geo-image-publish-flow / U9 失败后换账号

- 唯一 source writer：`/root/gsd_debugger`，工作树 `C:/Users/DemoUser/.codex/worktrees/geo-image-publish-flow/omnirank-ai`。主任务只读源码、独立维护树外本地 PG16/API/浏览器验证。
- 用户授权范围：图文制作与发布连通并在本地真实环境验证。主任务明确授权保存 revision 对齐及本次失败恢复链同包收口。
- 共享调用兼容：`services/xiaobang_publish_execute.py` 和图文 HTTP 复用 `materialize_publish_batch`；新增 `PublishAttemptConflict` 必须同时映射到既有 `IntentError` 409/查看进度。主任务已同意仅改该异常映射并加测试，不改变小榜流程、权限、计费。
- 无新生产表/API/DDL；现有 attempt 根、重试关联、替换字段复用；资金/鉴权/连接保护文件零改动；无部署。

## 2026-09-20 — Step1 默认关键词与抖音原地快捷入口

- 用户验收否决“先付130生成选题才有入口”，主任务授权本代理同一独占树修改现役 keywords GET 的可选只读工作投影（不是新API），并同步Writing前端。原confirmed/paid来源与ck.id保留，不动保护文件。
- 后续用户要求当前作品原地发布，并明确图文主要发抖音、这里是发布投放快捷入口。复用同一ImageNotePublishInline与中央发布后台，窄platformScope只由Writing传入；中央多平台默认不变。
- 新增关键词PG17例、纯函数总60例；新包47例亲跑通过。主任务独立维护API/runtime/浏览器/SDK替代适配器和验收报告；本人不运行生产部署或外部发布。
