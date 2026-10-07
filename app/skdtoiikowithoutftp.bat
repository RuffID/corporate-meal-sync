@echo off
py -3 "%~dp0main\loadwithoutftp.py" %*
exit /b %errorlevel%
