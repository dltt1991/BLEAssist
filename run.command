#!/usr/bin/env bash

set -u

SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

PYTHON="python3"
if [[ -x ".venv/bin/python" ]]; then
    PYTHON=".venv/bin/python"
fi

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "未找到 Python 3。请先安装 Python 3.10 或更高版本。"
    read -r -p "按回车键关闭…"
    exit 1
fi

if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
    echo "需要 Python 3.10 或更高版本。"
    "$PYTHON" --version
    read -r -p "按回车键关闭…"
    exit 1
fi

if ! "$PYTHON" -c 'import tkinter' >/dev/null 2>&1; then
    PYTHON_SERIES="$($PYTHON -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    echo "当前 Python 缺少 Tkinter。若使用 Homebrew，请执行："
    echo "  brew install python-tk@${PYTHON_SERIES}"
    read -r -p "按回车键关闭…"
    exit 1
fi

if ! "$PYTHON" -c 'import bleak' >/dev/null 2>&1; then
    echo "尚未安装 Bleak。请在项目目录执行："
    echo "  $PYTHON -m pip install -r requirements.txt"
    read -r -p "按回车键关闭…"
    exit 1
fi

exec "$PYTHON" app.py
