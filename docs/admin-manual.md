# 管理员手册:没有页面的四类管理操作

有四类记录只能由管理员处理,而系统**没有为它们做前端页面**。不处理的话,这些记录会一直挂着。本页说明它们在哪、怎么处理。

## 怎么调用

- 这些接口都在后端服务上。本地运行时,可在后端的 `/docs`(Swagger UI)里直接试调,接口全集在 `/openapi.json`。
- 先用管理员账号登录拿令牌,之后每个请求带 `Authorization: Bearer <token>`:

```bash
curl -s -X POST http://<后端地址>/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username": "<管理员账号>", "password": "<口令>"}'
# 返回里的 "token" 字段就是令牌
```

- 未登录返回 401,不是管理员返回 403。

---

## 1. 资金补偿工单 `/api/admin/fund-recovery`

资金操作失败后系统自动生成的补偿工单。自动重试解决不了的,会停在 `manual` 状态,等管理员人工结案。

| 方法与路径 | 作用 |
|---|---|
| `GET /api/admin/fund-recovery?status=&limit=&offset=` | 列工单。`status` 取 `pending` / `processing` / `manual` / `resolved` / `failed`,不填 = 所有未结案的 |
| `GET /api/admin/fund-recovery/{order_id}` | 看单个工单 |
| `POST /api/admin/fund-recovery/{order_id}/resolve` | 标为已解决。请求体 `{"note": "处理说明"}` |
| `POST /api/admin/fund-recovery/{order_id}/fail` | 标为无法解决。请求体 `{"note": "原因"}` |
| `POST /api/admin/fund-recovery/{order_id}/settle-geo-task` | **GEO 结算冲突工单专用**。请求体 `{"note": "…", "terminal": "failed|cancelled|timeout"}`,`terminal` 可不填 |

⚠️ 来源是 GEO 结算冲突的工单,**必须**用 `settle-geo-task` 收口 —— 它会同时把关联的 GEO 任务落到与实际扣款状态一致的终态。对这类工单调用通用的 `resolve` / `fail` 会被拒绝(400)。任务终态由后端按实际扣款状态推导,请求里的 `terminal` 只是偏好,与实际状态矛盾时以实际为准或拒绝。

## 2. 服务费审核 `/api/admin/service-fee`

需要**管理员**或**财务审核员**(`finance_reviewer`)角色。

| 方法与路径 | 作用 |
|---|---|
| `GET /api/admin/service-fee/conversion-reviews?limit=&offset=` | 列大额服务费转换的待审记录 |
| `POST /api/admin/service-fee/conversion-reviews/{review_id}/decide` | 审批。请求体 `{"approve": true, "note": "审核备注"}` |
| `GET /api/admin/service-fee/withdraw-requests?status=&limit=&offset=` | 列提现申请(该功能总开关关闭时返回 `feature_disabled`) |
| `POST /api/admin/service-fee/withdraw-requests/{settlement_id}/approve` | 批准。请求体 `{"note": "", "settlement_evidence": "转账凭证 URL,可后补", "second_signer_id": null}` |
| `POST /api/admin/service-fee/withdraw-requests/{settlement_id}/reject` | 拒绝。请求体 `{"reason": "拒绝理由(至少 2 个字)"}` |
| `POST /api/admin/service-fee/withdraw-requests/{settlement_id}/partial` | 部分结算。请求体 `{"partial_amount_yuan": 100.0, "note": "", "settlement_evidence": ""}` |
| `POST /api/admin/service-fee/clawback/manual` | 人工追回。请求体 `{"user_id": 0, "source_order_id": "…", "refund_amount_yuan": 0.0, "refund_reason": "legal_dispute|platform_force|fraud|chargeback"}` |

用户侧发起大额服务费转换后,记录进入待审;**这里是唯一的审批入口**。

## 3. 渠道合作申请 `/api/admin/channel-partner-requests`

服务商提交的下级渠道合作申请。

| 方法与路径 | 作用 |
|---|---|
| `GET /api/admin/channel-partner-requests?status=` | 列申请。`status` 取 `pending` / `approved` / `rejected` / `cancelled` |
| `POST /api/admin/channel-partner-requests/{request_row_id}/approve` | 批准。请求体 `{"channel_expected_version": 1, "decision_note": "审批说明", "cost_multiplier_bps": 10000}`;`cost_multiplier_bps` 可不填(沿用申请值),取值 10000–100000(10000 = 1.0 倍) |
| `POST /api/admin/channel-partner-requests/{request_row_id}/reject` | 拒绝。请求体 `{"decision_note": "拒绝理由(2–500 字)"}` |

