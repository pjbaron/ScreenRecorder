@echo off
setlocal
cd /d "%~dp0"

rem Builds dist\ScreenRecorder\ScreenRecorder.exe with ffmpeg.exe, ffprobe.exe and ctl.py beside it.
rem Needs python, pyinstaller and ffmpeg/ffprobe on PATH. Close the running app first (the exe is locked while it runs).
rem PyInstaller --noconfirm deletes and recreates dist\ScreenRecorder and build\ScreenRecorder.

for %%T in (python ffmpeg ffprobe) do (
    where %%T >nul 2>&1 || (echo ERROR: %%T not found on PATH & exit /b 1)
)

python -m PyInstaller --version >nul 2>&1 || (echo ERROR: pyinstaller not installed. Run: pip install -r requirements.txt pyinstaller & exit /b 1)

python -m PyInstaller --noconfirm --onedir --windowed --name ScreenRecorder recorder.py || (echo ERROR: PyInstaller failed & exit /b 1)

call :copytool ffmpeg || exit /b 1
call :copytool ffprobe || exit /b 1
copy /y ctl.py "dist\ScreenRecorder\" >nul || (echo ERROR: could not copy ctl.py & exit /b 1)

echo Built dist\ScreenRecorder\ScreenRecorder.exe
exit /b 0

:copytool
set "FOUND="
for /f "delims=" %%P in ('where %~1') do if not defined FOUND set "FOUND=%%P"
copy /y "%FOUND%" "dist\ScreenRecorder\" >nul || (echo ERROR: could not copy %FOUND% & exit /b 1)
exit /b 0
