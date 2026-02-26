# 시작프로그램 바로가기 생성
# orchestrator 데몬 로그인 시 자동 시작

$startupDir  = [System.Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startupDir 'AlohaCTO_Orchestrator_Daemon.lnk'
$batPath      = 'C:\Users\rubay\Documents\projects\rion-agent\run_orchestrator_daemon.bat'
$workDir      = 'C:\Users\rubay\Documents\projects\rion-agent'

$shell     = New-Object -ComObject WScript.Shell
$shortcut  = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath        = $batPath
$shortcut.WorkingDirectory  = $workDir
$shortcut.WindowStyle       = 7   # 최소화
$shortcut.Description       = 'AlohaCTO Orchestrator Telegram Daemon'
$shortcut.Save()

Write-Host "바로가기 생성: $shortcutPath"
