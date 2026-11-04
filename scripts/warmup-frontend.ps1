# Warm up Next.js dev routes after `dev.bat` starts frontend.
#
# Why: `next dev` compiles each route on first request (1.5-14s per page on
# Docker Desktop + Windows bind mounts). Pre-requesting the routes at startup
# moves that cost out of the first click while keeping hot reload enabled.
#
# The frontend middleware only checks whether cookie `kp_token` exists, so a
# placeholder value is enough to render the route for compilation. Page SSR
# does not call business APIs, and client-side code is not executed here.

$ErrorActionPreference = 'Stop'
$Base = 'http://localhost:3000'
$Routes = @(
    '/chat',
    '/herbs',
    '/prescriptions',
    '/theories',
    '/literatures',
    '/kb',
    '/documents',
    '/users',
    '/admin',
    '/audit',
    '/settings'
)
# Windows PowerShell may drop a manually supplied Cookie header; use a real
# session cookie so the middleware lets the route render (and compile).
$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$session.Cookies.Add(
    (New-Object System.Net.Cookie('kp_token', 'warmup', '/', 'localhost'))
)

# Bounded wait for the dev server to accept connections (max 60s).
$deadline = (Get-Date).AddSeconds(60)
while ((Get-Date) -lt $deadline) {
    try {
        $probe = Invoke-WebRequest -Uri "$Base/login" -UseBasicParsing -TimeoutSec 3
        if ($probe.StatusCode -eq 200) { break }
    } catch {
        # frontend not ready yet
    }
    Start-Sleep -Seconds 1
}

# Hard bounds so a slow/cold compilation can never make startup look stuck:
# one route may take at most 60s, and warmup gives up after 4 minutes. Routes
# that are skipped still compile on first visit; they are only a pre-warm.
$PerRouteTimeoutSec = 60
$TotalBudgetSec = 240
$total = $Routes.Count
$index = 0
$started = Get-Date

Write-Host ('[warmup] Precompiling {0} frontend routes (first start may take 1-2 minutes)...' -f $total)
foreach ($route in $Routes) {
    $index++
    if (((Get-Date) - $started).TotalSeconds -ge $TotalBudgetSec) {
        Write-Host ('[warmup] Time budget reached; skipping remaining {0} routes.' -f ($total - $index + 1))
        break
    }
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $resp = Invoke-WebRequest -Uri "$Base$route" -WebSession $session -UseBasicParsing -TimeoutSec $PerRouteTimeoutSec
        $status = $resp.StatusCode
        Write-Host ('[warmup] ({0}/{1}) {2} {3:N1}s HTTP {4}' -f $index, $total, $route, $sw.Elapsed.TotalSeconds, $status)
    } catch {
        Write-Host ('[warmup] ({0}/{1}) {2} failed: {3}' -f $index, $total, $route, $_.Exception.Message)
    }
}
Write-Host '[warmup] Done.'
