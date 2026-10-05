@echo off
chcp 65001 >nul
title OmniRank AI - 一键启动

echo.
echo ========================================
echo   🚀 OmniRank AI 一键启动脚本
echo ========================================
echo.

:: 检查并关闭占用8000端口的进程（后端）
echo [1/4] 检查后端端口 8000...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8000 ^| findstr LISTENING') do (
    echo      发现占用端口的进程 PID: %%a，正在关闭...
    taskkill /F /PID %%a >nul 2>&1
)
echo      ✓ 端口 8000 已就绪

:: 检查并关闭占用1688端口的进程（前端）
echo [2/4] 检查前端端口 1688...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :1688 ^| findstr LISTENING') do (
    echo      发现占用端口的进程 PID: %%a，正在关闭...
    taskkill /F /PID %%a >nul 2>&1
)
echo      ✓ 端口 1688 已就绪

:: 启动后端
echo [3/4] 启动后端服务...
cd /d "%~dp0"
start "OmniRank-Backend" cmd /k "title OmniRank Backend (Port 8000) && python -m uvicorn server:app --reload --port 8000"
timeout /t 3 /nobreak >nul
echo      ✓ 后端已启动 (http://localhost:8000)

:: 启动前端
echo [4/4] 启动前端服务...
cd /d "%~dp0frontend"
start "OmniRank-Frontend" cmd /k "title OmniRank Frontend (Port 1688) && npm run dev"
timeout /t 5 /nobreak >nul
echo      ✓ 前端已启动 (http://localhost:1688)

echo.
echo ========================================
echo   ✅ 启动完成！
echo ========================================
echo.
echo   📊 后端API:  http://localhost:8000
echo   🖥️  前端界面: http://localhost:1688
echo   📚 API文档:  http://localhost:8000/docs
echo.
echo   按任意键打开浏览器...
pause >nul

start http://localhost:1688
