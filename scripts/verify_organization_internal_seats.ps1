$ErrorActionPreference = 'Stop'

$containerName = 'codex-org-seats-verification-pg'
$databasePort = 55721
$existing = docker ps -a --filter "name=^/${containerName}$" --format '{{.ID}}'
if ($existing) {
    throw "Refusing to reuse existing container $containerName. Remove it manually after confirming ownership."
}

$containerId = $null
try {
    $containerId = docker run --rm -d `
        --name $containerName `
        --tmpfs '/var/lib/postgresql/data:rw,noexec,nosuid,size=1g' `
        -p "127.0.0.1:${databasePort}:5432" `
        -e POSTGRES_USER=orgtest `
        -e POSTGRES_PASSWORD=orgseats_local_only `
        -e POSTGRES_DB=orgtest `
        postgres:16
    if (-not $containerId) {
        throw 'Throwaway PostgreSQL 16 container did not start.'
    }

    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        docker exec $containerName pg_isready -U orgtest -d orgtest *> $null
        if ($LASTEXITCODE -eq 0) {
            $ready = $true
            break
        }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) {
        throw 'Throwaway PostgreSQL 16 did not become ready within 30 seconds.'
    }

    python tests/organization_internal_seats/verify_local.py `
        --dsn "postgresql://orgtest:orgseats_local_only@127.0.0.1:${databasePort}/orgtest"
    if ($LASTEXITCODE -ne 0) {
        throw "Organization verification failed with exit code $LASTEXITCODE."
    }
}
finally {
    if ($containerId) {
        docker stop $containerName *> $null
    }
}
