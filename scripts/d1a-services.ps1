$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
if ((Get-Location).Path -ne $repo -or (git branch --show-current) -ne 'feature/accounting-v2') { throw 'Wrong repository/branch' }
$names = @('saic-v2-d1a-postgres18','saic-v2-d1a-redis')
$volumes = @('saic-v2-d1a-pg18-data','saic-v2-d1a-redis-data')
$existingNames = @(docker ps -a --format '{{.Names}}')
if ($LASTEXITCODE -ne 0) { throw 'Docker unavailable' }
$existingVolumes = @(docker volume ls --format '{{.Name}}')
if ($LASTEXITCODE -ne 0) { throw 'Docker unavailable' }
foreach ($name in $names) { if ($existingNames -contains $name) { throw "Container collision: $name" } }
foreach ($name in $volumes) { if ($existingVolumes -contains $name) { throw "Volume collision: $name" } }
if (Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in 55434,6382 }) { throw 'Port collision' }
$resource = Join-Path $repo 'runs\d1a'
if (Test-Path -LiteralPath $resource) { throw 'Demo configuration directory already exists; inspect before reuse' }
New-Item -ItemType Directory -Path $resource | Out-Null
function New-PrivateToken {
    $bytes = New-Object byte[] 36
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $rng.Dispose()
    return [Convert]::ToBase64String($bytes).Replace('+','-').Replace('/','_')
}
$config = @{host='127.0.0.1'; port=55434; dbname='accounting_intelligence_v2_demo'; user='d1a_v2_demo'; password=(New-PrivateToken); redis_port=6382; redis_password=(New-PrivateToken); auth_secret=(New-PrivateToken); postgres_container=$names[0]; redis_container=$names[1]; cluster_name='saic_backend_v2_d1a'; created_utc=[DateTime]::UtcNow.ToString('o')}
$config | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $resource 'connection.json') -Encoding UTF8
@("POSTGRES_DB=$($config.dbname)","POSTGRES_USER=$($config.user)","POSTGRES_PASSWORD=$($config.password)", 'POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256') | Set-Content -LiteralPath (Join-Path $resource 'postgres.env') -Encoding ASCII
@('bind 0.0.0.0','port 6379','appendonly yes','appendfsync everysec','maxmemory 32mb','maxmemory-policy noeviction',"requirepass $($config.redis_password)") | Set-Content -LiteralPath (Join-Path $resource 'redis.conf') -Encoding ASCII
foreach ($volume in $volumes) {
    docker volume create --label com.saic.project=backend-v2 --label com.saic.checkpoint=d1a $volume
    if ($LASTEXITCODE -ne 0) { throw 'Volume creation failed' }
}
docker run -d --name $names[0] --label com.saic.project=backend-v2 --label com.saic.checkpoint=d1a --memory 256m --cpus 1 --publish 127.0.0.1:55434:5432 --env-file (Join-Path $resource 'postgres.env') --mount "type=volume,source=$($volumes[0]),target=/var/lib/postgresql" postgres:18 -c shared_buffers=32MB -c max_connections=30 -c work_mem=2MB -c cluster_name=saic_backend_v2_d1a
if ($LASTEXITCODE -ne 0) { throw 'Dedicated PostgreSQL creation failed' }
docker run -d --name $names[1] --label com.saic.project=backend-v2 --label com.saic.checkpoint=d1a --memory 64m --cpus 0.25 --publish 127.0.0.1:6382:6379 --mount "type=volume,source=$($volumes[1]),target=/data" --mount "type=bind,source=$(Join-Path $resource 'redis.conf'),target=/usr/local/etc/redis/redis.conf,readonly" redis:7.2-alpine redis-server /usr/local/etc/redis/redis.conf
if ($LASTEXITCODE -ne 0) { throw 'Dedicated Redis creation failed' }
# Official image bootstraps a fresh cluster only. No application schema is executed.
Write-Output 'Dedicated V2 demo containers created; application schema initialization has NOT run.'
