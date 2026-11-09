param(
  [int]$Port = 8765,
  [switch]$NoReload
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
  $bootstrap = $null
  $bootstrapArgs = @()
  if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3.12 -c "import sys" 2>$null
    if ($LASTEXITCODE -eq 0) {
      $bootstrap = "py"
      $bootstrapArgs = @("-3.12")
    }
  }
  if (-not $bootstrap) {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) {
      throw "未找到 Python。请先安装 Python 3.11 或更高版本。"
    }
    $bootstrap = $python.Source
  }
  & $bootstrap @bootstrapArgs -m venv .venv
}

& $venvPython -m pip install -q -r requirements.txt
& $venvPython -m playwright install chromium

$serverArgs = @(
  "-m", "uvicorn", "app.main:app",
  "--host", "0.0.0.0",
  "--port", "$Port"
)
if (-not $NoReload) {
  $serverArgs += "--reload"
}

Write-Host ""
Write-Host "拾影已启动：" -ForegroundColor Green
Write-Host "  本机   http://127.0.0.1:$Port"
Write-Host "  手机   http://<你的局域网 IP>:$Port"
Write-Host "  接口文档 http://127.0.0.1:$Port/docs"
Write-Host ""

& $venvPython @serverArgs