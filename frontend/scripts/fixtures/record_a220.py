r"""按**真端点**录制桩,并在真后端上验 PUT 契约的三条语义。

🔴 为什么要录:上一版的桩是我**按契约手写**的 —— 我给 `/api/my-clients/{id}` 也加了
   `writing_basics`,而真端点根本不回它。桩绿、真红,门 30/30 全绿却没发现。
   桩必须来自真回包,不能来自契约文档。

录两份:`/api/profiles/{id}` 与 `/api/my-clients/{brand_id}`,各带 provenance
(后端 sha / 端点 / 取样时刻),门里打印出来当分母自证。
"""
import json
import os
import sys
import urllib.error
import urllib.request

B = 'http://127.0.0.1:55703'
SCRATCH = os.path.dirname(os.path.abspath(__file__))
TOKEN = open(os.path.join(SCRATCH, 'a220_token.txt')).read().strip()
PROFILE_ID = 'a220-profile-1'
BRAND_ID = 990220
BACKEND_SHA = 'c6bdcb5299f3862e08a573cbf74fa5ffad66b96b'


def req(method, path, body=None):
    r = urllib.request.Request(
        B + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + TOKEN})
    try:
        with urllib.request.urlopen(r) as f:
            return f.getcode(), json.loads(f.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode('utf-8', 'replace'))


SIX = {
    'business_summary': '端到端-业务描述',
    'target_customers': '端到端-目标客户',
    'products_services': '端到端-产品服务',
    'key_selling_points': '端到端-核心卖点',
    'proof_cases': '端到端-案例口碑',
    'forbidden_notes': '端到端-禁用表达',
}

fails = []


def check(name, cond, detail=''):
    print(('  OK   ' if cond else '  FAIL ') + name + (' — ' + str(detail) if detail else ''))
    if not cond:
        fails.append(name)


print('E 端到端(真后端 @ %s · 私库 geo_a220_a1_test)' % BACKEND_SHA[:9])

# ① 六个字段全传 ⇒ 全部落库
c, _ = req('PUT', '/api/profiles/' + PROFILE_ID, dict(SIX))
check('E1 PUT 六字段 http=200', c == 200, c)
c, d = req('GET', '/api/profiles/' + PROFILE_ID)
wb = (d.get('profile') or d).get('writing_basics') or {}
check('E2 六字段逐字回读一致', wb == SIX, json.dumps(wb, ensure_ascii=False)[:160])

# ② 只传一个 ⇒ 另外五个**不变**(契约:不传=不覆盖)
c, _ = req('PUT', '/api/profiles/' + PROFILE_ID, {'business_summary': '改过的业务描述'})
c, d = req('GET', '/api/profiles/' + PROFILE_ID)
wb2 = (d.get('profile') or d).get('writing_basics') or {}
untouched = {k: v for k, v in SIX.items() if k != 'business_summary'}
check('E3 只传一个字段 ⇒ 另外五个逐字不变(不传=不覆盖)',
      wb2.get('business_summary') == '改过的业务描述'
      and all(wb2.get(k) == v for k, v in untouched.items()),
      json.dumps(wb2, ensure_ascii=False)[:160])

# ③ 显式空串 ⇒ 清空(而不是被当成"没传")
c, _ = req('PUT', '/api/profiles/' + PROFILE_ID, {'proof_cases': ''})
c, d = req('GET', '/api/profiles/' + PROFILE_ID)
wb3 = (d.get('profile') or d).get('writing_basics') or {}
check('E4 显式空串 ⇒ 该字段清空,其余不动',
      wb3.get('proof_cases') == '' and wb3.get('forbidden_notes') == SIX['forbidden_notes'],
      'proof_cases=%r forbidden_notes=%r' % (wb3.get('proof_cases'), wb3.get('forbidden_notes')))

# 还原成六个都有值,便于录制
req('PUT', '/api/profiles/' + PROFILE_ID, dict(SIX))

# ── 录制 ───────────────────────────────────────────────────────────────
c1, prof = req('GET', '/api/profiles/' + PROFILE_ID)
c2, mycl = req('GET', '/api/my-clients/%d' % BRAND_ID)
rec = {
    'provenance': {
        'backend_sha': BACKEND_SHA,
        'db': 'geo_a220_a1_test(生产 schema dump 2026-08-19 + 该 sha 的 prestart 迁移)',
        'brand_id': BRAND_ID, 'profile_id': PROFILE_ID,
        'note': '按真端点录制,不是按契约手写 —— 手写的桩看不见端点漂移',
    },
    'profiles_get': {'http': c1, 'body': prof},
    'my_clients_get': {'http': c2, 'body': mycl},
}
out = os.path.join(SCRATCH, 'a220_recorded.json')
with open(out, 'w', encoding='utf-8') as f:
    json.dump(rec, f, ensure_ascii=False, indent=1)

mp = (mycl or {}).get('profile') or {}
print('  录制完成 →', out)
print('  /api/profiles 有 writing_basics =', 'writing_basics' in ((prof.get('profile') or prof)))
print('  /api/my-clients 有 writing_basics =', 'writing_basics' in mp, '(今天应为 False,c2\'\' 后翻 True)')

print('\n跑满 4 条' + ('' if not fails else ' · 红:' + ','.join(fails)))
sys.exit(1 if fails else 0)
