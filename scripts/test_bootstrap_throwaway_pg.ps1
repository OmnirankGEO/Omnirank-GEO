# [v5 req5] Windows PowerShell 版 throwaway PG bootstrap · 一键起测试库并跑 NO-GO 回归。
# schema 由 tests/regression/conftest.py 的 session fixture 幂等填齐(users 必填列 + 当前 schema)。
# 与 test_bootstrap_throwaway_pg.sh 等价 · 供 Windows 直接执行:
#   pwsh -File scripts/test_bootstrap_throwaway_pg.ps1
#
# 安全:TEST_DATABASE_URL 指向本机 127.0.0.1 + 库名含 test/throwaway(_dbsafe.py 会二次校验)。
$ErrorActionPreference = "Stop"

$Name = if ($env:PG_NAME) { $env:PG_NAME } else { "geofix2-test-pg" }
$Port = if ($env:PG_PORT) { $env:PG_PORT } else { "5434" }
$DbUrl = "postgresql://geo_admin:test@127.0.0.1:$Port/test_geo_agentscope"

Write-Host "=== (re)start throwaway PG :$Port ==="
docker rm -f $Name 2>$null | Out-Null
docker run -d --name $Name -e POSTGRES_PASSWORD=test -e POSTGRES_USER=geo_admin `
  -e POSTGRES_DB=test_geo_agentscope -p "${Port}:5432" postgres:16 | Out-Null

$ready = $false
foreach ($i in 1..30) {
    docker exec $Name pg_isready -U geo_admin -d test_geo_agentscope 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    Start-Sleep -Seconds 1
}
if (-not $ready) { throw "throwaway PG 未就绪(30s 超时)" }
Write-Host "pg ready"

$env:TEST_DATABASE_URL = $DbUrl
$env:DATABASE_URL = $DbUrl
# [v5 req5] 破坏性测试(清表/DROP throwaway 库)显式授权 · 仅本机 test/throwaway 库
$env:ALLOW_DESTRUCTIVE_TEST_DB = "1"

Write-Host "=== run NO-GO regression (fixture auto-provisions schema) ==="
# [v8 · Deploy-CTO NO-GO] 不再只跑精选:一并跑现役 GEO task/worker 测试(断言随 v5-v8 状态机演进必须同步)
python -m pytest tests/regression tests/security tests/test_geo_plan_tasks_db.py -q
# [v9 · Deploy-CTO NO-GO P2-5] 每批 pytest 后【立即】记录退出码:原脚本只 `exit $LASTEXITCODE`
#   返回第二批(pricing)结果,第一批(GEO)失败会被第二批通过覆盖成假绿。现逐批捕获后汇总退出。
$geoExit = $LASTEXITCODE
if ($geoExit -ne 0) { Write-Host "!!! GEO 批失败 exit=$geoExit" -ForegroundColor Red }

# [v8 reconciliation] 双价目表 SSOT 测试独立 session 跑(其 conftest DROP 重建共享表)
python -m pytest tests/pricing_ssot -q
$pricingExit = $LASTEXITCODE
if ($pricingExit -ne 0) { Write-Host "!!! 双价目表批失败 exit=$pricingExit" -ForegroundColor Red }

# 任一批失败即整体失败(GEO 优先返回,避免 pricing 通过掩盖 GEO 失败)
if ($geoExit -ne 0) { exit $geoExit }
exit $pricingExit