`channel_expected_version` 是并发保护:填**被申请用户**(申请里的 `target_user_id`)当前渠道关系的版本号,从 `GET /api/admin/user-governance/users/{target_user_id}` 返回的渠道关系里的 `relationship_version` 读取。版本已变则拒绝,重新读取后再批。

⚠️ 这组接口所在模块在启动时**必须能导入**,导入失败服务会拒绝启动(其余三组导入失败只会少掉接口、服务照常起)。

## 4. 飞轮健康(只读)`/api/admin/flywheel`

查看数据飞轮各后台任务是否按时运行,以及积累下来的数据。**只读,不改任何记录。**

| 方法与路径 | 作用 |
|---|---|
| `GET /api/admin/flywheel/health` | 总览:每个任务最近一次成功时间、是否超期,以及正在告警的检查项 |
| `GET /api/admin/flywheel/runs?job_key=&limit=` | 任务运行记录 |
| `GET /api/admin/flywheel/judgments?point_key=&limit=` | 判定记录 |
| `GET /api/admin/flywheel/corpus-value?industry_key=&limit=` | 语料价值标签 |
| `GET /api/admin/flywheel/diagnosis-reuse` | 诊断复用素材 |
| `GET /api/admin/flywheel/assignments?industry_key=&limit=` | 写作任务分配 |

---

## 5. 发布渠道配置与自检

发布渠道只由 `.env` 决定,没有页面开关。

| 键 | 取值 | 说明 |
|---|---|---|
| `PUBLISH_CHANNEL` | 不设 / `dry_run` / `api` | 不设 = 未接入;填了认不出的值,或 `api` 没配齐地址与 Key,都会在日志里记一条告警,并按未接入处理 |
| `PUBLISH_CHANNEL_API_BASE` | 服务地址,到 `/v1` | 仅 `api` 模式;没有默认值 |
| `PUBLISH_CHANNEL_API_KEY` | API Key | 仅 `api` 模式;没有默认值 |
| `PUBLISH_CHANNEL_DRY_RUN_DELAY_MINUTES` | 分钟数 | 仅 `dry_run`;下单后多久标为已发布,不设按 2 |

**三种状态下的表现**

- **未接入**:所有发布入口返回 HTTP 503,`code` = `ADMISSION_UNAVAILABLE`,提示「发布渠道未接入,接入后才能发布。」。不扣算力,不建发布任务。
- **`dry_run`**:订单号以 `DRY-` 开头;满设定的分钟数后,由定时任务标为已发布,结果链接形如 `about:blank#dry-run-<订单号>`(故意不像真链接)。演示媒体名字都带「(演示)」。在途订单可撤,撤单原额退回算力。不发出任何网络请求。
- **`api`**:每个媒体单独报价、单独下单,撤一个媒体不影响其他媒体;状态由定时任务逐单查询回写;渠道拒稿或撤单,按原有的失败退款链原额退回算力。只接软文。
  - **渠道临时出错时**:报价或下单遇到渠道 5xx、限流、超时等,会带着同一个幂等键自动再试 2 次,不会重复下单。
  - 渠道**明确拒绝**的,原额退回算力。
  - 重试后仍**不确定**渠道有没有受理的,条目进入「待同步」,先不退;定时任务会去渠道核对这张单,核对到就接着走,**满 24 小时**仍核对不到才判失败、原额退回。

**自检**

1. 未接入:随便发起一次发布 ⇒ 503 + 上面那句提示;用户算力余额不变。
2. `dry_run`:先在「用户管理 → 钱包与账单 → 校正算力」给测试账号加**付费算力**(赠送算力付不了发布,会返回 402 `INSUFFICIENT_PAID_POINTS`;接口是 `PUT /api/admin/user-governance/users/{user_id}/wallet-adjustment`,`point_type` 填 `paid`)。再发一篇到任一「(演示)」媒体 ⇒ 立刻扣付费算力、得到 `DRY-` 单号;等设定的分钟数 ⇒ 状态变为已发布。再发一篇、在途时撤单 ⇒ 算力原额退回。
3. `api`:先挑一家低价媒体试发一篇软文,确认状态回写和结果链接,再正式使用。

**出了问题先看**:定时任务容器(`omnirank-cron-blue`)是否在运行 —— 发布状态全靠它回写。

## 部署后自检

服务起来后,确认四组接口都已注册:

```bash
curl -s http://<后端地址>/openapi.json | python -c "import sys,json;p=json.load(sys.stdin)['paths'];\
need=['/api/admin/fund-recovery','/api/admin/service-fee','/api/admin/channel-partner-requests','/api/admin/flywheel'];\
miss=[n for n in need if not any(k.startswith(n) for k in p)];print('MISSING',miss) if miss else print('OK 4/4')"
```

应输出 `OK 4/4`。
