@echo off
:restart
cd /d "C:\Users\rubay\Documents\projects\rion-agent"
"C:\Users\rubay\AppData\Local\Python\pythoncore-3.14-64\python.exe" orchestrator.py >> logs\orchestrator_daemon.log 2>&1
echo [%date% %time%] Orchestrator exited, restarting in 15s... >> logs\orchestrator_daemon.log
timeout /t 15 /nobreak > nul
goto restart
