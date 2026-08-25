# TalkShelf 部署：手动启动脚本（M4，Cloudflare Tunnel 方案）
# 用法：powershell -File deploy\start.ps1
# 作用：1) 后台启动 TalkShelf（uvicorn 127.0.0.1:8000 --workers 1）
#       2) 后台启动 cloudflared 隧道（读 deploy/cloudflared/tunnel-token.txt 的 token）
# 停止：deploy\stop.ps1；状态：deploy\status.ps1
# 注意：本脚本不注册自启；重启电脑后需手动再跑一次。
$ErrorActionPreference = 'Stop'

$deploy    = $PSScriptRoot
$repo      = Split-Path $deploy -Parent
$python    = Join-Path $repo '.venv\Scripts\python.exe'
$backend   = Join-Path $repo 'backend'
$cfexe     = Join-Path $deploy 'cloudflared\cloudflared.exe'
$tokenFile = Join-Path $deploy 'cloudflared\tunnel-token.txt'
$logs      = Join-Path $deploy 'logs'
$pidUv     = Join-Path $logs 'uvicorn.pid'
$pidTun    = Join-Path $logs 'tunnel.pid'

# ---- 公网访问地址：从仓库根 .env 的 PUBLIC_URL 读取（未配置时回退示例域名）----
$publicUrl = 'https://note.example.com'
$envFile   = Join-Path $repo '.env'
if (Test-Path $envFile) {
    foreach ($line in (Get-Content $envFile -ErrorAction SilentlyContinue)) {
        if ($line -match '^\s*PUBLIC_URL\s*=\s*(.+?)\s*$') {
            $publicUrl = $Matches[1].Trim().Trim('"').Trim("'")
            break
        }
    }
}

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

if (-not (Test-Path $python)) {
    Write-Host "[X] 未找到 venv Python：$python"
    Write-Host "    请先在仓库根目录用 VS Code/命令行创建 .venv 并安装依赖（见 README「安装」）。"
    exit 1
}
New-Item -ItemType Directory -Force -Path $logs | Out-Null

Write-Host '=== TalkShelf 部署启动 ==='

# ---- 1. TalkShelf（uvicorn，只绑回环）----
$uvPid = Read-PidFile $pidUv
if (Test-ProcessAlive $uvPid) {
    Write-Host "[..] uvicorn 已在运行（PID $uvPid），跳过启动"
} else {
    $proc = Start-Process -FilePath $python `
        -ArgumentList @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port','8000','--workers','1','--no-use-colors') `
        -WorkingDirectory $backend -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logs 'uvicorn.out.log') `
        -RedirectStandardError (Join-Path $logs 'uvicorn.err.log') -PassThru
    $proc.Id | Out-File -FilePath $pidUv -Encoding ascii
    Write-Host "[OK] uvicorn 已启动（PID $($proc.Id)，日志 deploy\logs\uvicorn.*.log）"
}

# ---- 2. cloudflared 隧道 ----
$tok = ''
if (Test-Path $tokenFile) { $tok = (Get-Content $tokenFile -Raw).Trim() }
if (-not $tok) {
    Write-Host ""
    Write-Host "[..] 未找到隧道 token（deploy\cloudflared\tunnel-token.txt）——公网入口未启动。"
    Write-Host "     步骤：Cloudflare 控制台 Networking -> Tunnels -> Create a tunnel（Windows）"
    Write-Host "     把安装命令里的 eyJ... 段粘贴进 tunnel-token.txt 后重跑本脚本。"
} else {
    if (-not (Test-Path $cfexe)) {
        Write-Host "[X] 未找到 cloudflared.exe：$cfexe（下载后放入该路径）"
        exit 1
    }
    $tunPid = Read-PidFile $pidTun
    if (Test-ProcessAlive $tunPid) {
        Write-Host "[..] cloudflared 已在运行（PID $tunPid），跳过启动"
    } else {
        $proc = Start-Process -FilePath $cfexe `
            -ArgumentList @('tunnel','run','--token',$tok) `
            -WorkingDirectory (Join-Path $deploy 'cloudflared') -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $logs 'tunnel.out.log') `
            -RedirectStandardError (Join-Path $logs 'tunnel.err.log') -PassThru
        $proc.Id | Out-File -FilePath $pidTun -Encoding ascii
        Write-Host "[OK] cloudflared 已启动（PID $($proc.Id)，日志 deploy\logs\tunnel.*.log）"
    }
}

# ---- 3. 健康检查（启动后等 4 秒看进程是否存活）----
Start-Sleep -Seconds 4
$uvPid = Read-PidFile $pidUv
$tunPid = Read-PidFile $pidTun
$uvOk = Test-ProcessAlive $uvPid
$tunOk = Test-ProcessAlive $tunPid
if (-not $uvOk) {
    Write-Host "[!] uvicorn 进程未存活，请查看 deploy\logs\uvicorn.err.log"
}
if ($tok -and -not $tunOk) {
    Write-Host "[!] cloudflared 进程未存活，请查看 deploy\logs\tunnel.err.log（常见：7844 端口出站被封）"
}

Write-Host ""
Write-Host "本地访问： http://127.0.0.1:8000"
Write-Host "公网访问： $publicUrl（需完成 deploy\README.md 第 1/2/4 步）"
Write-Host "状态查询： deploy\status.ps1；停止： deploy\stop.ps1"
