import { expect, test, type Page, type Route } from 'playwright/test';


const agentUser = {
  id: 7007,
  user_id: 7007,
  username: 'closed-loop-agent',
  display_name: '服务商运营',
  is_admin: true,
  is_active: 1,
  must_change_password: 0,
  agent_level: 2,
  roles: [{ id: 1, name: 'admin', display_name: '管理员' }],
  permissions: ['writing:read', 'writing:write', 'brands:read'],
  client_brand_ids: [501, 502],
};


async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}


const repairCalls: Array<Record<string, unknown>> = [];
const autoRepairCalls: Array<Record<string, unknown>> = [];

async function installWritingSession(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'local-closed-loop-probe');
    localStorage.setItem('omnirank_m3_onboarded_at', 'local-ui-probe');
  });
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === '/api/auth/me') return json(route, { success: true, user: agentUser });
    if (path === '/api/wallet') {
      return json(route, { success: true, data: { paid_points: 100000, total: 100000, frozen_points: 0 } });
    }
    if (path === '/api/writing/projects') {
      return json(route, {
        projects: [
          {
            id: 10,
            quote_ids: [10],
            brand_id: 501,
            brand_name: '岱林生物',
            industry: '生物制药设备',
            keyword_count: 1,
            total_required_articles: 6,
            monthly_price: 0,
            writing_status: 'pending',
            confirmed_at: '2026-07-20T00:00:00Z',
          },
          {
            id: 11,
            quote_ids: [11],
            brand_id: 502,
            brand_name: '星河医疗',
            industry: '医疗器械',
            keyword_count: 1,
            total_required_articles: 3,
            monthly_price: 0,
            writing_status: 'pending',
            confirmed_at: '2026-07-20T00:00:00Z',
          },
        ],
      });
    }
    if (path === '/api/writing/projects/10') {
      return json(route, {
        quote: { id: 10, brand_id: 501, brand_name: '岱林生物', industry: '生物制药设备' },
        keywords: [{
          id: 21,
          keyword: '细胞治疗药物研发和生产隔离器推荐',
          required_articles: 6,
          recommended_platforms: null,
          final_price: 0,
        }],
        topics: [{
          id: 31,
          keyword_id: 21,
          original_keyword: '细胞治疗药物研发和生产隔离器推荐',
          optimized_title: '细胞治疗隔离器采购核验指南',
          article_style: 'buying_guide',
          style_code: 'buying_guide',
          style_family: 'implementation_guide',
          status: 'completed',
          article_id: 99,
          reviewed_at: '2026-07-20T00:00:00Z',
          publication_eligible: false,
          publication_eligibility_message: '待审核',
          // [WP9-P0-7 · D8] 文章层零阻断:违法绝对化不拒存,降为定位标注 + 一键修复/忽略
          quality_warning: {
            needs_legal_fix: true,
            evidence_legal: {
              hard: [{
                code: 'absolute_first_claim',
                severity: 'hard',
                message: '正文把品牌写成第一(《广告法》第九条绝对化用语)。',
                evidence: '我们是行业第一的隔离器供应商',
                matched_text: '行业第一',
              }],
            },
          },
        }, {
          id: 32,
          keyword_id: 21,
          original_keyword: '细胞治疗药物研发和生产隔离器推荐',
          optimized_title: '细胞治疗隔离器证据核验清单',
          article_style: 'buying_guide',
          style_code: 'buying_guide',
          style_family: 'implementation_guide',
          status: 'failed',
          generation_request_id: 'writing-articles-refunded-32',
          generation_error_code: 'ARTICLE_PROVIDER_UNAVAILABLE',
          generation_error_message: '写作模型暂时不可用，本次未交付内容，可稍后安全重试。',
          generation_retryable: true,
          generation_failure_phase: 'provider',
          generation_refund_status: 'refunded',
          generation_refund_message: '退款已完成',
        }, {
          id: 33,
          keyword_id: 21,
          original_keyword: '细胞治疗药物研发和生产隔离器推荐',
          optimized_title: '隔离器生产风险核验清单',
          article_style: 'risk_compliance',
          style_code: 'risk_compliance',
          style_family: 'risk_compliance',
          status: 'failed',
          generation_request_id: 'writing-articles-recovery-33',
          generation_error_code: 'ARTICLE_SAVE_FAILED',
          generation_error_message: '正文已生成但未能安全保存，本次没有覆盖原稿，可稍后重试。',
          generation_retryable: true,
          generation_failure_phase: 'save',
          generation_refund_status: 'pending_recovery',
          generation_refund_message: '退款处理中，请勿重复提交',
        }],
      });
    }
    if (path === '/api/writing/projects/11') {
      return json(route, {
        quote: { id: 11, brand_id: 502, brand_name: '星河医疗', industry: '医疗器械' },
        keywords: [{
          id: 22,
          keyword: '医疗器械采购指南',
          required_articles: 3,
          recommended_platforms: null,
          final_price: 0,
        }],
        topics: [],
      });
    }
    if (path === '/api/writing/closed-loop/projects/10/summary') {
      return json(route, {
        enabled: true,
        simple_ui_enabled: true,
        available: true,
        project_status: 'pending',
        delivery: { contract_total: 6, due: 6, completed: 2, pending: 4, blocked: 1, published: 1 },
        health: {
          status: 'available',
          articles_with_evidence_record: 2,
          articles_needing_review: 1,
          waiting_for_information: 1,
          message: '健康信息来自已保存的证据和审核事实。',
        },
        blockers: [{
          message: '请补充产品资质材料',
          owner: 'agent',
          next_action: '上传资质文件',
          target_time: '2026-08-01T00:00:00Z',
        }],
        next_action: { label: '处理 1 个问题', kind: 'resolve_blockers' },
      });
    }
    if (path === '/api/articles/99') {
      return json(route, {
        success: true,
        article: {
          id: 99,
          title: '细胞治疗隔离器采购核验指南',
          content: '# 细胞治疗隔离器采购核验指南\n\n我们是行业第一的隔离器供应商。\n\n先核对工艺和证据边界。',
          length_guidance: {
            summary: '资料较少，优先写紧凑可信版本',
            detail: '宁可少写，也不重复内容或补写未经核验的事实。',
            depth: 'compact',
            evidence_limited: true,
            actual_chars: 4500,
            minimum_chars: 3000,
            target_chars: 4500,
            maximum_chars: 8000,
          },
        },
      });
    }
    if (path === '/api/brand-images/article-preview/99') {
      return json(route, {
        success: true,
        content: '# 细胞治疗隔离器采购核验指南\n\n先核对工艺和证据边界。',
        images: [],
        brand_id: 501,
        contact_consent: 'disabled',
        length_guidance: {
          summary: '资料较少，优先写紧凑可信版本',
          detail: '宁可少写，也不重复内容或补写未经核验的事实。',
          depth: 'compact',
          evidence_limited: true,
          actual_chars: 4500,
          minimum_chars: 3000,
          target_chars: 4500,
          maximum_chars: 8000,
        },
      });
    }
    if (path === '/api/topics/31/mark-reviewed') return json(route, { success: true });
    // [WP9-P0-7 ②] span 级"AI 仅修此处"(不重跑整篇、不二次计费)
    // [工单 C-4] 一键修复(汇总卡入口·服务端批量一轮·轮数硬闸)
    if (path === '/api/articles/99/auto-repair') {
      autoRepairCalls.push((route.request().postDataJSON() ?? {}) as Record<string, unknown>);
      return json(route, {
        success: true, article_id: 99, rounds_used: 1, rounds_left: 1,
        repaired_count: 0, remaining: [{ code: 'absolute_first_claim', matched_text: '行业第一' }],
        article_review_status: 'blocked',
      });
    }
    if (path === '/api/articles/99/repair-finding') {
      repairCalls.push(route.request().postDataJSON() as Record<string, unknown>);
      return json(route, {
        success: true, article_id: 99, repaired: true, needs_legal_fix: false,
        paragraph_before: '我们是行业第一的隔离器供应商。',
        paragraph_after: '我们在公开口径下表现较好。',
      });
    }
    if (path === '/api/writing/projects/10/knowledge-status') {
      return json(route, { success: true, quote_id: 10, brand_id: 501, status: 'ready', materials_summary: {} });
    }
    if (path === '/api/writing/projects/11/knowledge-status') {
      return json(route, { success: true, quote_id: 11, brand_id: 502, status: 'ready', materials_summary: {} });
    }
    if (path === '/api/writing/projects/10/structure-guidance') {
      return json(route, { success: true, can_apply: false, default_enabled: false });
    }
    if (path === '/api/writing/projects/11/structure-guidance') {
      return json(route, { success: true, can_apply: false, default_enabled: false });
    }
    if (path === '/api/writing/competitors/10') return json(route, { competitors: [], mode: 'evidence_only' });
    if (path === '/api/writing/competitors/11') return json(route, { competitors: [], mode: 'evidence_only' });
    if (path === '/api/knowledge/client/501') return json(route, { success: true, documents: [] });
    if (path === '/api/knowledge/client/502') return json(route, { success: true, documents: [] });
    if (path === '/api/brand-images/list/501') return json(route, { success: true, assets: [] });
    if (path === '/api/brand-images/list/502') return json(route, { success: true, assets: [] });
    if (path === '/api/my-clients/501') {
      return json(route, { success: true, brand: { id: 501, name: '岱林生物' }, profile: {} });
    }
    if (path === '/api/my-clients/502') {
      return json(route, { success: true, brand: { id: 502, name: '星河医疗' }, profile: {} });
    }
    return json(route, { success: true, data: {}, items: [], total: 0 });
  });
}


