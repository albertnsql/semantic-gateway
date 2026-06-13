@echo off
title AI Semantic Gateway — Launcher

echo.
echo  ============================================================
echo   AI Semantic Gateway — Starting Services
echo  ============================================================
echo.

REM ── Paths ────────────────────────────────────────────────────
set ROOT=%~dp0
set GATEWAY=%ROOT%gateway
set FRONTEND=%ROOT%frontend
set PYTHON=C:\Users\AlbertNadar\AppData\Local\Programs\Python\Python311\python.exe

REM ── Check Python ─────────────────────────────────────────────
if not exist "%PYTHON%" (
    echo  [ERROR] Python not found at:
    echo          %PYTHON%
    echo          Edit this file and fix the PYTHON path.
    pause
    exit /b 1
)

REM ── Check gateway dir ────────────────────────────────────────
if not exist "%GATEWAY%\main.py" (
    echo  [ERROR] gateway\main.py not found.
    echo          Make sure you are running this from the Streaming_Analytics\ root.
    pause
    exit /b 1
)

REM ── Check frontend dir ───────────────────────────────────────
if not exist "%FRONTEND%\package.json" (
    echo  [ERROR] frontend\package.json not found.
    echo          Make sure the frontend has been scaffolded.
    pause
    exit /b 1
)

echo  [1/2] Starting FastAPI backend on http://localhost:8000 ...
start "Backend — FastAPI Gateway" cmd /k "title Backend ^| FastAPI Gateway && cd /d "%GATEWAY%" && echo. && echo  Backend starting... && echo  Docs: http://localhost:8000/docs && echo. && "%PYTHON%" -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload"

REM Small delay so backend gets a head start
timeout /t 3 /nobreak >nul

echo  [2/2] Starting React frontend on http://localhost:5173 ...
start "Frontend — React Vite" cmd /k "title Frontend ^| React Vite && cd /d "%FRONTEND%" && echo. && echo  Frontend starting... && echo  App: http://localhost:5173 && echo. && npm run dev"

echo.
echo  ============================================================
echo   Both services launched in separate windows.
echo.
echo   Backend   →  http://localhost:8000
echo   API Docs  →  http://localhost:8000/docs
echo   Frontend  →  http://localhost:5173
echo  ============================================================
echo.

REM Open browser after a short delay
timeout /t 5 /nobreak >nul
echo  Opening browser...
start "" "http://localhost:5173"

echo.
echo  Press any key to close this launcher window.
echo  (The backend and frontend windows will keep running.)
pause >nul
