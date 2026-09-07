param(
    [string]$TaskName = "EcommerceBulkWorker"
)

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "已删除系统启动任务: $TaskName"
} else {
    Write-Host "系统启动任务不存在: $TaskName"
}
