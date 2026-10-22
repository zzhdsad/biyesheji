<#
.SYNOPSIS
    Rebuild biyesheji Docker images and restart containers, keeping all data volumes.
.DESCRIPTION
    Flow:
      ensure .env (LLM_* cleared)
      -> (when building backend) ensure model weights in backend/models-image/prebuilt-models
         (export from running kp-backend if missing) and build the biyesheji-models:latest
         image once (reused on later runs unless -RebuildModels)
      -> docker compose -p biyesheji build (backend pulls models from that image, so the
         4.3GB weights are NOT re-sent every build)
      -> up -d (without -v) -> smoke test.
    All steps print progress live and append to build.log.
.PARAMETER Service
    both (default) / backend / frontend : rebuild only the selected image.
.PARAMETER NoCache
    Pass --no-cache to compose build (full dependency reinstall, slow but clean).
.PARAMETER SkipUp
    Build images only, do not run up -d.
.PARAMETER SkipModels
    Skip the model weights export check (use when weights are already in place).
.PARAMETER RebuildModels
    Force rebuilding the biyesheji-models:latest image (use after updating model weights).
.EXAMPLE
    .\build.ps1                 # full rebuild
    .\build.ps1 -Service backend # rebuild backend only
    .\build.ps1 -NoCache         # clean rebuild
    .\build.ps1 -RebuildModels   # also rebuild the model image
#>
param(
    [ValidateSet('both', 'backend', 'frontend')] [string] $Service = 'both',
    [switch] $NoCache,
    [switch] $SkipUp,
    [switch] $SkipModels,
    [switch] $RebuildModels
)

$ErrorActionPreference = 'Continue'
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location $ProjectDir
$LogPath = Join-Path $ProjectDir 'build.log'

function ts { Get-Date -Format 'HH:mm:ss' }
function Write-Banner($msg) {
    $line = "=" * 60
    Write-Host ""
    Write-Host "[$(ts)] $line" -ForegroundColor DarkGray
    Write-Host "[$(ts)] $msg" -ForegroundColor Cyan
    Write-Host "[$(ts)] $line" -ForegroundColor DarkGray
}
function Invoke-Logged {
    param([scriptblock] $Cmd, [string] $Label)
    Write-Banner $Label
    & $Cmd 2>&1 | ForEach-Object {
        $line = "[$(ts)] $_"
        Write-Host $line
        Add-Content -Path $LogPath -Value $line
    }
    return $LASTEXITCODE
}

# 0. Prerequisites
Write-Banner "ENV CHECK"
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Write-Error "docker not found, please install Docker Desktop"; exit 1 }
docker info > $null 2>&1
if ($LASTEXITCODE) { Write-Error "docker daemon not running, please start Docker Desktop"; exit 1 }
Write-Host "[$(ts)] docker is ready"

# 1. .env
Write-Banner ".env PREP"
if (-not (Test-Path .env)) {
    if (-not (Test-Path .env.example)) { Write-Error "missing .env.example, cannot generate .env"; exit 1 }
    Write-Host "[$(ts)] .env not found, generating from .env.example with LLM_* cleared ..."
    $src = Get-Content .env.example -Raw -Encoding UTF8
    $out = foreach ($l in ($src -split "`r?`n")) {
        if ($l -match '^\s*LLM_BASE_URL=') { 'LLM_BASE_URL=' }
        elseif ($l -match '^\s*LLM_MODEL=') { 'LLM_MODEL=' }
        elseif ($l -match '^\s*LLM_API_KEY=') { 'LLM_API_KEY=' }
        else { $l }
    }
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllLines((Join-Path $ProjectDir '.env'), $out, $enc)
    Write-Host "[$(ts)] .env created (LLM_* cleared, matching running image)"
} else {
    Write-Host "[$(ts)] .env exists, skip"
}

$needBackend = $Service -in ('both', 'backend')

