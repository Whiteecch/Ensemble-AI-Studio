@echo off
rem Ensemble-AI-Studio —— 桌面应用启动器（双击即可；可加参数，如 --stub 离线）
chcp 65001 >nul
cd /d "%~dp0"

rem 1) 若系统环境变量里没有 DEEPSEEK_API_KEY（例如双击经 Explorer 启动、其环境未刷新），
rem    尝试读同目录 .env.deepseek（内容一行：DEEPSEEK_API_KEY=sk-你的key），让双击也能用真模型。
if not defined DEEPSEEK_API_KEY if exist "%~dp0.env.deepseek" (
    for /f "usebackq tokens=1,* delims==" %%a in ("%~dp0.env.deepseek") do (
        if /i "%%a"=="DEEPSEEK_API_KEY" set "DEEPSEEK_API_KEY=%%b"
    )
)

rem 2) 明确告诉用的是哪个后端，避免看到占位台词还不清楚原因。
if defined DEEPSEEK_API_KEY (
    echo [后端] 检测到 DEEPSEEK_API_KEY，将使用 DeepSeek 真实模型。
) else (
    echo [后端] 未读到 DEEPSEEK_API_KEY —— 将回退离线 stub 占位台词。
    echo        要接真实模型，任选其一：
    echo          a) 系统级 setx DEEPSEEK_API_KEY 你的key  之后重开所有窗口
    echo          b) 在本目录新建 .env.deepseek，内容写一行：DEEPSEEK_API_KEY=sk-你的key
)
echo.

set PYTHONIOENCODING=utf-8

rem 未建虚拟环境时给出可照做的提示（公开仓库最常见的第一次失败）。
if not exist ".venv\Scripts\python.exe" (
    echo [错误] 还没建虚拟环境 `.venv`。先在本目录执行：
    echo         python -m venv .venv
    echo         .venv\Scripts\python -m pip install -e ".[gui]"
    echo.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -m harness.gui.app %*
echo.
pause
