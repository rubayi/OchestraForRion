@echo off
cd /d "C:\Users\rubay\Documents\projects\rion-agent"
"C:\Users\rubay\AppData\Local\Python\pythoncore-3.14-64\python.exe" orchestrator.py --now >> logs\orchestrator.log 2>&1
