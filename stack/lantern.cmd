@echo off
REM Run `lantern` from PowerShell or cmd.exe.
REM
REM The real script is a bash script that has to run inside Ubuntu WSL: it
REM drives docker compose, and Docker Desktop resolves bind paths in the WSL
REM namespace. From PowerShell, `./lantern` has no Windows file association, so
REM Windows hands it to whatever opens unknown files -- usually VS Code.
REM
REM   .\lantern.cmd status
REM   .\lantern.cmd use minecraft
REM   .\lantern.cmd stop
REM
REM --cd takes a Windows path and drops WSL into the matching /mnt/c/... one.
wsl.exe -d Ubuntu-26.04 --cd "%~dp0" -- ./lantern %*
