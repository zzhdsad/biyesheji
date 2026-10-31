#Requires -Version 5.1
<#
空库初始化：Dry Run（默认）/ 执行清理 / 从备份恢复。

安全约束（脚本强制）：
  1. 默认 Dry Run，必须 -Execute 才改动数据；
  2. 清理前校验数据库 / 后端连接库 / Milvus 集合 / 上传目录同属一个已登记环境，
     任一不一致或无法确认即拒绝（不再只看数据库名称）；
  3. 目标库为主库时必须 -AllowMain，否则退出；
  4. 执行前必须有可用备份（无备份则先备份，备份失败即中止）；
  5. 清理后逐表残留校验，不为 0 就报警并给出恢复命令。

用法：
  .\reset-database.ps1                       # Dry Run
  .\reset-database.ps1 -Execute              # 备份后清理并校验
  .\reset-database.ps1 -Restore .\backups\x.dump
#>
param(
    [string]$TargetDb = "kp_dev",
    [switch]$Execute,
    [string]$Restore,
    [switch]$AllowMain,
    [string]$BackupDir = ".\backups"
)

# 不用 Stop：docker/psql 的原生 stderr 会被 PowerShell 当成终止性错误，
# 导致"某张表不存在"这类可容忍情况直接中断整个脚本。
$ErrorActionPreference = "Continue"
$PGC = "kp-postgres"; $PGU = "postgres"; $MAIN = "knowledge_platform"; $BE = "kp-backend"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$UploadDir = "/app/uploads"

# 保留：迁移版本 / 模型与系统配置 / 用户（首次部署需能登录）
$KeepTables = @("alembic_version", "model_configs", "system_configs", "users")

function Q([string]$Sql, [string]$Db = $TargetDb) {
    $o = docker exec $PGC psql -U $PGU -d $Db -t -A -c $Sql 2>$null
    return ($o | Out-String).Trim()
}
function ListTables {
    # 动态发现业务表，避免硬编码清单里出现库里不存在的表名（实测踩到）
    $o = docker exec $PGC psql -U $PGU -d $TargetDb -t -A -c "SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY 1;" 2>$null
    return ($o -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}
function Count([string]$T) { $r = Q "SELECT count(*) FROM $T;"; if ($r -match '^\d+$') { return [int]$r }; return -1 }
function MilvusCount([string]$Collection) {
    # 用脚本文件而非 -c 内联：多行 Python 经 docker exec 传参容易被引号/换行破坏
    docker cp (Join-Path $ScriptDir "scripts\milvus_count.py") ("{0}:/tmp/milvus_count.py" -f $BE) | Out-Null
    $o = docker exec $BE python /tmp/milvus_count.py $Collection 2>$null | Select-Object -Last 1
    if ($o -match '^-?\d+$') { return [int]$o }; return -1
}
function UploadCount {
    $o = docker exec $BE sh -c "find $UploadDir -type f 2>/dev/null | wc -l" 2>&1
    if ($o -match '^\d+$') { return [int]$o }; return -1
}

function ContainerEnvValue([string]$Name) {
    $o = docker exec $BE printenv $Name 2>$null
    return ($o | Out-String).Trim()
}
function BackendDbName {
    $url = ContainerEnvValue "DATABASE_URL"
    if ($url -match '/([^/?]+)(\?|$)') { return $Matches[1] }
    return ""
}
function UploadMountSource {
    # 取容器内 /app/uploads 实际绑定的宿主机目录；未挂载则返回空，由守卫拒绝
    $o = docker inspect $BE --format '{{range .Mounts}}{{.Destination}}={{.Source}}{{println}}{{end}}' 2>$null
    foreach ($line in ($o -split "`n")) {
        $line = $line.Trim()
        if ($line.StartsWith("$UploadDir=")) { return $line.Substring($UploadDir.Length + 1) }
    }
    return ""
}
function CheckEnvironment {
    # 数据库、后端连接库、Milvus 集合、上传目录必须同属一个已登记环境
    $facts = [ordered]@{
        db                   = $TargetDb
        backend_db           = (BackendDbName)
        milvus_collection    = (ContainerEnvValue "MILVUS_COLLECTION")
        upload_source        = (UploadMountSource)
        upload_container_dir = $UploadDir
        allow_main           = [bool]$AllowMain
    } | ConvertTo-Json -Compress
    $b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($facts))
    $guardPath = Join-Path $ScriptDir "backend\src\infrastructure\env_guard.py"
    if (-not (Test-Path $guardPath)) {
        return [pscustomobject]@{ allowed = $false; reason = "缺少环境校验模块 env_guard.py"; profile = $null; environment = $null }
    }
    docker cp $guardPath ("{0}:/tmp/env_guard.py" -f $BE) 2>$null | Out-Null
    $out = docker exec $BE python /tmp/env_guard.py --facts-b64 $b64 2>$null | Select-Object -Last 1
    if (-not $out) {
        return [pscustomobject]@{ allowed = $false; reason = "无法执行环境校验（后端容器不可用？）"; profile = $null; environment = $null }
    }
    try {
        return $out | ConvertFrom-Json
    } catch {
        return [pscustomobject]@{ allowed = $false; reason = "环境校验输出无法解析"; profile = $null; environment = $null }
    }
}