async function openCompletedArticle(page: Page) {
  await page.goto('/writing?quote_id=10');
  await page.getByRole('tab', { name: '已完成 (1)' }).click();
  const preview = page.getByTitle('预览');
  if (!(await preview.isVisible())) {
    await page.getByText('细胞治疗药物研发和生产隔离器推荐', { exact: true }).click();
  }
  await page.getByTitle('预览').click();
  await expect(page.getByRole('dialog')).toBeVisible();
}


test('普通模式只给人话摘要、唯一下一步且无横向溢出', async ({ page }, testInfo) => {
  await installWritingSession(page);
  await page.goto('/writing?quote_id=10');
  const summary = page.getByRole('region', { name: '项目交付摘要' });
  await expect(summary).toBeVisible();
  await expect(summary.getByText('合同交付')).toBeVisible();
  await expect(summary.getByText('请补充产品资质材料')).toBeVisible();
  await expect(summary.getByRole('button', { name: '处理 1 个问题' })).toBeVisible();
  await expect(page.getByRole('button', { name: '需要时打开高级设置' })).toBeVisible();
  await expect(page.getByRole('button', { name: /刷新缓存/ })).toBeHidden();
  const snapshot = await page.evaluate(() => ({
    text: document.body.innerText,
    overflow: document.documentElement.scrollWidth - window.innerWidth,
  }));
  expect(snapshot.overflow).toBeLessThanOrEqual(1);
  for (const internal of ['delivery_slot_key', 'Evidence policy', 'style version', 'experiment arm']) {
    expect(snapshot.text).not.toContain(internal);
  }
  await page.screenshot({ path: testInfo.outputPath('writing-simple.png'), fullPage: true });
});


