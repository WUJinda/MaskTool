# mask-tool 打包发布流程（标准化 v1）

> 适用场景：功能改动后需要重新生成离线部署包。本流程已实测跑通，一条命令完成
> 测试 → 打包 → 自动验证 → 出版本化便携包。
> 配套文件：`scripts/build-release.ps1`（一键脚本）、`mask-tool.spec`（打包配置）。

---

## 一、标准发布流程（改完代码后）

```powershell
# 在项目根目录 E:\Project\mask-tool 下执行
powershell -ExecutionPolicy Bypass -File scripts\build-release.ps1
```

脚本自动完成 5 步（全程约 2 分钟）：

| 步骤 | 动作 | 失败行为 |
| --- | --- | --- |
| 0 | 结束正在运行的 mask-tool.exe（否则文件被占用） | — |
| 1 | 运行 pytest 全量测试 | 测试不过即中止 |
| 2 | PyInstaller 打包（`--clean` 全新构建，约 40 秒） | 构建失败即中止 |
| 3 | **自动验证**：启动刚打包的 exe，轮询 streamlit 健康检查（60 秒内需返回 200），验证完自动关闭 | 未就绪即中止，并提示看日志 |
| 4 | 生成版本号命名的便携包 | — |
| 5 | 输出产物路径与体积 | — |

**产物**：
- `dist\mask-tool\` —— 绿色目录（开发机上直接双击 `mask-tool.exe` 试用）
- `dist\mask-tool-portable-v<版本号>.zip` —— 拷去离线机的部署包（约 113MB）
- 版本号自动读取 `pyproject.toml` 的 `version` 字段；**发新版本前记得先改它**

可选参数：`-SkipTests`（跳过测试）、`-SkipVerify`（跳过自动验证，不建议）。

## 二、首次环境准备（一次性）

新开发机或重建 venv 后需要：

```powershell
cd E:\Project\mask-tool
.venv\Scripts\python.exe -m pip install -e ".[app]"   # 项目依赖（venv 已有则跳过）
.venv\Scripts\python.exe -m pip install pyinstaller    # 打包工具（2026-09 已装 6.22.3）
```

## 三、发布给离线机（部署方操作）

1. 拷贝 `dist\mask-tool-portable-v<版本号>.zip` 到目标机（U 盘 / 内网共享）
2. 解压到任意目录（如 `D:\mask-tool\`）
3. 双击 `mask-tool.exe`；建议右键 → 发送到 → 桌面快捷方式
4. 目标机要求：Windows 10/11（WebView2 随 Edge 自带），无需 Python、无需联网

**升级已有部署**：关掉正在运行的窗口 → 用新版 zip 解压覆盖原目录（或解压为新目录后删旧目录）。用户数据（批次历史等）在 `%USERPROFILE%\.mask-tool\`，与程序目录无关，覆盖升级不会丢。

**自定义词库**：编辑 `<程序目录>\_internal\config\lexicon.yaml`（内置词库已随包带上）。

## 四、何时需要改 `mask-tool.spec`

| 改动类型 | spec 需要做什么 |
| --- | --- |
| 普通 Python 代码改动（src/ 内） | **不需要动**，重新跑脚本即可 |
| `pyproject.toml` 新增依赖包 | 通常不需要（Analysis 自动分析 import）；若新包含数据文件或启动时查自身版本，见下两行 |
| 新包启动时报 `PackageNotFoundError` | `_metadata` 循环的包列表里加上该包名（copy_metadata） |
| 新包带静态资源（词典/前端/模板） | `datas` 里补 `collect_data_files("包名")` |
| 新增词库/模板/图标等数据文件 | `datas` 里补 `("源路径", "目标目录")` |
| 运行报 `ModuleNotFoundError` 且模块名在代码里是动态导入 | `hiddenimports` 里补模块名 |

## 五、常见问题排查（实测踩坑实录）

**产物 exe 启动后窗口一直不出现 / 白等**：
看 `%LOCALAPPDATA%\mask-tool\` 下三个日志——
- `streamlit-child-error.log`：streamlit 子进程崩溃的完整 traceback
- `streamlit.log`：streamlit 运行日志（stderr 重定向）
- `desktop-error.log`：主进程（pywebview 侧）异常

| 症状（日志内容） | 原因 | 修复 |
| --- | --- | --- |
| `PackageNotFoundError: No package metadata was found for xxx` | PyInstaller 未收集该包元数据 | spec `_metadata` 列表加包名（已修：streamlit 全链 17 个包） |
| `server.port does not work when global.developmentMode is true` | frozen 环境下 streamlit 误判开发模式 | 已在 desktop.py 修复（显式 `--global.developmentMode false`） |
| `ModuleNotFoundError` | 动态导入未被静态分析发现 | spec `hiddenimports` 补模块 |
| 打包/压缩时报文件被占用 | exe 还在运行 | 脚本第 0 步会自动处理；手动跑压缩前先关窗口 |
| 健康检查超时但日志显示 Uvicorn 已启动 | 系统代理拦截 localhost（企业环境常见） | 脚本已禁用会话代理；手动验证时注意同问题 |

## 六、架构说明（为什么这样打包）

- **入口**：`src/mask_tool/desktop.py` 直接作为 PyInstaller 入口，`console=False` 等效 pythonw 无窗口
- **streamlit 子进程**：打包后 exe 不支持 `-m`，desktop.py 在 frozen 模式下以
  `mask-tool.exe --mt-streamlit-server --server.port ...` 重启自身作为 streamlit
  server 子进程（保留"关窗即全部退出"的原有架构）。开发模式（venv）行为不变
- **app.py 定位**：frozen 下从 `sys._MEIPASS`（即 `_internal\`）解析
- **配置/词库**：打入 `_internal\config\`，由 config_loader 的锚点链自动找到

## 七、已知限制

1. UI"词库管理-添加词条"在打包版不生效（读写路径不同源，即代码审查 P1-1）；
   打包版改词库请直接编辑 `_internal\config\lexicon.yaml`。修复 P1-1 后此项自动消除
2. 体积约 267MB（streamlit 前端 + pandas + pymupdf 为主），无进一步压缩空间除非裁依赖
3. onefile 模式不采用：每次启动需解压数百 MB，冷启动显著变慢
