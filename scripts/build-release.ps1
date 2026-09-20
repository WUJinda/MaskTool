# build-release.ps1 - mask-tool one-click release build
#
# Standard flow: stop running instance -> pytest -> PyInstaller ->
#   auto-verify (exe + health check + page-level render check) ->
#   versioned portable zip.
#
# Usage:
#   .venv\Scripts\python.exe is NOT needed; run from anywhere:
#     powershell -ExecutionPolicy Bypass -File scripts\build-release.ps1
#   Options:
#     -SkipTests    skip pytest
#     -SkipVerify   skip launching the built exe for health check
#
# Output:
#   dist\mask-tool\                      green folder (run mask-tool.exe directly)
#   dist\mask-tool-portable-v<ver>.zip   versioned portable zip for deployment

param(
    [switch]$SkipTests,
    [switch]$SkipVerify
)

$ErrorActionPreference = "Stop"

# Resolve project root (parent of scripts\)
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Fail($msg) {
    Write-Host "[FAILED] $msg" -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------- version ---
$verLine = Select-String -Path pyproject.toml -Pattern '^version\s*=\s*"(.+)"' |
    Select-Object -First 1
if (-not $verLine) { Fail "cannot read version from pyproject.toml" }
$Version = $verLine.Matches[0].Groups[1].Value
Write-Host "=== mask-tool v$Version release build ===" -ForegroundColor Cyan

# ------------------------------------------- stop running instance (locks) ---
$running = Get-Process -Name "mask-tool" -ErrorAction SilentlyContinue
if ($running) {
    Write-Host "[0/5] Stopping running mask-tool.exe (files are locked otherwise)"
    $running | Stop-Process -Force
    Start-Sleep -Seconds 2
}

# ------------------------------------------------------------------- tests ---
if (-not $SkipTests) {
    Write-Host "[1/5] Running pytest ..."
    $ErrorActionPreference = "Continue"
    & .venv\Scripts\python.exe -m pytest --tb=short -q 2>&1 |
        ForEach-Object { "$_" } | Select-Object -Last 2
    $ErrorActionPreference = "Stop"
    if ($LASTEXITCODE -ne 0) { Fail "pytest failed - aborting build" }
}

# ------------------------------------------------------------- PyInstaller ---
Write-Host "[2/5] PyInstaller build (about 40s) ..."
if (-not (Test-Path ".venv\Scripts\pyinstaller.exe")) {
    Fail "pyinstaller not found. First-time setup: .venv\Scripts\python.exe -m pip install pyinstaller"
}
if (Test-Path "dist\mask-tool") { Remove-Item "dist\mask-tool" -Recurse -Force }
# PyInstaller logs (INFO) go to stderr; PS 5.1 turns stderr lines into
# ErrorRecords which would abort under ErrorActionPreference=Stop.
# -> temporarily relax, judge success by $LASTEXITCODE only.
$ErrorActionPreference = "Continue"
& .venv\Scripts\pyinstaller.exe mask-tool.spec --noconfirm --clean 2>&1 |
    ForEach-Object { "$_" } | Select-Object -Last 3
$ErrorActionPreference = "Stop"
if ($LASTEXITCODE -ne 0) { Fail "PyInstaller build failed" }

# ------------------------------------------------------------ auto-verify ---
if (-not $SkipVerify) {
    Write-Host "[3/5] Verifying build: launch exe + streamlit health check ..."
    # corporate proxies often intercept localhost; disable for this session
    [System.Net.WebRequest]::DefaultWebProxy = $null
    Start-Process -FilePath "dist\mask-tool\mask-tool.exe" -WorkingDirectory "dist\mask-tool"
    $ok = $false
    foreach ($i in 1..30) {
        Start-Sleep -Seconds 2
        $cmdLines = @(Get-CimInstance Win32_Process -Filter "Name='mask-tool.exe'" |
            ForEach-Object { $_.CommandLine })
        $port = $null
        foreach ($cl in $cmdLines) {
            if ($cl -match "--server\.port (\d+)") { $port = $Matches[1]; break }
        }
        if ($port) {
            try {
                $r = Invoke-WebRequest "http://127.0.0.1:$port/_stcore/health" `
                    -TimeoutSec 3 -UseBasicParsing
                if ($r.StatusCode -eq 200 -and $r.Content -match "ok") {
                    $ok = $true; break
                }
            } catch { }
        }
    }
    if (-not $ok) {
        Get-Process -Name "mask-tool" -ErrorAction SilentlyContinue | Stop-Process -Force
        Fail ("health check failed (exe started but streamlit not ready in 60s). " +
              "Check %LOCALAPPDATA%\mask-tool\streamlit-child-error.log and streamlit.log")
    }
    Write-Host "       health check OK (HTTP 200)" -ForegroundColor Green

    # ---- page-level verification (2026-09-20) --------------------------------
    # Streamlit only executes the page script after a browser websocket
    # connects, and script errors (e.g. the v0.1.2 frozen ImportError caused
    # by missing mask_tool modules in PYZ) are sent to the browser only -
    # neither the health endpoint nor server logs reveal them.
    # verify_page.py loads the page in headless Edge (CDP) and asserts DOM
    # markers: no stException / sidebar present / settings component rooted.
    # Exit codes: 0=PASS, 1/2=FAIL, 3=SKIP (missing Edge or websocket-client).
    # NOTE: runs while the exe is still alive; instance closed afterwards.
    & .venv\Scripts\python.exe scripts\verify_page.py --port $port --timeout 60
    switch ($LASTEXITCODE) {
        0 { Write-Host "       page render OK (headless Edge)" -ForegroundColor Green }
        3 { Write-Host "       [WARN] page-level verify skipped (Edge or websocket-client unavailable); health check only" -ForegroundColor Yellow }
        default {
            Get-Process -Name "mask-tool" -ErrorAction SilentlyContinue | Stop-Process -Force
            Fail "page-level verify FAILED - page broken despite healthy server (see verify_page output above)"
        }
    }

    # always close the verification instance
    Get-Process -Name "mask-tool" -ErrorAction SilentlyContinue | Stop-Process -Force
}

# ------------------------------------------------------------------- zip ----
Write-Host "[4/5] Creating versioned portable zip ..."
$zip = "dist\mask-tool-portable-v$Version.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path "dist\mask-tool" -DestinationPath $zip
$zipMB = [math]::Round((Get-Item $zip).Length / 1MB, 1)

# ----------------------------------------------------------------- report ---
$dirMB = [math]::Round(
    (Get-ChildItem "dist\mask-tool" -Recurse -File |
        Measure-Object Length -Sum).Sum / 1MB)
Write-Host "[5/5] Done." -ForegroundColor Cyan
Write-Host ""
Write-Host "  green folder : dist\mask-tool           ($dirMB MB)"
Write-Host "  portable zip : $zip ($zipMB MB)"
Write-Host ""
Write-Host "  Deploy: copy the zip to the target machine, extract, run mask-tool.exe"
