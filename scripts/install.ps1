# awen Agent 一键安装（Windows PowerShell）。
# 用法：
#   iwr https://raw.githubusercontent.com/zheng-zhengwen/awen-agent/main/scripts/install.ps1 -UseBasicParsing | iex
# 可选环境变量：
#   $env:AWEN_VERSION = "latest" 或 "v0.5.5"
#   $env:AWEN_REF     = "main" / tag，从 git 安装
#   $env:AWEN_LOCAL   = "C:\path\to\repo"，从本地安装
#   $env:AWEN_AUTO_INSTALL = "1"，缺 Python/pipx 时尽量自动安装；默认 1
#   $env:GITHUB_TOKEN  = 私有仓库读取 release 时可用
#   $env:PIP_INDEX_URL = pip 镜像
#   $env:AWEN_WITH_SEMANTIC = "1"，同时安装本地语义检索依赖 sentence-transformers
$ErrorActionPreference = "Stop"

# 让中文提示在 GBK 控制台也不乱码；并在老 PowerShell 上启用 TLS 1.2，否则 GitHub 连接会失败。
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}
try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}

$ownerRepo = if ($env:AWEN_GITHUB_REPO) { $env:AWEN_GITHUB_REPO } else { "zheng-zhengwen/awen-agent" }
$repo = if ($env:AWEN_REPO) { $env:AWEN_REPO } else { "https://github.com/$ownerRepo.git" }
$version = if ($env:AWEN_VERSION) { $env:AWEN_VERSION } else { "latest" }
$ref  = if ($env:AWEN_REF)  { $env:AWEN_REF }  else { "" }
$autoInstall = if ($env:AWEN_AUTO_INSTALL) { $env:AWEN_AUTO_INSTALL } else { "1" }
$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$wheelhouse = if ($env:AWEN_WHEELHOUSE) { $env:AWEN_WHEELHOUSE } else { Join-Path $scriptDir "wheelhouse" }

function Info($m) { Write-Host "[awen] $m" -ForegroundColor Green }
function Die($m)  { Write-Host "[awen] $m" -ForegroundColor Red; exit 1 }

