# TalkShelf 一键启动脚本
# 用法：右键「使用 PowerShell 运行」，或 `powershell -File start.ps1`
# 自动处理：Ollama 未运行 → 启动；Ollama 运行但 bge-m3 不可见（重启后 app 自启的环境坑）→ 重启 serve
# 停止：在窗口中按 Ctrl+C
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
# 查找 Ollama 可执行文件：优先环境变量 OLLAMA_PATH，再到 PATH，再到常见安装目录；
# 找不到则 $ollama 为 $null（分发环境没装 Ollama 也能用——设置页可关闭「本地 Embedding」，见 README）。
# 跨机器可用（Windows / macOS / Linux 的 PowerShell 通用）。
$ollama = $null
if ($env:OLLAMA_PATH -and (Test-Path $env:OLLAMA_PATH)) {
    $ollama = $env:OLLAMA_PATH
} elseif (Get-Command ollama -ErrorAction SilentlyContinue) {
    $ollama = (Get-Command ollama).Source
} else {
    foreach ($cand in @(
        "$env:ProgramFiles\Ollama\ollama.exe",
        "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe",
        "$HOME\Applications\Ollama\ollama.exe",
        "/usr/local/bin/ollama",
        "/opt/homebrew/bin/ollama",
        "/usr/bin/ollama"
    )) {
        if ($cand -and (Test-Path $cand)) { $ollama = $cand; break }
    }
}
$python = Join-Path $root '.venv\Scripts\python.exe'
$hostAddr = '127.0.0.1'   # 本机访问。想用手机/局域网访问改成 '0.0.0.0'

Write-Host '=== TalkShelf 启动 ==='

# 1. 确保 Ollama + bge-m3 可用（未安装 Ollama 则跳过：分发环境可在设置页关闭「本地 Embedding」）
$needStart = $false
if ($ollama) {
    try {
        $tags = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 3
        $hasModel = $tags.models | Where-Object { $_.name -like 'bge-m3*' }
        if (-not $hasModel) { $needStart = $true }
    } catch {
        $needStart = $true
    }
} else {
    Write-Host '[..] 未检测到 Ollama：语义检索/向量查重不可用'
    Write-Host '      （安装 Ollama 后重启即恢复；也可在设置页关闭「本地 Embedding」）'
}

if ($needStart) {
    Write-Host '[..] 启动 Ollama（带正确模型路径）...'
    Get-Process -Name 'ollama*' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Start-Process -FilePath $ollama -ArgumentList 'serve' -WindowStyle Hidden
    Start-Sleep -Seconds 6
    Write-Host '[OK] Ollama 已启动'
} else {
    Write-Host '[OK] Ollama 运行中，bge-m3 可用'
}

# 2. 启动 TalkShelf（前台；Ctrl+C 停止）
Write-Host ''
Write-Host "打开浏览器访问: http://$hostAddr`:8000"
Write-Host '停止服务: 在此窗口按 Ctrl+C'
Write-Host ''
Push-Location (Join-Path $root 'backend')
try {
    # --no-use-colors：Windows PowerShell 5.1 控制台不解析 ANSI 颜色码，会原样打印 [32mINFO[0m 之类的乱码，故禁用
    & $python -m uvicorn app.main:app --host $hostAddr --port 8000 --workers 1 --no-use-colors
} finally {
    Pop-Location
}
