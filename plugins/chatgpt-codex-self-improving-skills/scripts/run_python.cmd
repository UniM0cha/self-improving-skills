@echo off
setlocal

where.exe py >nul 2>&1
if errorlevel 1 goto python_fallback

py -3 %*
exit /b %errorlevel%

:python_fallback
where.exe python >nul 2>&1
if errorlevel 1 goto python_missing

python %*
exit /b %errorlevel%

:python_missing
>&2 echo Python 3 was not found. Install the Python launcher or add python to PATH.
exit /b 9009
