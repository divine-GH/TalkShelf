# TalkShelf 部署：状态检查脚本（M4，Cloudflare Tunnel 方案）
# 用法：powershell -File deploy\status.ps1
# 作用：显示 uvicorn / cloudflared 进程状态、本地探活、隧道连接日志摘要
$ErrorActionPreference = 'Continue'

$deploy = $PSScriptRoot
$logs   = Join-Path $deploy 'logs'
$pidUv  = Join-Path $logs 'uvicorn.pid'
$pidTun = Join-Path $logs 'tunnel.pid'

function Test-ProcessAlive([int]$procId) {
    if (-not $procId) { return $false }
    try { Get-Process -Id $procId -ErrorAction Stop | Out-Null; return $true }
    catch { return $false }
}

function Read-PidFile([string]$path) {
    if (Test-Path $path) {
        $v = (Get-Content $path -Raw).Trim()
        if ($v -match '^\d+$') { return [int]$v }
    }
    return 0
}

Write-Host '=== TalkShelf 部署状态 ==='
Write-Host ''

# ---- uvicorn ----
$uvPid = Read-PidFile $pidUv
if (Test-ProcessAlive $uvPid) {
    Write-Host "[OK] uvicorn    运行中（PID $uvPid，127.0.0.1:8000）"
} else {
    Write-Host "[--] uvicorn    未运行"
}

# ---- cloudflared ----
$tunPid = Read-PidFile $pidTun
if (Test-ProcessAlive $tunPid) {
    Write-Host "[OK] cloudflared 运行中（PID $tunPid）"
} else {
    Write-Host "[--] cloudflared 未运行"
}

# ---- 本地探活（/api/version 免登录）----
Write-Host ''
try {
    $v = Invoke-RestMethod -Uri 'http://127.0.0.1:8000/api/version' -TimeoutSec 5
    Write-Host "[OK] 本地服务    http://127.0.0.1:8000  ->  $($v.name) v$($v.version)"
} catch {
    Write-Host "[--] 本地服务    http://127.0.0.1:8000  不可达（$($_.Exception.Message)）"
}

# ---- 隧道连接日志摘要（已注册连接行；stderr/stdout 都扫）----
Write-Host ''
$conn = $null
foreach ($f in @((Join-Path $logs 'tunnel.err.log'), (Join-Path $logs 'tunnel.out.log'))) {
    if (Test-Path $f) {
        $m = Get-Content $f -Tail 200 -ErrorAction SilentlyContinue |
             Select-String -Pattern 'Registered tunnel connection|connection.*established|FAILED|error' |
             Select-Object -Last 3
        if ($m) { $conn = $m; break }
    }
}
if ($conn) {
    Write-Host '[..] 隧道日志摘要（最近）：'
    $conn | ForEach-Object { Write-Host "     $_" }
} else {
    Write-Host '[..] 隧道日志：暂无连接记录（隧道可能未启动或日志为空）'
}
