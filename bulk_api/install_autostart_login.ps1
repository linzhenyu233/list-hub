# 注册「登录时自动启动批量发布 Worker」的计划任务（当前登录用户身份）。
#
# 与项目既有的 install_worker_autostart.ps1 的区别：
#   · 既有脚本用 SYSTEM 账号（开机自启、无需登录），但 SYSTEM 会话没有共享盘凭据，
#     发品读图会报 WinError 1326（项目代码注释里记录过这个坑）；
#   · 本脚本用「当前用户 + 登录时触发」：不需要存密码，且能继承该用户的共享盘凭据。
#
# 范围：只拉起 Worker；三个 API 服务（微信 8000 / 小红书 8010 / 中台 8020）仍手动启动。
#
# 用法（普通权限即可）：
#   安装：      powershell -ExecutionPolicy Bypass -File bulk_api\install_autostart_login.ps1
#   查看：      Get-ScheduledTask -TaskName BulkWorkerAutostart
#   手动跑一次：Start-ScheduledTask -TaskName BulkWorkerAutostart
#   卸载：      Unregister-ScheduledTask -TaskName BulkWorkerAutostart -Confirm:$false

# 任务名说明：不用 EcommerceBulkWorker —— 那个名字已被一个「更高权限账户（SYSTEM/管理员）」
# 创建的既有任务占用，当前用户既查不到（Get-ScheduledTask 不可见）也覆盖不了
# （Register-ScheduledTask 报 Access denied）。故换名并存，互不影响。
param(
    [string]$TaskName = "BulkWorkerAutostart"
)

$ErrorActionPreference = "Stop"

$startScript = Join-Path $PSScriptRoot "start_worker.ps1"
if (-not (Test-Path $startScript)) {
    throw "找不到启动脚本：$startScript"
}

$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$startScript`""

# AtLogOn：登录后触发 → 继承当前用户的网络凭据（这是能读共享盘的关键）
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERNAME"

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description "登录后自动启动批量发布 Worker（当前用户身份，可读共享盘）。三个 API 服务不在此任务内，仍需手动启动。" `
    -Force | Out-Null

Write-Host "已创建计划任务：$TaskName"
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State | Format-Table -AutoSize
Write-Host "启动脚本：$startScript"
