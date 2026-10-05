"""核查 v3.3/v3.4 GEO 托管前后端一致性"""
import re
import importlib.util
from pathlib import Path

ROOT = Path(__file__).parent.parent

print('=== v3.3/v3.4 GEO 托管前后端一致性核查 ===\n')

# 1. 加载后端 API 并提取所有端点
spec = importlib.util.spec_from_file_location('m', ROOT / 'api/managed_campaign_api.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
backend = sorted({r.path for r in m.router.routes} |
                 {r.path for r in m.admin_router.routes})

spec2 = importlib.util.spec_from_file_location('g', ROOT / 'api/geo_assets_api.py')
g = importlib.util.module_from_spec(spec2); spec2.loader.exec_module(g)
backend += [r.path for r in g.router.routes]

print(f'后端端点 ({len(backend)} 个):')
for ep in sorted(set(backend)):
    print(f'  {ep}')

# 2. 提取前端 api.ts 的所有调用
with open(ROOT / 'frontend/src/components/managed/api.ts', encoding='utf-8') as f:
    content = f.read()

frontend_endpoints = set()
# 匹配 ${BASE}/xxx
for m_obj in re.finditer(r'\$\{BASE\}([^`\'",\)]*)', content):
    frontend_endpoints.add('/api/managed' + m_obj.group(1))
# 匹配 `/api/xxx`
for m_obj in re.finditer(r'`(/api/[^`\'",\)]+)', content):
    frontend_endpoints.add(m_obj.group(1))

print(f'\n前端 api.ts 调用 ({len(frontend_endpoints)} 个):')
for ep in sorted(frontend_endpoints):
    print(f'  {ep}')

# 3. 一致性检查（路径参数归一化）
def normalize(p: str) -> str:
    # 去 query string
    p = p.split('?')[0]
    p = re.sub(r'\$\{[^}]+\}', '{x}', p)
    p = re.sub(r'\{[^}]+\}', '{x}', p)
    return p.rstrip('/')

backend_n = {normalize(p) for p in backend}
frontend_n = {normalize(p) for p in frontend_endpoints}

missing_in_backend = []
for fp in sorted(frontend_endpoints):
    if normalize(fp) not in backend_n:
        missing_in_backend.append(fp)

print('\n=== 一致性结论 ===')
if missing_in_backend:
    print(f'[FAIL] 前端调用了但后端未实现 ({len(missing_in_backend)}):')
    for m_ep in missing_in_backend:
        print(f'  {m_ep}')
else:
    print('[OK] 所有前端调用都有后端对应实现')

unused_backend = []
for bp in sorted(set(backend)):
    if normalize(bp) not in frontend_n:
        unused_backend.append(bp)

if unused_backend:
    print(f'\n[WARN] 后端实现了但前端未调用 ({len(unused_backend)})（可能是预留）:')
    for u in unused_backend:
        print(f'  {u}')

# 4. [开源 E3 · B2 · 2026-09-28] 原「AI Agent 工具注册检查」读社媒 agent 源码里的托管工具定义;
#    该 agent 随社媒一起删除,托管工具不再有 agent 侧注册面,本节撤掉(其余各节照旧)。

# 5. server.py 路由注册检查
with open(ROOT / 'server.py', encoding='utf-8') as f:
    server_src = f.read()
managed_routers = re.findall(r'(managed_router|managed_admin_router|geo_assets_router)', server_src)
print(f'\n=== server.py 路由注册 ===')
for r in sorted(set(managed_routers)):
    print(f'  {r}: 已注册')

# 6. scheduler 任务注册检查
with open(ROOT / 'api/scheduler.py', encoding='utf-8') as f:
    scheduler_src = f.read()
scheduler_jobs = re.findall(r"id\s*=\s*['\"](\w*managed\w*|\w*dormancy\w*)['\"]", scheduler_src)
print(f'\n=== scheduler 注册的托管任务 ({len(scheduler_jobs)} 个) ===')
for j in scheduler_jobs:
    print(f'  {j}')

# 7. 前端组件文件清单
managed_components = list((ROOT / 'frontend/src/components/managed').glob('*.tsx'))
managed_pages = (
    list((ROOT / 'frontend/src/pages/Managed').glob('*.tsx')) +
    list((ROOT / 'frontend/src/pages/MaterialCenter').glob('*.tsx')) +
    [ROOT / 'frontend/src/pages/Admin/ManagedCampaignsAdmin.tsx']
)
# [开源 E3 · 前端 · 2026-10-01 · WO_322] 旧对话 UI 里的两张托管方案卡随旧对话 UI 孤儿一起删,清单为空
agent_cards = []

print(f'\n=== 前端文件清单 ===')
print(f'components/managed: {len(managed_components)}')
for f in managed_components:
    print(f'  {f.name}')
print(f'agent 卡片: {len(agent_cards)}')
for f in agent_cards:
    print(f'  {f.name} {"[OK]" if f.exists() else "[MISSING]"}')
print(f'pages: {len(managed_pages)}')
for f in managed_pages:
    print(f'  {f.parent.name}/{f.name} {"[OK]" if f.exists() else "[MISSING]"}')

# 8. SQL 迁移文件检查
sql_files = list((ROOT / 'scripts').glob('migration_v3_3*.sql'))
print(f'\n=== SQL 迁移文件 ({len(sql_files)} 个) ===')
for f in sql_files:
    size = f.stat().st_size
    print(f'  {f.name} ({size} bytes)')

print('\n=== 核查完成 ===')