# 2. Model weights + models image (only required when building backend)
if ($needBackend) {
    Write-Banner "MODEL WEIGHTS (backend/models-image/prebuilt-models)"
    $modelDir = Join-Path $ProjectDir 'backend/models-image/prebuilt-models'
    if (-not $SkipModels) {
        if (Test-Path $modelDir) {
            $sz = [math]::Round(((Get-ChildItem $modelDir -Recurse -File -Force | Measure-Object Length -Sum).Sum / 1GB), 2)
            Write-Host "[$(ts)] prebuilt-models exists (${sz}GB), skip export"
        } else {
            Write-Host "[$(ts)] prebuilt-models missing, exporting /app/models from running kp-backend ..."
            $running = docker ps --filter name=kp-backend --format '{{.Names}}'
            if (-not $running) {
                Write-Error "kp-backend not running, cannot export models. Start the old container first, or place bge-m3 / bge-reranker-v2-m3 into backend/models-image/prebuilt-models/"
                exit 1
            }
            New-Item -ItemType Directory -Force -Path $modelDir | Out-Null
            $cp = Start-Process -FilePath docker -ArgumentList "cp kp-backend:/app/models/. `"$modelDir`"" -NoNewWindow -PassThru
            while (-not $cp.HasExited) {
                $cur = [math]::Round(((Get-ChildItem $modelDir -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum / 1GB), 2)
                Write-Host "[$(ts)] exporting ... ${cur}GB" -ForegroundColor Yellow
                Start-Sleep -Seconds 5
            }
            if ($cp.ExitCode -ne 0) { Write-Error "model export failed (docker cp exit code $($cp.ExitCode))"; exit 1 }
            $ok = (Test-Path (Join-Path $modelDir 'bge-m3')) -and (Test-Path (Join-Path $modelDir 'bge-reranker-v2-m3'))
            if (-not $ok) { Write-Error "export incomplete (missing bge-m3 or bge-reranker-v2-m3)"; exit 1 }
            $sz = [math]::Round(((Get-ChildItem $modelDir -Recurse -File -Force | Measure-Object Length -Sum).Sum / 1GB), 2)
            Write-Host "[$(ts)] model export done, size ${sz}GB" -ForegroundColor Green
        }
    } else {
        Write-Host "[$(ts)] -SkipModels set, skip weight export check"
    }

    # ensure the standalone models image exists (built once, reused afterwards)
    Write-Banner "MODELS IMAGE (biyesheji-models:latest)"
    $imgExists = docker images --format '{{.Repository}}:{{.Tag}}' | Where-Object { $_ -eq 'biyesheji-models:latest' }
    if ($imgExists -and -not $RebuildModels) {
        Write-Host "[$(ts)] biyesheji-models:latest exists, skip build"
    } else {
        if ($RebuildModels) { Write-Host "[$(ts)] -RebuildModels set, rebuilding models image" }
        else { Write-Host "[$(ts)] biyesheji-models:latest missing, building from backend/models-image ..." }
        Invoke-Logged -Label "BUILD MODELS IMAGE" -Cmd { & docker build --progress=plain -t biyesheji-models:latest -f backend/models-image/Dockerfile backend/models-image }
    }
} else {
    Write-Host "[$(ts)] frontend-only build, skip model weights/image steps"
}

# 3. Build (--progress is a global compose flag, place before 'build')
$buildArgs = @('--progress=plain')
if ($NoCache) { $buildArgs += '--no-cache' }
$buildArgs += 'build'

if ($Service -in ('both', 'backend')) {
    Invoke-Logged -Label "BUILD BACKEND (biyesheji-backend)" -Cmd { & docker compose -p biyesheji @buildArgs backend }
}
if ($Service -in ('both', 'frontend')) {
    Invoke-Logged -Label "BUILD FRONTEND (biyesheji-frontend)" -Cmd { & docker compose -p biyesheji @buildArgs frontend }
}

# 4. Restart
if (-not $SkipUp) {
    Invoke-Logged -Label "RESTART CONTAINERS (keep volumes, no down -v)" -Cmd { & docker compose -p biyesheji up -d }

    Write-Banner "SMOKE TEST"
    Start-Sleep -Seconds 20
    docker ps --format "table {{.Names}}`t{{.Status}}" | Select-String kp- | ForEach-Object { Write-Host "[$(ts)] $_"; Add-Content -Path $LogPath -Value "[$(ts)] $_" }
    try {
        $c = (Invoke-WebRequest -Uri http://localhost:8000/docs -UseBasicParsing -TimeoutSec 20).StatusCode
        Write-Host "[$(ts)] backend /docs -> $c" -ForegroundColor Green
        Add-Content -Path $LogPath -Value "[$(ts)] backend /docs -> $c"
    } catch { Write-Warning "[$(ts)] backend /docs unreachable: $($_.Exception.Message)"; Add-Content -Path $LogPath -Value "[$(ts)] backend /docs unreachable: $($_.Exception.Message)" }
    try {
        $c = (Invoke-WebRequest -Uri http://localhost:3000 -UseBasicParsing -TimeoutSec 30).StatusCode
        Write-Host "[$(ts)] frontend / -> $c" -ForegroundColor Green
        Add-Content -Path $LogPath -Value "[$(ts)] frontend / -> $c"
    } catch { Write-Warning "[$(ts)] frontend / unreachable: $($_.Exception.Message)"; Add-Content -Path $LogPath -Value "[$(ts)] frontend / unreachable: $($_.Exception.Message)" }
} else {
    Write-Host "[$(ts)] -SkipUp set, skip up and smoke test"
}

Write-Banner "BUILD SCRIPT DONE (see build.log)"
Pop-Location
