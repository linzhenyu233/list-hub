# 登录后自动启动「批量发布 Worker」。
#
# 只启动 Worker：三个 API 服务（微信/小红书/中台）仍由人工按需启动，本脚本不介入。
#
# ⚠️ 必须由「当前登录用户」运行（不要改成 SYSTEM）：
#   Worker 发品时要读共享盘 \\192.168.10.250 上的图片，用的是运行进程所在
#   Windows 会话的凭据；SYSTEM 会话没有这份凭据，会导致发布读图报 WinError 1326。
#
# 安装/卸载见同目录 install_autostart_login.ps1

$ErrorActionPreference = "Continue"
$root = "C:\Users\zssj\PycharmProjects\PythonProject"
$py = "C:\Users\zssj\AppData\Local\Programs\Python\Python313\python.exe"
$workDir = "$root\bulk_api"

# 自启没有窗口，过程写进日志，方便事后确认"是没启动还是启动失败"
try { Start-Transcript -Path (Join-Path $PSScriptRoot "_autostart.log") -Append | Out-Null } catch { }

Write-Host "[worker_autostart] $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') 检查 Worker"

# 已在运行就不重复启动（Worker 自身也有单实例互斥，这里提前判断只为日志清晰）
$running = Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -match 'bulk_worker\.py' }
if ($running) {
    Write-Host ("[skip ] Worker 已在运行: PID " + (($running | ForEach-Object { $_.ProcessId }) -join ", "))
    return
}

Start-Process -FilePath $py -ArgumentList "bulk_worker.py" -WorkingDirectory $workDir `
    -RedirectStandardOutput "$workDir\_worker.out" -RedirectStandardError "$workDir\_worker.err" -WindowStyle Hidden
Start-Sleep -Seconds 6

$now = Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -match 'bulk_worker\.py' }
if ($now) {
    Write-Host ("[ok   ] Worker 已启动: PID " + (($now | ForEach-Object { $_.ProcessId }) -join ", "))
} else {
    Write-Host "[fail ] Worker 未能启动，下面是 _worker.err 末尾："
    Get-Content "$workDir\_worker.err" -Tail 20 -ErrorAction SilentlyContinue
}
