# 一键初始化并启动模拟服务
#
#   pwsh run.ps1 -Init                 # 建 venv、装依赖、生成夹具与示例图
#   pwsh run.ps1                       # 启动（0.0.0.0:18421，局域网可真机访问）
#   pwsh run.ps1 -Reload               # 开发模式，改动自动重载
#   pwsh run.ps1 -Port 8001            # 换端口
[CmdletBinding()]
param(
    [int]$Port = 18421,
    [string]$Bind = "0.0.0.0",
    [switch]$Reload,
    [switch]$Init,
    [switch]$Check
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"

if ($Init -or -not (Test-Path $venvPython)) {
    Write-Host "创建虚拟环境 .venv ..."
    python -m venv .venv
    & $venvPython -m pip install --upgrade pip
    & $venvPython -m pip install -r requirements.txt
}

if ($Init) {
    Write-Host "生成夹具与示例图片 ..."
    & $venvPython scripts\generate_fixtures.py
    & $venvPython scripts\generate_sample_images.py
}

if ($Check) {
    Write-Host "校验契约 ..."
    & $venvPython scripts\contract_diff.py
    exit $LASTEXITCODE
}

# .env 由 uvicorn 自己加载（uvicorn[standard] 自带 python-dotenv）；
# 设备变量优先级更高，方便临时覆盖。
$envFile = Join-Path $PSScriptRoot ".env"
$arguments = @("-m", "uvicorn", "app.main:app", "--host", $Bind, "--port", "$Port")
if (Test-Path -LiteralPath $envFile) {
    Write-Host "已加载配置 $envFile"
    $arguments += @("--env-file", $envFile)
}
if ($Reload) { $arguments += "--reload" }

Write-Host "启动模拟服务 http://127.0.0.1:$Port （文档 /docs，健康检查 /health）"
& $venvPython @arguments