Write-Host ""
Write-Host "==== 空库初始化 ====" -ForegroundColor Yellow
Write-Host ("目标库: {0}   模式: {1}" -f $TargetDb, $(if ($Restore) { "恢复" } elseif ($Execute) { "执行清理" } else { "Dry Run" }))

# 闸门 0：数据库 / 后端连接库 / Milvus 集合 / 上传目录必须同属一个已登记环境
$envCheck = CheckEnvironment
if ($envCheck.allowed) {
    Write-Host ("[通过] {0}（上传目录: {1}）" -f $envCheck.reason, $envCheck.environment.upload_source) -ForegroundColor Green
} else {
    Write-Host ("[拒绝] {0}" -f $envCheck.reason) -ForegroundColor Red
    Write-Host "        无法确认目标归属或存在共享风险时，脚本不执行任何清理。" -ForegroundColor Red
    exit 2
}

# 闸门 1：禁止误操作主库
if ($TargetDb -eq $MAIN -and -not $AllowMain) {
    Write-Host "[拒绝] 目标是主库，需显式 -AllowMain 才允许（不可逆操作）。" -ForegroundColor Red
    exit 2
}
if ((Q "SELECT 1 FROM pg_database WHERE datname='$TargetDb';" "postgres") -ne "1") {
    Write-Host "[拒绝] 目标库不存在，请先创建并执行 alembic upgrade head。" -ForegroundColor Red
    exit 2
}

# 恢复模式
if ($Restore) {
    if (-not (Test-Path $Restore)) { Write-Host "[拒绝] 备份文件不存在" -ForegroundColor Red; exit 2 }
    Write-Host "== 恢复 ==" -ForegroundColor Yellow
    Q "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$TargetDb' AND pid <> pg_backend_pid();" "postgres" | Out-Null
    Q "DROP DATABASE IF EXISTS $TargetDb;" "postgres" | Out-Null
    Q "CREATE DATABASE $TargetDb;" "postgres" | Out-Null
    $cp = "/tmp/restore.dump"
    docker cp $Restore ("{0}:{1}" -f $PGC, $cp) | Out-Null
    docker exec $PGC pg_restore -U $PGU -d $TargetDb --no-owner --no-privileges $cp 2>&1 | Select-Object -Last 2
    docker exec $PGC rm -f $cp | Out-Null
    Write-Host "恢复后行数：" -ForegroundColor Cyan
    foreach ($t in $CleanTables) { $n = Count $t; if ($n -gt 0) { Write-Host ("  {0,-28} {1}" -f $t, $n) } }
    Write-Host "请重启后端并执行 alembic upgrade head 对齐版本。" -ForegroundColor Green
    exit 0
}

# 现状盘点（只读）
Write-Host "== 现状（只读）==" -ForegroundColor Yellow
$CleanTables = ListTables | Where-Object { $KeepTables -notcontains $_ }
$snap = @{}
foreach ($t in $CleanTables) { $n = Count $t; if ($n -gt 0) { $snap[$t] = $n } }
foreach ($t in $CleanTables) { if ($snap.ContainsKey($t)) { Write-Host ("  {0,-28} {1}" -f $t, $snap[$t]) } }
Write-Host ("  {0,-28} {1}" -f "Milvus 向量", (MilvusCount $envCheck.environment.milvus_collection))
Write-Host ("  {0,-28} {1}" -f "上传文件", (UploadCount))
Write-Host ("  保留不清理: {0}" -f ($KeepTables -join ", "))

Write-Host "== 备份检查 ==" -ForegroundColor Yellow
$latest = $null
if (Test-Path $BackupDir) {
    $latest = Get-ChildItem $BackupDir -Filter "*.dump" -File | Sort-Object LastWriteTime -Descending | Select-Object -First 1
}
if ($latest) {
    $age = (Get-Date) - $latest.LastWriteTime
    Write-Host ("  最新备份: {0}  {1:N2} MB  {2:N1} 小时前" -f $latest.Name, ($latest.Length / 1MB), $age.TotalHours)
} else {
    Write-Host "  未找到备份（*.dump）" -ForegroundColor Yellow
    if (-not $Execute) { Write-Host "  [阻断] 无备份时不允许清理。" -ForegroundColor Red }
}

