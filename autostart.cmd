@echo off
echo [%date% %time%] [reg-run] fired >> "C:\Users\GROK\Documents\qwen-agent\gpu_monitor\autostart.log"
start "" "C:\Users\GROK\AppData\Local\Programs\Python\Python312\pythonw.exe" "C:\Users\GROK\Documents\qwen-agent\gpu_monitor\gpu_monitor.py"
