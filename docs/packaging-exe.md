# mask-tool 打包为 exe 的调研报告

> 调研日期：2026-09-18
> 背景：当前通过 `start-windows.bat` → venv `pythonw.exe` 启动，双击后仅闪现毫秒级 cmd 窗口。
> 问题：能否打包为标准 exe 程序，通过桌面快捷方式双击打开？

## 一、结论

**可行，推荐 PyInstaller（onedir 模式）**，但不是"一键打包"，有两个必须处理的工程点：

| 维度 | 评估 |
| --- | --- |
| 技术可行性 | ✅ 成熟。pywebview 官方文档有专门的 PyInstaller 章节；streamlit 社区有成熟先例 |
| 工作量 | 约 0.5–1 天（含编写 .spec、补齐 hiddenimports、安装包验证） |
| 体积预估 | **300–500 MB**（onedir）；onefile 更大且启动更慢，不推荐 |
| 启动速度 | onedir 模式 3–8 秒（streamlit 初始化 + WebView2 加载），与当前 venv 方式相当 |
| 代码改动 | `desktop.py` 需增加 frozen 分支改造 streamlit 启动方式（见 3.1） |
| WebView2 依赖 | Win10（较新版本）/Win11 随 Edge 自带，无需处理；极旧 Win10 需引导安装运行时 |

## 二、为什么体积大：依赖构成

exe 体积主要由这些依赖贡献（均为本项目 `pyproject.toml` 声明）：

- **streamlit**：纯 Python 部分约 30 MB，且自带 **前端静态资源**（`streamlit/static/`，约 100+ MB 级别的 JS/CSS/字体）
- **pandas**（含 numpy）：约 80–120 MB
- **pymupdf**（MuPDF C 扩展）：约 40–60 MB
- **pywebview + WebView2 互操作层**：约 5–10 MB
- **jieba**：词典约 60 MB（若用智能识别功能则必须打包；可评估改用 `jieba.dt` 按需加载或换更小的分词方案）
- python-docx / python-pptx / openpyxl / rich 等：合计约 20 MB

压缩优化空间：`--strip`、排除测试文件、`exclude-module` 裁剪未用模块（如 pandas 的 PyArrow 后端若未用）。预计可将 onedir 压到 250–350 MB，但无法低于 200 MB 量级——这是 Python 科学计算栈桌面分发的固有成本。

## 三、必须处理的两个工程点

### 3.1 `desktop.py` 的 streamlit 启动逻辑（核心改造）

当前代码：

```python
cmd = [sys.executable, "-m", "streamlit", "run", str(app_file), ...]
subprocess.Popen(cmd, ...)
```

PyInstaller frozen 后 `sys.executable` 指向 exe 自身，而 **打包后的 exe 不支持 `-m` 模块运行**，此逻辑会直接失效。需要增加 frozen 分支：

```python
if getattr(sys, "frozen", False):
    # 方案 A（推荐）：exe 以特殊参数重新拉起自身作为 streamlit 子进程
    cmd = [sys.executable, "--streamlit-server", "--server.port", str(port), ...]
    # __main__ 入口检测到该参数时，进程内执行：
    #   from streamlit.web import cli as stcli
    #   sys.argv = ["streamlit", "run", app_path, ...]
    #   stcli.main()
else:
    cmd = [sys.executable, "-m", "streamlit", "run", ...]  # 现有逻辑不变
```

`app_file` 也要按 frozen 调整：打包时把 `web/app.py` 作为数据文件收集，运行时从 `sys._MEIPASS`（onefile）或 `sys.executable` 同级目录（onedir）解析。

方案 B（备选）：在同进程内用线程跑 streamlit（`stcli.main()` 阻塞在线程中）。改动更少，但 streamlit 与 pywebview 的事件循环共存于一个进程，出错时无法独立重启，稳定性略差；当前"独立子进程 + taskkill 进程树"的架构保留方案 A 更稳。

### 3.2 PyInstaller 的资源收集（.spec 配置）

```python
# mask-tool.spec 关键项（示意）
a = Analysis(
    ["src/mask_tool/__main__.py"],
    datas=[
        # streamlit 前端静态资源（最大头）
        *collect_data_files("streamlit"),
        # 业务数据：分词词典、配置模板等
        *collect_data_files("jieba"),
        ("src/mask_tool/web/app.py", "mask_tool/web"),
        ("assets/masktool.ico", "assets"),
    ],
    hiddenimports=[
        "engineio.async_drivers",  # streamlit 依赖链
        "pywebview.platforms.edgechromium",
        "pandas._libs.tsernels",   # 按实际缺失补
    ],
    excludes=["tkinter", "matplotlib", "pytest"],
)
exe = EXE(..., icon="assets/masktool.ico", console=False, name="mask-tool")
```

注意点：

- `console=False` 保证无控制台（与本次 pythonw 优化等效；本次新增的 `_fatal_dialog` 原生错误弹窗在 exe 下同样生效，无需再改）
- pywebview 需要 `--collect-all webview` 或手动补 platform 后端的 hiddenimports
- streamlit 版本升级后 `collect_data_files` 清单可能变化，建议把打包验证纳入发布流程

## 四、分发与安装体验

- **onedir + Inno Setup / NSIS**：产出 `mask-tool-setup.exe` 安装包，安装到 Program Files，自动创建开始菜单/桌面快捷方式、注册卸载信息——即完全体的"标准 Windows 软件"体验
- 仅绿色分发：zip 压缩 `dist/mask-tool/` 整个目录，用户解压后发送快捷方式到桌面
- 首次启动智能识别若用到本地模型/词典，需确认其路径解析在 frozen 下正确（`_MEIPASS` 相对路径）

## 五、替代方案对比

| 方案 | 体积 | 工作量 | 评价 |
| --- | --- | --- | --- |
| **PyInstaller（本报告主推）** | 300–500 MB | 0.5–1 天 | 社区成熟、文档多、与现有代码兼容性最好 |
| Nuitka | 200–350 MB | 1–2 天 | 编译为 C，体积小启动快，但需 C 编译器，streamlit 动态导入排查成本高 |
| cx_Freeze | 类似 PyInstaller | 1 天左右 | 无明显优势 |
| Embeddable Python + 自解压 | 200–300 MB | 1–2 天 | 体积最小，但需自行处理 venv 启动逻辑，本质是把当前 venv 方案搬家 |
| Electron/Tauri 重写前端 | 80–150 MB | 1–2 周 | 需放弃 streamlit 前端，工作量不成比例 |
| **维持现状（venv + pythonw 快捷方式）** | 0（复用 venv） | 0 | 本次已优化到位；缺点：依赖本机项目目录，无法分发 |

## 六、建议

1. **仅自用**：无需打包。当前 bat 修复 + pythonw 方案已是零成本最优解；后续若想去掉最后的 cmd 闪现，可将桌面入口换成直指 `pythonw.exe` 的快捷方式（一分钟工作量，随时可做）。
2. **需要分发给别人**（推荐触发条件：出现 2 个以上无 Python 环境的使用者）：投入 PyInstaller onedir 打包。执行顺序：
   - `desktop.py` 增加 frozen 分支（3.1）
   - 编写 `mask-tool.spec` 并在 venv 安装 `pyinstaller`
   - 打包 → 逐功能回归测试（文件解析、脱敏、下载保存）
   - 可选：Inno Setup 制作安装包
3. **体积敏感**（如需经聊天工具传输）：优先评估 jieba 换小型分词、pandas 换轻量 CSV 处理，可将体积压到 150 MB 量级再打包。
