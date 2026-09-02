@echo off
rem Shim so `run`, `run dev`, `run setup` work from cmd.exe, and from any
rem PowerShell whose execution policy would otherwise refuse the .ps1 (a
rem checkout unzipped from a download carries the mark of the web, and
rem RemoteSigned blocks it). Everything real lives in run.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
