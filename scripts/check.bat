@echo off
rem 新版 LSTtranslate 的全部检查：测试（含依赖规则）、类型检查、lint。
setlocal
cd /d "%~dp0.."
set "PYTHONUTF8=1"

echo === pytest ===
uv run pytest || goto :fail
echo === basedpyright ===
uv run basedpyright || goto :fail
echo === ruff ===
uv run ruff check lst tests || goto :fail
uv run ruff format --check lst tests || goto :fail
echo === import-linter ===
uv run lint-imports || goto :fail

echo.
echo All checks passed.
endlocal
exit /b 0

:fail
echo.
echo [FAILED] see output above.
endlocal
exit /b 1