test('高级设置由用户主动展开，摘要与现役动作同时保留', async ({ page }, testInfo) => {
  await installWritingSession(page);
  await page.goto('/writing?quote_id=10');
  await expect(page.getByRole('region', { name: '项目交付摘要' })).toBeVisible();
  await page.getByRole('button', { name: '需要时打开高级设置' }).click();
  await expect(page.getByRole('button', { name: '收起高级设置' })).toBeVisible();
  await expect(page.getByRole('button', { name: /刷新缓存/ })).toBeVisible();
  await expect(page.getByRole('region', { name: '项目交付摘要' })).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow).toBeLessThanOrEqual(1);
  await page.screenshot({ path: testInfo.outputPath('writing-advanced.png'), fullPage: true });
});


test('快速切换项目时迟到摘要不能覆盖当前客户', async ({ page }) => {
  await installWritingSession(page);
  await page.route('**/api/writing/closed-loop/projects/10/summary', async (route) => {
    await new Promise(resolve => setTimeout(resolve, 700));
    return json(route, {
      enabled: true,
      simple_ui_enabled: true,
      available: true,
      delivery: { contract_total: 99, completed: 98, pending: 1, blocked: 0, published: 98 },
      health: {
        status: 'available',
        articles_with_evidence_record: 98,
        articles_needing_review: 0,
        waiting_for_information: 0,
        message: '旧项目迟到摘要',
      },
      blockers: [],
      next_action: { label: '旧项目动作', kind: 'generate_titles' },
    });
  });
  await page.route('**/api/writing/closed-loop/projects/11/summary', async (route) => {
    await new Promise(resolve => setTimeout(resolve, 40));
    return json(route, {
      enabled: true,
      simple_ui_enabled: true,
      available: true,
      delivery: { contract_total: 3, completed: 1, pending: 2, blocked: 0, published: 1 },
      health: {
        status: 'available',
        articles_with_evidence_record: 1,
        articles_needing_review: 0,
        waiting_for_information: 0,
        message: '当前项目摘要',
      },
      blockers: [],
      next_action: { label: '生成当前项目标题', kind: 'generate_titles' },
    });
  });

  await page.goto('/writing');
  await page.getByText('岱林生物', { exact: true }).click();
  await page.getByRole('button', { name: /^返回/ }).click();
  await page.getByText('星河医疗', { exact: true }).click();

  const summary = page.getByRole('region', { name: '项目交付摘要' });
  await expect(summary.getByText('生成当前项目标题')).toBeVisible();
  await page.waitForTimeout(900);
  await expect(page.getByRole('heading', { name: '星河医疗' })).toBeVisible();
  await expect(summary.getByText('生成当前项目标题')).toBeVisible();
  await expect(summary.getByText('99')).toHaveCount(0);
  await expect(summary.getByText('旧项目动作')).toHaveCount(0);
});