if (-not $Execute) {
    Write-Host "== Dry Run 计划（未执行）==" -ForegroundColor Yellow
    Write-Host "  1) 备份 $TargetDb"
    foreach ($t in $CleanTables) { if ($snap.ContainsKey($t)) { Write-Host "  2) TRUNCATE $t ($($snap[$t]) 行)" } }
    Write-Host "  3) 清空 Milvus 集合 $($envCheck.environment.milvus_collection) 的全部向量"
    Write-Host "  4) 清空上传目录 $($envCheck.environment.upload_source)"
    Write-Host "  5) 残留校验（表=0 / 向量=0 / 文件=0 / 管理员存在）"
    Write-Host ""
    Write-Host ("确认后执行: .\reset-database.ps1 -Execute -TargetDb {0}" -f $TargetDb) -ForegroundColor Green
    exit 0
}

# 执行
Write-Host "== 执行清理 ==" -ForegroundColor Yellow
if (-not (Test-Path $BackupDir)) { New-Item -ItemType Directory $BackupDir | Out-Null }
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$file = Join-Path $BackupDir "kp_${TargetDb}_$stamp.dump"
$cd = "/tmp/kp_$stamp.dump"
docker exec $PGC pg_dump -U $PGU -Fc -d $TargetDb -f $cd | Out-Null
if ($LASTEXITCODE -ne 0) { throw "备份失败，已中止，未改动任何数据" }
docker cp ("{0}:{1}" -f $PGC, $cd) $file | Out-Null
docker exec $PGC rm -f $cd | Out-Null
if ((Get-Item $file).Length -lt 1KB) { throw "备份文件异常（<1KB），已中止" }
Write-Host ("  备份完成 {0} ({1:N2} MB)" -f (Split-Path $file -Leaf), ((Get-Item $file).Length / 1MB)) -ForegroundColor Green

# 清理前复检：备份期间容器配置若被切换（如 dev/prod 环境互换），必须中止
$recheck = CheckEnvironment
if (-not $recheck.allowed) { throw ("清理前环境复检未通过，已中止：{0}" -f $recheck.reason) }

foreach ($t in $CleanTables) { if ($snap.ContainsKey($t)) { Q "TRUNCATE TABLE $t RESTART IDENTITY CASCADE;" | Out-Null } }
Write-Host "  PostgreSQL 业务表已清空"

docker cp (Join-Path $ScriptDir "scripts\milvus_clear.py") ("{0}:/tmp/milvus_clear.py" -f $BE) | Out-Null
docker exec $BE python /tmp/milvus_clear.py $($envCheck.environment.milvus_collection) 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host "  [警告] Milvus 清理脚本返回非 0，请查看残留校验结果" -ForegroundColor Yellow }
Write-Host "  Milvus 向量已清空"
docker exec $BE sh -c "rm -rf $UploadDir/* 2>/dev/null" | Out-Null
if ($LASTEXITCODE -ne 0) { Write-Host "  [警告] 上传目录清理命令返回非 0，请查看残留校验结果" -ForegroundColor Yellow }
Write-Host "  上传目录已清空"

# 残留校验
Write-Host "== 残留校验 ==" -ForegroundColor Yellow
$bad = @()
foreach ($t in $CleanTables) { $n = Count $t; if ($n -gt 0) { $bad += "$t=$n"; Write-Host ("  [残留] {0,-28} {1}" -f $t, $n) -ForegroundColor Red } }
$mv = MilvusCount $envCheck.environment.milvus_collection; $up = UploadCount
Write-Host ("  {0,-28} {1}" -f "Milvus 向量", $mv)
Write-Host ("  {0,-28} {1}" -f "上传文件", $up)
if ($mv -ne 0) { $bad += "milvus=$mv" }
if ($up -gt 0) { $bad += "uploads=$up" }
$admin = Q "SELECT count(*) FROM users WHERE role='admin';"
Write-Host ("  {0,-28} {1}" -f "管理员账号", $admin)
if ($admin -notmatch '^[1-9]') { Write-Host "  [警告] 无管理员账号，需重启后端触发 init_db 种子" -ForegroundColor Yellow }

Write-Host ""
if ($bad.Count -eq 0) { Write-Host "清理完成，无残留。" -ForegroundColor Green }
else { Write-Host ("存在残留: {0}" -f ($bad -join ", ")) -ForegroundColor Red }
Write-Host "== 恢复方案 ==" -ForegroundColor Yellow
Write-Host ("  .\reset-database.ps1 -Restore '{0}' -TargetDb {1}" -f (Resolve-Path $file), $TargetDb)
if ($bad.Count -gt 0) { exit 1 }
