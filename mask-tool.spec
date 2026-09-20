# mask-tool PyInstaller 打包配置（onedir 模式）
# 产物：dist/mask-tool/mask-tool.exe + _internal/
# 部署：整目录拷贝到目标机，双击 exe（无控制台窗口，依赖全部内置）

# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_submodules,
    copy_metadata,
)

# importlib.metadata 版本查询需要包元数据（streamlit/version.py 启动即查；
# altair/pandas 等在 streamlit 加载链上也会查自身版本）
_metadata = []
for _pkg in [
    "streamlit", "streamlit-aggrid", "webview", "altair", "pandas", "numpy",
    "openpyxl", "python-docx", "python-pptx", "pymupdf", "jieba", "typer",
    "rich", "pydeck", "tornado", "click", "pyyaml", "packaging",
]:
    try:
        _metadata += copy_metadata(_pkg)
    except Exception:
        pass  # 未安装的包跳过

datas = [
    # streamlit 前端静态资源（体积大头，~100MB）
    *collect_data_files("streamlit", include_py_files=False),
    # streamlit-aggrid 前端 build 产物
    *collect_data_files("st_aggrid", include_py_files=False),
    # jieba 分词词典
    *collect_data_files("jieba", include_py_files=False),
    # 包元数据（importlib.metadata）
    *_metadata,
    # 业务资源：web/app.py（frozen 下由 _MEIPASS/mask_tool/web/app.py 定位）
    ("src/mask_tool/web/app.py", "mask_tool/web"),
    # 配置与词库（config_loader 的 resolve_data_path 锚点：exe 同级 config/ 优先）
    ("config/default.yaml", "config"),
    ("config/lexicon.yaml", "config"),
    ("config/whitelist.yaml", "config"),
    ("config/sample_lexicon.yaml", "config"),
    # 品牌图标（窗口/快捷方式）
    ("assets/masktool.ico", "assets"),
]

hiddenimports = [
    # pywebview Windows 后端（pythonnet/WinForms/EdgeChromium）
    *collect_submodules("webview.platforms"),
    # streamlit 运行时动态导入
    "streamlit.web.cli",
    "streamlit.runtime.scriptrunner.magic_funcs",
    # typer/rich 动态导入面
    "typer",
    "rich.markdown",
]

a = Analysis(
    ["src/mask_tool/desktop.py"],
    pathex=["src"],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "pytest",
        "IPython",
        "notebook",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="mask-tool",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,               # 无控制台（等效 pythonw）
    icon="assets/masktool.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="mask-tool",
)
