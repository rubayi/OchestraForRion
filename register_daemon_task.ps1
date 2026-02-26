# AlohaCTO Orchestrator Daemon 작업 등록
# 실행: powershell -ExecutionPolicy Bypass -File register_daemon_task.ps1

$taskName = 'AlohaCTO_Orchestrator_Daemon'
$batPath  = 'C:\Users\rubay\Documents\projects\rion-agent\run_orchestrator_daemon.bat'
$workDir  = 'C:\Users\rubay\Documents\projects\rion-agent'

# 기존 작업 삭제
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

# 작업 구성
$action   = New-ScheduledTaskAction -Execute $batPath -WorkingDirectory $workDir
$trigger  = New-ScheduledTaskTrigger -AtLogOn
$trigger.Delay = 'PT30S'
$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances      IgnoreNew `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit     '00:00:00' `
    -RestartCount           999 `
    -RestartInterval        (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable

$task = Register-ScheduledTask `
    -TaskName  $taskName `
    -Action    $action `
    -Trigger   $trigger `
    -Settings  $settings `
    -Force `
    -RunLevel  Limited

if ($task) {
    Write-Host "✅ 등록 완료: $taskName (State: $($task.State))"
} else {
    Write-Host "❌ 등록 실패"
}
