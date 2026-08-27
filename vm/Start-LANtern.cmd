@echo off
rem Start the LANtern VM headless. Closing this window does not stop the VM.
"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe" startvm lantern --type headless
if errorlevel 1 (
  echo.
  echo Could not start lantern. Is it already running?
  pause
) else (
  echo.
  echo LANtern is starting. Give it about 40 seconds, then open:
  echo     http://192.168.0.115:8090
  timeout /t 6 >nul
)
