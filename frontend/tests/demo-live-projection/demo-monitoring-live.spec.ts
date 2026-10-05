/**
 * D10 演示实时投影 · 端到端门（SSOT v2.0 §8.2 · Owner 2026-07-25）
 *
 * ⚠️ 状态：**已写未跑**。本 spec 需要集成环境（跑起来的后端 + 库里存在有效
 *    demo grant + 已播种的演示账号），本地 Builder 工作树不具备，故交付时
 *    **未执行**。不要把它当成已通过的门（feedback_test_must_follow_code_change /
 *    「真 PG 判别待集成环境·别写全绿」）。
 *
 * 运行前置（集成环境）：
 *   DEMO_BASE_URL          后端可达地址
 *   DEMO_VIEWER_USER       演示账号（对 DEMO_BRAND_ID 持有有效 demo grant）
 *   DEMO_VIEWER_PASS
 *   DEMO_BRAND_ID          被授权品牌
 *   DEMO_CASE_ID           对应 case
 *   DEMO_FOREIGN_BRAND_ID  未授权的另一品牌（跨租户判别用）
 *
 * 覆盖 Owner 预告的三个攻击面 + D10 的核心承诺（看到实时真数据）。
 */
import { expect, test } from 'playwright/test';

const BASE = process.env.DEMO_BASE_URL ?? 'http://localhost:8000';
const BRAND = process.env.DEMO_BRAND_ID ?? '';
const CASE = process.env.DEMO_CASE_ID ?? '';
const FOREIGN = process.env.DEMO_FOREIGN_BRAND_ID ?? '';
const USER = process.env.DEMO_VIEWER_USER ?? '';
const PASS = process.env.DEMO_VIEWER_PASS ?? '';

const ready = Boolean(BRAND && CASE && USER && PASS);
test.skip(!ready, 'D10 e2e 需要集成环境（DEMO_* 环境变量未配置）');

async function demoToken(request: any): Promise<string> {
  const res = await request.post(`${BASE}/api/auth/login`, {
    data: { username: USER, password: PASS },
  });
  expect(res.ok()).toBeTruthy();
  return (await res.json()).token;
}

function demoHeaders(token: string) {
  return {
    Authorization: `Bearer ${token}`,
    'X-Demo-Brand-ID': BRAND,
    'X-Demo-Case-ID': CASE,
  };
}

test.describe('D10 · 演示 = 实时只读投影', () => {
  test('监测页看到真实关键词与结果，且数据源标为 live', async ({ request }) => {
    const token = await demoToken(request);
    const res = await request.get(
      `${BASE}/api/monitoring/keywords?brand_id=${BRAND}`,
      { headers: demoHeaders(token) },
    );
    expect(res.status()).toBe(200);
    // D10 核心承诺：走真实 handler，不是冻结快照。
    expect(res.headers()['x-demo-data-source']).toBe('live');
    expect(res.headers()['x-demo-mode']).toBe('demo');
    const body = await res.json();
    const rows = body.keywords ?? body.data ?? body;
    expect(Array.isArray(rows) ? rows.length : Object.keys(rows).length).toBeGreaterThan(0);
  });

  test('攻击面 1 · 跨品牌资源越权被 fail-closed', async ({ request }) => {
    test.skip(!FOREIGN, '未配置 DEMO_FOREIGN_BRAND_ID');
    const token = await demoToken(request);
    const res = await request.get(
      `${BASE}/api/monitoring/keywords?brand_id=${FOREIGN}`,
      { headers: demoHeaders(token) },
    );
    expect([403, 404]).toContain(res.status());
  });

  test('攻击面 2 · 副作用 GET 被服务端拒绝且零副作用', async ({ request }) => {
    const token = await demoToken(request);
    for (const path of [
      '/api/monitoring/run-stream',
      '/api/monitoring/run',
      '/api/content/deep-analyze',
    ]) {
      const res = await request.get(`${BASE}${path}?brand_id=${BRAND}`, {
        headers: demoHeaders(token),
      });
      expect(res.status(), `${path} 未被拒绝`).toBe(409);
      const detail = (await res.json()).detail;
      expect(detail.code).toBe('DEMO_ACTION_PREVIEW');
      // §13 七字段 + 至少一个出口
      for (const key of ['message', 'reason', 'impact', 'repair_hint', 'actions', 'rule_version']) {
        expect(detail, `${path} 缺 §13 字段 ${key}`).toHaveProperty(key);
      }
      expect(detail.actions.length).toBeGreaterThan(0);
      // 零写入 / 零 provider / 零资金
      expect(detail.saved).toBe(false);
      expect(detail.charged).toBe(false);
      expect(detail.provider_called).toBe(false);
      expect(detail.job_created).toBe(false);
    }
  });

  test('攻击面 2b · 一切写方法被拒绝', async ({ request }) => {
    const token = await demoToken(request);
    const res = await request.post(`${BASE}/api/monitoring/keywords`, {
      headers: demoHeaders(token),
      data: { brand_id: BRAND, keyword: 'e2e-should-never-persist' },
    });
    expect(res.status()).toBe(409);
    expect((await res.json()).detail.saved).toBe(false);
  });

  test('攻击面 3 · 响应不含 §2.4 隐私硬边界字段', async ({ request }) => {
    const token = await demoToken(request);
    const forbidden = [
      'upstream_agent_id', 'parent_agent_name', 'sv_code', 'base_cost',
      'platform_cost', 'purchase_cost', 'cost_multiplier_bps', 'markup_rate',
      'coefficient', 'gross_profit', 'portal_token', 'api_key',
      'contact_phone', 'wechat',
    ];
    for (const path of [
      `/api/monitoring/keywords?brand_id=${BRAND}`,
      `/api/quotes?brand_id=${BRAND}`,
      `/api/diagnosis?brand_id=${BRAND}`,
    ]) {
      const res = await request.get(`${BASE}${path}`, { headers: demoHeaders(token) });
      if (res.status() !== 200) continue;
      const raw = await res.text();
      for (const key of forbidden) {
        expect(raw, `${path} 出站泄漏 ${key}`).not.toContain(`"${key}"`);
      }
    }
  });

  test('门户凭证面继续走冻结快照（不得返回真 token）', async ({ request }) => {
    const token = await demoToken(request);
    const res = await request.get(
      `${BASE}/api/portal/tokens/by-brand/${BRAND}`,
      { headers: demoHeaders(token) },
    );
    if (res.status() === 200) {
      expect(res.headers()['x-demo-data-source']).toBe('frozen_snapshot');
      expect(await res.text()).not.toContain('"portal_token"');
    }
  });
});
