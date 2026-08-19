# note-brain 一键启动脚本
# 用法：右键「使用 PowerShell 运行」，或 `powershell -File start.ps1`
# 自动处理：Ollama 未运行 → 启动；Ollama 运行但 bge-m3 不可见（重启后 app 自启的环境坑）→ 重启 serve
# 停止：在窗口中按 Ctrl+C
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$ollama = 'C:\Users\Admin\AppData\Local\Programs\Ollama\ollama.exe'
$python = Join-Path $root '.venv\Scripts\python.exe'
$hostAddr = '127.0.0.1'   # 本机访问。想用手机/局域网访问改成 '0.0.0.0'

Write-Host '=== note-brain 启动 ==='

# 1. 确保 Ollama + bge-m3 可用
$needStart = $false
try {
    $tags = Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags' -TimeoutSec 3
    $hasModel = $tags.models | Where-Object { $_.name -like 'bge-m3*' }
    if (-not $hasModel) { $needStart = $true }
} catch {
    $needStart = $true
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

# 2. 启动 note-brain（前台；Ctrl+C 停止）
Write-Host ''
Write-Host "打开浏览器访问: http://$hostAddr`:8000"
Write-Host '停止服务: 在此窗口按 Ctrl+C'
Write-Host ''
Push-Location (Join-Path $root 'backend')
try {
    & $python -m uvicorn app.main:app --host $hostAddr --port 8000 --workers 1
} finally {
    Pop-Location
}
