#!/bin/bash
# ============================================
#  mask-tool 桌面应用启动器（macOS 主入口）
#  打开 pywebview 原生窗口，无需浏览器。
#  关闭窗口即退出，无残留后台服务。
# ============================================

cd "$(dirname "$0")"

if [ ! -f "pyproject.toml" ]; then
    echo "错误：请在 mask-tool 项目目录中运行此脚本"
    read -p "按回车键退出..."
    exit 1
fi

if [ -d ".venv" ]; then
    source .venv/bin/activate
fi

echo "正在启动 mask-tool 桌面窗口..."
python -m mask_tool.desktop

if [ $? -ne 0 ]; then
    echo ""
    echo "[启动失败] 常见原因："
    echo "  1. 依赖未安装：pip install -e \".[app]\""
    echo "  2. 首次使用请先运行 install-mac.command"
    read -p "按回车键退出..."
    exit 1
fi