function Install-FromWheelhouse($wheel, $wheelhouse) {
  $installDir = if ($env:AWEN_INSTALL_DIR) { $env:AWEN_INSTALL_DIR } else { Join-Path $HOME ".awen\runtime" }
  $binDir = if ($env:AWEN_BIN_DIR) { $env:AWEN_BIN_DIR } else { Join-Path $HOME ".awen\bin" }
  $venvPython = Join-Path $installDir "Scripts\python.exe"
  $venvPip = Join-Path $installDir "Scripts\pip.exe"
  $launcher = Join-Path $binDir "awen.cmd"

  Info "发现离线依赖包：$wheelhouse"
  Info "安装到本地运行环境：$installDir"
  & $py -m venv --clear $installDir
  & $venvPip install --no-index --find-links $wheelhouse $wheel.FullName
  $semanticMarker = Join-Path $wheelhouse ".awen-semantic"
  if (($env:AWEN_WITH_SEMANTIC -eq "1") -or (Test-Path $semanticMarker)) {
    Info "安装本地语义检索依赖（sentence-transformers）…"
    & $venvPip install --no-index --find-links $wheelhouse "sentence-transformers>=3.0"
  }

  New-Item -ItemType Directory -Force -Path $binDir | Out-Null
  "@echo off`r`n`"$venvPython`" -m awen_agent.cli %*`r`n" | Set-Content -Encoding ASCII $launcher

  $semanticManifest = Join-Path $scriptDir "semantic-manifest.json"
  if (Test-Path $semanticManifest) {
    $manifest = Get-Content $semanticManifest -Raw | ConvertFrom-Json
    $src = Join-Path $scriptDir $manifest.model_dir
    if (-not (Test-Path $src)) { Die "离线 embedding 模型目录不存在：$src" }
    $dstRoot = if ($env:AWEN_EMBEDDING_MODEL_DIR) { $env:AWEN_EMBEDDING_MODEL_DIR } else { Join-Path $HOME ".awen\models\embedding" }
    $dst = Join-Path $dstRoot $manifest.name
    New-Item -ItemType Directory -Force -Path $dstRoot | Out-Null
    if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
    Copy-Item -Recurse -Force $src $dst
    Info "配置离线本地语义检索模型：$($manifest.model) -> $dst"
    & $launcher retrieval embeddings --backend sentence-transformers --model $manifest.model --model-path $dst --no-download --json | Out-Null
  }

  Info "✓ 离线安装完成。"
  Info "如果 awen 不能直接执行，把这个目录加入 PATH：$binDir"
  & $launcher self doctor
  Info "初始化本地检索索引…"
  & $launcher retrieval sync --json | Out-Null
  Info "下一步：awen config，然后 awen chat"
}

# 1) Python
$py = $null
foreach ($c in @("py", "python", "python3")) {
  if (Get-Command $c -ErrorAction SilentlyContinue) { $py = $c; break }
}
if (-not $py) {
  if (($autoInstall -eq "1") -and (Get-Command winget -ErrorAction SilentlyContinue)) {
    Info "未发现 Python，尝试通过 winget 安装 Python 3…"
    winget install --id Python.Python.3 --source winget --accept-package-agreements --accept-source-agreements
    foreach ($c in @("py", "python", "python3")) {
      if (Get-Command $c -ErrorAction SilentlyContinue) { $py = $c; break }
    }
  }
}
if (-not $py) { Die "需要 Python 3.9+（从 python.org 安装，勾选 Add to PATH）。" }
Info "使用 Python：$py"

$localWheel = $null
if (Test-Path $wheelhouse) {
  $localWheel = Get-ChildItem $wheelhouse -Filter "*.whl" |
    Where-Object { $_.Name -like "awen_agent-*.whl" -or $_.Name -like "awen-agent-*.whl" } |
    Sort-Object Name |
    Select-Object -Last 1
}

if ($localWheel) {
  Install-FromWheelhouse $localWheel $wheelhouse
  exit 0
}

# 2) 在线安装确保 pipx
if (-not (Get-Command pipx -ErrorAction SilentlyContinue)) {
  Info "未发现 pipx，正在安装…"
  & $py -m pip install --user pipx
  & $py -m pipx ensurepath
  $env:Path = "$env:APPDATA\Python\Scripts;$env:LOCALAPPDATA\Programs\Python\Scripts;$env:Path"
}

# 通过 github.com 的 releases/latest 跳转解析最新 tag —— 不走限流严重的 api.github.com，
# 国内/共享出口 IP 常因 API 限流拿到 403，这条路几乎不会。
function Resolve-LatestTag($ownerRepo) {
  try {
    $req = [System.Net.HttpWebRequest]::Create("https://github.com/$ownerRepo/releases/latest")
    $req.AllowAutoRedirect = $false
    $req.UserAgent = "awen-install"
    $req.Timeout = 30000
    $resp = $req.GetResponse()
    $loc = $resp.Headers["Location"]
    $resp.Close()
    if ($loc -and ($loc -match "/tag/([^/?#]+)")) { return $Matches[1] }
  } catch {}
  return $null
}

# API 发现（私有仓库带 token、或资产命名不符约定时的兜底）。
function Get-ReleaseWheelUrl($ownerRepo, $version) {
  $api = "https://api.github.com/repos/$ownerRepo/releases"
  $url = if (($version -eq "") -or ($version -eq "latest")) { "$api/latest" } else { "$api/tags/$version" }
  $headers = @{ "Accept" = "application/vnd.github+json"; "User-Agent" = "awen-install" }
  if ($env:GITHUB_TOKEN) { $headers["Authorization"] = "Bearer $env:GITHUB_TOKEN" }
  $release = Invoke-RestMethod -Uri $url -Headers $headers -TimeoutSec 30
  $asset = $release.assets | Where-Object { $_.name -like "*.whl" } | Select-Object -First 1
  if (-not $asset) { throw "release 中没有 wheel 资产" }
  return $asset.browser_download_url
}

function Get-InstallSpec($ownerRepo, $repo, $version, $ref) {
  if ($env:AWEN_LOCAL) { return $env:AWEN_LOCAL }
  if ($ref) { return "git+$repo@$ref" }

  $tag = if ($version -and ($version -ne "latest")) { $version } else { Resolve-LatestTag $ownerRepo }

  # 1) 直接从 releases/download 下载 wheel —— 不依赖 api.github.com，规避 403。
  if ($tag) {
    $ver = $tag -replace '^v', ''
    $wheelName = "awen_agent-$ver-py3-none-any.whl"
    $wheelUrl = "https://github.com/$ownerRepo/releases/download/$tag/$wheelName"
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) $wheelName
    try {
      Info "下载 Release wheel：$tag …"
      $iwr = @{ Uri = $wheelUrl; OutFile = $tmp; UseBasicParsing = $true; TimeoutSec = 180 }
      if ($env:GITHUB_TOKEN) { $iwr["Headers"] = @{ Authorization = "Bearer $env:GITHUB_TOKEN" } }
      Invoke-WebRequest @iwr
      return $tmp
    } catch {
      Info "直接下载 wheel 失败（$($_.Exception.Message)），改用 GitHub API…"
    }
  }

  # 2) API 兜底
  try { return (Get-ReleaseWheelUrl $ownerRepo $version) } catch {
    Info "Release API 不可用：$($_.Exception.Message)"
  }

  # 3) git 兜底：优先用解析到的 tag（uv 对 tag 解析正确；用分支名 main 在 uv 下会被当成 tag 而失败）。
  if ($tag) { Info "回退到 git tag 安装：$tag"; return "git+$repo@$tag" }
  Info "回退到 git main 分支安装。私有仓库请配置 GitHub 凭据或 GITHUB_TOKEN/AWEN_REF。"
  return "git+$repo@refs/heads/main"
}

# 3) 安装 / 升级
Info "查找安装来源（$ownerRepo@$version）…"
$spec = Get-InstallSpec $ownerRepo $repo $version $ref
Info "安装 awen-agent（来源：$spec）…"
& $py -m pipx install --force $spec
if ($env:AWEN_WITH_SEMANTIC -eq "1") {
  Info "安装本地语义检索依赖（sentence-transformers）…"
  & $py -m pipx inject awen-agent "sentence-transformers>=3.0"
}

# pipx 把 awen 装进隔离 venv，并放一个启动器到 bin 目录；用它做安装后自检，
# 不要用系统 python -m awen_agent.cli（那个 venv 之外没有这个模块）。
$pipxBin = if ($env:PIPX_BIN_DIR) { $env:PIPX_BIN_DIR } else { Join-Path $HOME ".local\bin" }
$awenExe = Join-Path $pipxBin "awen.exe"
if (-not (Test-Path $awenExe)) { $awenExe = "awen" }

Info "✓ 安装完成。若 awen 不能直接执行，请重开 PowerShell（PATH 已加入 $pipxBin）。"
try { & $awenExe self doctor } catch { Info "安装后诊断未运行：$($_.Exception.Message)" }
try { & $awenExe retrieval sync --json | Out-Null } catch { Info "检索索引初始化未运行：$($_.Exception.Message)" }
Info "  awen config   然后  awen chat"