test('普通预览只显示服务端给出的人话篇幅提示', async ({ page }) => {
  await installWritingSession(page);
  await openCompletedArticle(page);
  const guidance = page.getByTestId('article-length-guidance');
  await expect(guidance.getByText('资料较少，优先写紧凑可信版本')).toBeVisible();
  await expect(guidance.getByText('12K')).toHaveCount(0);
  await expect(page.getByTestId('article-length-guidance-advanced')).toHaveCount(0);
});


test('用户主动打开高级设置后才显示服务端建议区间', async ({ page }) => {
  await installWritingSession(page);
  await page.goto('/writing?quote_id=10');
  await page.getByRole('button', { name: '需要时打开高级设置' }).click();
  await page.getByRole('tab', { name: '已完成 (1)' }).click();
  const preview = page.getByTitle('预览');
  if (!(await preview.isVisible())) {
    await page.getByText('细胞治疗药物研发和生产隔离器推荐', { exact: true }).click();
  }
  await page.getByTitle('预览').click();
  await expect(page.getByTestId('article-length-guidance-advanced')).toContainText('建议有效正文 3,000–8,000 字');
});


test('失败卡展示稳定原因和退款状态，只有已退款任务允许安全重试', async ({ page }) => {
  await installWritingSession(page);
  await page.goto('/writing?quote_id=10');
  await expect(page.getByRole('tab', { name: '待写 (2)' })).toBeVisible();
  await page.getByRole('button', { name: '全部展开' }).click();

  await expect(page.getByText(/ARTICLE_PROVIDER_UNAVAILABLE.*退款已完成/)).toBeVisible();
  await expect(page.getByText(/ARTICLE_SAVE_FAILED.*退款处理中，请勿重复提交/)).toBeVisible();
  await expect(page.getByRole('button', { name: '安全重试' })).toHaveCount(1);
  await expect(page.getByText(/Traceback|at .*\.py:\d+|psycopg/i)).toHaveCount(0);
});


test('[工单 C-4] hard 只出一张汇总卡:一键修复走 auto-repair;详情弹窗定位高亮 + 单处修复走段级链', async ({ page }) => {
  repairCalls.length = 0;
  autoRepairCalls.length = 0;
  await installWritingSession(page);
  await page.goto('/writing?quote_id=10');
  // 完成稿在「已完成」页签(零阻断:违法绝对化没有导致拒存/丢稿)
  await page.getByRole('tab', { name: /已完成/ }).click();
  await page.getByText('细胞治疗药物研发和生产隔离器推荐', { exact: true }).first().click();

  // [C-4] 不再平铺逐条卡:hard 入口只有一张汇总卡
  const summary = page.locator('[data-testid="hard-summary-card"]').first();
  await expect(summary).toBeVisible();
  await expect(summary).toContainText('本篇有 1 处需要处理');
  await expect(page.locator('[data-governance-code="ABSOLUTE_FIRST_CLAIM"]')).toHaveCount(0);

  // 一键修复 = 服务端批量一轮(auto-repair 端点,不是逐条 repair-finding)
  await summary.locator('[data-testid="auto-repair-btn"]').click();
  await expect.poll(() => autoRepairCalls.length).toBeGreaterThan(0);

  // 详情弹窗:逐项定位高亮(Owner 点名交互)
  await summary.locator('[data-testid="view-findings-btn"]').click();
  const dialog = page.locator('[data-testid="hard-detail-dialog"]');
  await expect(dialog).toBeVisible();
  await expect(dialog.locator('[data-testid="hard-detail-item"]')).toHaveCount(1);
  await expect(dialog.locator('[data-testid="hard-detail-body"]')).toContainText('行业第一');
  await dialog.locator('[data-testid="locate-finding-btn"]').first().click();
  const highlight = dialog.locator('mark[data-testid="finding-highlight"]');
  await expect(highlight).toBeVisible();
  await expect(highlight).toContainText(/行业第一|我们是/);

  // 单处修复仍走段级链(带定位锚,不重跑整篇)
  await dialog.getByRole('button', { name: 'AI 修复', exact: true }).first().click();
  await expect.poll(() => repairCalls.length).toBeGreaterThan(0);
  expect(repairCalls[0].finding_code).toBe('absolute_first_claim');
  expect(repairCalls[0].matched_text).toBe('行业第一');
});
