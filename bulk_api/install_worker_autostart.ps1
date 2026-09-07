param(
    [string]$TaskName = "EcommerceBulkWorker"
)

$ErrorActionPreference = "Stop"
$pythonPath = (Get-Command python.exe -ErrorAction Stop).Source
$workerPath = Join-Path $PSScriptRoot "bulk_worker.py"

$actionParams = @{
    Execute = $pythonPath
    Argument = '"' + $workerPath + '"'
    WorkingDirectory = $PSScriptRoot
}
$action = New-ScheduledTaskAction @actionParams
$trigger = New-ScheduledTaskTrigger -AtStartup
$principalParams = @{
    UserId = "SYSTEM"
    LogonType = "ServiceAccount"
    RunLevel = "Highest"
}
$principal = New-ScheduledTaskPrincipal @principalParams
$settingsParams = @{
    StartWhenAvailable = $true
    RestartCount = 5
    RestartInterval = New-TimeSpan -Minutes 1
    ExecutionTimeLimit = [TimeSpan]::Zero
    MultipleInstances = "IgnoreNew"
}
$settings = New-ScheduledTaskSettingsSet @settingsParams
$registerParams = @{
    TaskName = $TaskName
    Action = $action
    Trigger = $trigger
    Principal = $principal
    Settings = $settings
    Description = "微信小店和小红书批量商品发布后台 Worker"
    Force = $true
}
Register-ScheduledTask @registerParams | Out-Null

Write-Host "已创建系统启动任务: $TaskName"
Write-Host "Worker: $workerPath"
Write-Host "Python: $pythonPath"
