# TalkShelf 部署：手动停止脚本（M4，Cloudflare Tunnel 方案）
# 用法：powershell -File deploy\stop.ps1
# 作用：停止 cloudflared 隧道与 uvicorn（按 PID 文件，运行中才杀）
$ErrorActionPreference = 'Stop'

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

Write-Host '=== TalkShelf 部署停止 ==='

# ---- 1. cloudflared（额外按进程名兜底：进程名唯一，不会误杀）----
$tunPid = Read-PidFile $pidTun
$stopped = @()
if (Test-ProcessAlive $tunPid) {
    Stop-Process -Id $tunPid -Force -ErrorAction SilentlyContinue
    $stopped += "cloudflared PID $tunPid"
}
Get-Process -Name 'cloudflared' -ErrorAction SilentlyContinue | ForEach-Object {
    if ($_.Id -ne $tunPid) {
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        $stopped += "cloudflared PID $($_.Id)"
    }
}
if ($stopped.Count -gt 0) { Write-Host "[OK] 已停止：$($stopped -join '、')" }
else { Write-Host "[..] cloudflared 未在运行" }
Remove-Item $pidTun -Force -ErrorAction SilentlyContinue

# ---- 2. uvicorn（只用 PID 文件，避免误杀其它 python 进程）----
$uvPid = Read-PidFile $pidUv
if (Test-ProcessAlive $uvPid) {
    Stop-Process -Id $uvPid -Force -ErrorAction SilentlyContinue
    Write-Host "[OK] 已停止：uvicorn PID $uvPid"
} else {
    Write-Host "[..] uvicorn 未在运行"
    if (Test-Path $pidUv) {
        Write-Host "     （PID 文件存在但进程已不在，已清理）"
    }
}
Remove-Item $pidUv -Force -ErrorAction SilentlyContinue

Write-Host '完成。'
