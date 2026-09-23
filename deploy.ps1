# ============================================================
#  Qwen3-TTS 一键环境部署脚本（修正版 v7）
#  适用: Windows 10/11 + NVIDIA GPU (>=6GB VRAM, >= sm_61) + Python 3.11
#  编码: UTF-8 with BOM
#  修订:
#    · 步骤 2.5 修正 torch 分支判断逻辑
#    · 步骤 4 用 uv 安装时锁定 torch 三元组 + transformers 版本
#    · 步骤 4.5 校验版本一致性，不符合则强制修正
#    · PyTorch 走上海交大镜像
# ============================================================

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$VenvPath    = Join-Path $ProjectRoot ".venv"
$ModelsPath  = Join-Path $ProjectRoot "models"

# ---------- 镜像配置 ----------
$PipMirror      = "https://pypi.tuna.tsinghua.edu.cn/simple"
$PipExtraMirror = "https://mirrors.aliyun.com/pypi/simple"
$HfMirror       = "https://hf-mirror.com"

# PyTorch wheels 专用镜像（上海交大）
# 备选：南京大学 https://mirrors.nju.edu.cn/pytorch/whl
#      阿里云   https://mirrors.aliyun.com/pytorch-wheels
$TorchMirrorBase = "https://mirror.sjtu.edu.cn/pytorch-wheels"

$env:PIP_INDEX_URL       = $PipMirror
$env:PIP_EXTRA_INDEX_URL = $PipExtraMirror
$env:PIP_TRUSTED_HOST    = "pypi.tuna.tsinghua.edu.cn mirrors.aliyun.com"
$env:HF_ENDPOINT         = $HfMirror
$env:MODELSCOPE_DOMAIN   = "https://www.modelscope.cn"

function Write-Step($msg) {
    Write-Host ""
    Write-Host "============================================" -ForegroundColor Cyan
    Write-Host "  $msg" -ForegroundColor Cyan
    Write-Host "============================================" -ForegroundColor Cyan
}

# ============================================================
#  步骤 0：智能硬件检测
# ============================================================
Write-Step "步骤 0/6：检测 NVIDIA GPU"

$gpuInfo = $null
try {
    $gpuInfo = & nvidia-smi --query-gpu=name,memory.total,compute_cap --format=csv,noheader 2>$null
} catch { }

if (-not $gpuInfo) {
    Write-Host ""
    Write-Host "[错误] 未检测到 NVIDIA GPU！" -ForegroundColor Red
    Write-Host "       本 TTS 项目依赖 CUDA 加速，暂不支持 CPU 运行。" -ForegroundColor Red
    Write-Host "       请确认已安装 NVIDIA 显卡驱动，并确保 nvidia-smi 在 PATH 中。" -ForegroundColor Red
    Read-Host "按回车键退出"
    exit 1
}

$gpuLines = @($gpuInfo | Where-Object { $_ -match '\S' })
Write-Host "[OK] 检测到 $($gpuLines.Count) 块 NVIDIA GPU：" -ForegroundColor Green

$allPass         = $true
$failedGpus      = @()
$needsLegacyCuda = $false

foreach ($line in $gpuLines) {
    $parts = $line -split ',\s*'
    if ($parts.Count -lt 3) { continue }

    $gpuName = $parts[0].Trim()
    $memStr  = $parts[1].Trim()
    $capStr  = $parts[2].Trim()

    $memMiB = 0
    if ($memStr -match '(\d+)') { $memMiB = [int]$Matches[1] }
    $memGB = [math]::Round($memMiB / 1024, 1)

    $capValue = 0.0
    if ($capStr -match '(\d+\.\d+)') { $capValue = [double]$Matches[1] }

    Write-Host "  · $gpuName | 显存: ${memGB} GB | 计算能力: $capValue" -ForegroundColor Gray

    if ($memMiB -lt 6144) {
        $allPass = $false
        $failedGpus += "  x $gpuName : 显存仅 ${memGB} GB，低于最低要求 6 GB"
        continue
    }

    if ($capValue -lt 6.1) {
        $allPass = $false
        $failedGpus += "  x $gpuName : 计算能力 $capValue，低于最低要求 6.1"
        continue
    }

    if ($capValue -lt 7.5) {
        $needsLegacyCuda = $true
    }
}

if (-not $allPass) {
    Write-Host ""
    Write-Host "============================================" -ForegroundColor Red
    Write-Host "  [不满足最低硬件要求，安装已中断]" -ForegroundColor Red
    Write-Host "============================================" -ForegroundColor Red
    Write-Host ""
    Write-Host "以下 GPU 未通过检测：" -ForegroundColor Yellow
    $failedGpus | ForEach-Object { Write-Host $_ -ForegroundColor Yellow }
    Write-Host ""
    Write-Host "Qwen3-TTS 1.7B 模型的最低硬件要求：" -ForegroundColor Cyan
    Write-Host "  · 显存 (VRAM)：>= 6 GB（推荐 8 GB+）" -ForegroundColor White
    Write-Host "  · 计算能力：>= 6.1（Pascal 架构起步）" -ForegroundColor White
    Write-Host ""
    Write-Host "建议：" -ForegroundColor Cyan
    Write-Host "  · 更换显存 >= 8 GB 的 NVIDIA 显卡（RTX 3060 12G / RTX 4060 Ti 16G）" -ForegroundColor White
    Write-Host "  · 或改用 Qwen3-TTS-12Hz-0.6B 轻量版模型（需 3.5~4 GB 显存）" -ForegroundColor White
    Write-Host "  · 或使用云端 GPU 服务" -ForegroundColor White
    Write-Host ""
    Read-Host "按回车键退出"
    exit 1
}

Write-Host "[OK] 所有 GPU 均满足最低硬件要求" -ForegroundColor Green

# ---------- 根据 GPU 计算能力选择 CUDA / torch 组合 ----------
if ($needsLegacyCuda) {
    $TorchCuda      = "cu118"
    $TorchVersion   = "2.4.1"
    $TorchvisionVer = "0.19.1"
    $TorchaudioVer  = "2.4.1"
    Write-Host "[INFO] 检测到 Pascal 架构 GPU，使用 CUDA 11.8 + torch 2.4.1" -ForegroundColor Yellow
} else {
    $TorchCuda      = "cu126"
    $TorchVersion   = "2.6.0"
    $TorchvisionVer = "0.21.0"
    $TorchaudioVer  = "2.6.0"
    Write-Host "[INFO] 使用 CUDA 12.6 + torch 2.6.0（支持 sm_75+）" -ForegroundColor Yellow
}
$TorchOfficial = "$TorchMirrorBase/$TorchCuda"

# qwen-tts 强制依赖的 transformers 版本
$RequiredTransformers = "4.57.3"

Write-Host ""
Write-Host "驱动信息：" -ForegroundColor Cyan
& nvidia-smi | Select-String "CUDA Version" | ForEach-Object { Write-Host "  $_" -ForegroundColor Gray }

# ============================================================
#  步骤 1：检查 Python 3.11
# ============================================================
Write-Step "步骤 1/6：检查 Python 3.11"

$pythonExe = $null

$scoopPython = "$env:USERPROFILE\scoop\apps\python311\current\python.exe"
if (Test-Path $scoopPython) { $pythonExe = $scoopPython }

if (-not $pythonExe) {
    $found = Get-Command python -ErrorAction SilentlyContinue
    if ($found) {
        $ver = & python --version 2>&1
        if ($ver -match "3\.11") { $pythonExe = $found.Source }
    }
}

if (-not $pythonExe) {
    Write-Host "[!] 未找到 Python 3.11，正在通过 scoop 安装..." -ForegroundColor Yellow

    if (-not (Get-Command scoop -ErrorAction SilentlyContinue)) {
        Write-Host "[错误] 未安装 scoop。请先安装 scoop（https://scoop.sh）后重试。" -ForegroundColor Red
        Write-Host "       安装命令：irm get.scoop.sh | iex" -ForegroundColor Yellow
        Read-Host "按回车键退出"
        exit 1
    }

    Write-Host "配置 scoop 国内镜像（中科大）..." -ForegroundColor Yellow
    scoop config scoop_bucket "https://mirrors.ustc.edu.cn/scoop"
    scoop config scoop_repo  "https://mirrors.ustc.edu.cn/scoop"

    scoop bucket add versions 2>$null
    scoop install versions/python311

    if (Test-Path $scoopPython) {
        $pythonExe = $scoopPython
        Write-Host "[OK] Python 3.11 安装完成" -ForegroundColor Green
    } else {
        Write-Host "[错误] Python 3.11 安装失败，请手动安装后重试。" -ForegroundColor Red
        Read-Host "按回车键退出"
        exit 1
    }
}

Write-Host "[OK] 使用 Python: $pythonExe" -ForegroundColor Green
& $pythonExe --version

# ============================================================
#  步骤 2：创建 .venv
# ============================================================
Write-Step "步骤 2/6：创建虚拟环境 .venv"

if (Test-Path "$VenvPath\Scripts\python.exe") {
    $venvVer = & "$VenvPath\Scripts\python.exe" --version 2>&1
    if ($venvVer -match "3\.11") {
        Write-Host "[OK] 已存在可用的 .venv（$venvVer），跳过创建。" -ForegroundColor Green
    } else {
        Write-Host "[!] 现有 .venv 版本不匹配（$venvVer），正在重建..." -ForegroundColor Yellow
        Remove-Item -Recurse -Force $VenvPath
        & $pythonExe -m venv $VenvPath
        Write-Host "[OK] .venv 重建完成" -ForegroundColor Green
    }
} else {
    Write-Host "正在创建 .venv ..."
    & $pythonExe -m venv $VenvPath
    Write-Host "[OK] .venv 创建完成" -ForegroundColor Green
}

$venvPython = "$VenvPath\Scripts\python.exe"
$venvPip    = "$VenvPath\Scripts\pip.exe"

$pipIni = @"
[global]
index-url = $PipMirror
extra-index-url = $PipExtraMirror
trusted-host = pypi.tuna.tsinghua.edu.cn
               mirrors.aliyun.com
timeout = 120
"@
Set-Content -Path "$VenvPath\pip.ini" -Value $pipIni -Encoding ASCII
Write-Host "[OK] 已写入 $VenvPath\pip.ini（国内镜像）" -ForegroundColor Green

& $venvPython -m pip install --upgrade pip --quiet

# ============================================================
#  步骤 2.5：检查已安装 torch（修正后的判断逻辑）
# ============================================================
Write-Host ""
Write-Host "检查已安装的 torch ..." -ForegroundColor Cyan

$skipTorchInstall = $false

$installedTorch = $null
try {
    $installedTorch = (& $venvPython -c "import torch; print(torch.__version__)" 2>$null) -join ""
    $installedTorch = $installedTorch.Trim()
} catch {
    $installedTorch = $null
}

if ($installedTorch -and $installedTorch -match '\S') {
    $installedTag = $null
    if ($installedTorch -match '\+(cu\d+)') {
        $installedTag = $Matches[1]
    }

    Write-Host "  已安装版本: $installedTorch" -ForegroundColor Gray
    Write-Host "  已安装标签: $installedTag" -ForegroundColor Gray
    Write-Host "  目标标签:   $TorchCuda" -ForegroundColor Gray

    if ($installedTag -eq $TorchCuda) {
        Write-Host "[OK] torch 已是目标 CUDA 分支 ($installedTag)，跳过卸载与重装。" -ForegroundColor Green
        $skipTorchInstall = $true
    } else {
        Write-Host "[!] 已安装 torch 分支 ($installedTag) 与目标 ($TorchCuda) 不一致，将卸载重装。" -ForegroundColor Yellow
        & $venvPip uninstall -y torch torchvision torchaudio --quiet
        Write-Host "[OK] 旧版 torch 已卸载" -ForegroundColor Green
    }
} else {
    Write-Host "[OK] 未检测到已安装的 torch，将全新安装。" -ForegroundColor Green
}

# ============================================================
#  步骤 3：安装 PyTorch (CUDA) —— 走上海交大镜像
# ============================================================
if ($skipTorchInstall) {
    Write-Step "步骤 3/6：安装 PyTorch（已满足目标版本，跳过）"

    $torchVer  = (& $venvPython -c "import torch; print(torch.__version__)" 2>&1) -join ""
    $cudaCheck = (& $venvPython -c "import torch; print(torch.cuda.is_available())" 2>&1) -join ""

    Write-Host "torch 版本: $torchVer" -ForegroundColor Gray

    if ($cudaCheck -ne "True") {
        Write-Host ""
        Write-Host "[错误] 已安装 torch 但 cuda.is_available() = False" -ForegroundColor Red
        Write-Host "       请手动重装或按下方建议排查。" -ForegroundColor Red
        Write-Host ""
        Write-Host "  手动重装命令：" -ForegroundColor Cyan
        Write-Host "     Remove-Item '$VenvPath\pip.ini' -Force" -ForegroundColor Gray
        Write-Host "     `$env:PIP_INDEX_URL=`$null; `$env:PIP_EXTRA_INDEX_URL=`$null; `$env:PIP_CONFIG_FILE='NUL'" -ForegroundColor Gray
        Write-Host "     $venvPip install --no-cache-dir --force-reinstall torch==$TorchVersion torchvision==$TorchvisionVer torchaudio==$TorchaudioVer --index-url $TorchOfficial" -ForegroundColor Gray
        Write-Host ""
        Read-Host "按回车键退出"
        exit 1
    }

    Write-Host "[OK] torch.cuda.is_available() = True" -ForegroundColor Green
} else {
    Write-Step "步骤 3/6：安装 PyTorch ($TorchCuda)"

    if ($TorchCuda -notin @("cu118", "cu126")) {
        Write-Host "[错误] 未知的 CUDA 分支: $TorchCuda" -ForegroundColor Red
        Read-Host "按回车键退出"
        exit 1
    }

    Write-Host "目标组合：torch $TorchVersion / torchvision $TorchvisionVer / torchaudio $TorchaudioVer" -ForegroundColor Yellow
    Write-Host "CUDA 分支：$TorchCuda" -ForegroundColor Yellow
    Write-Host "镜像源：  $TorchOfficial" -ForegroundColor Gray
    Write-Host "（安装阶段将临时移除 pip.ini 并禁用 pip 配置文件）" -ForegroundColor Gray

    $pipIniPath   = "$VenvPath\pip.ini"
    $pipIniBackup = "$VenvPath\pip.ini.deploy-backup"
    $pipIniMoved  = $false
    if (Test-Path $pipIniPath) {
        if (Test-Path $pipIniBackup) { Remove-Item $pipIniBackup -Force }
        Rename-Item -Path $pipIniPath -NewName (Split-Path $pipIniBackup -Leaf)
        $pipIniMoved = $true
        Write-Host "[OK] 已临时移除 $pipIniPath" -ForegroundColor Green
    }

    $savedIndexUrl    = $env:PIP_INDEX_URL
    $savedExtraIndex  = $env:PIP_EXTRA_INDEX_URL
    $savedTrustedHost = $env:PIP_TRUSTED_HOST
    $savedConfigFile  = $env:PIP_CONFIG_FILE
    $env:PIP_INDEX_URL       = $null
    $env:PIP_EXTRA_INDEX_URL = $null
    $env:PIP_TRUSTED_HOST    = $null
    $env:PIP_CONFIG_FILE     = "NUL"

    try {
        Write-Host ""
        Write-Host "执行：pip install --no-cache-dir --force-reinstall torch==$TorchVersion ..." -ForegroundColor Gray
        & $venvPip install --no-cache-dir --force-reinstall `
            "torch==$TorchVersion" `
            "torchvision==$TorchvisionVer" `
            "torchaudio==$TorchaudioVer" `
            --index-url $TorchOfficial
    } finally {
        $env:PIP_INDEX_URL       = $savedIndexUrl
        $env:PIP_EXTRA_INDEX_URL = $savedExtraIndex
        $env:PIP_TRUSTED_HOST    = $savedTrustedHost
        $env:PIP_CONFIG_FILE     = $savedConfigFile

        if ($pipIniMoved -and (Test-Path $pipIniBackup)) {
            Rename-Item -Path $pipIniBackup -NewName (Split-Path $pipIniPath -Leaf)
            Write-Host "[OK] 已恢复 $pipIniPath" -ForegroundColor Green
        }
    }

    Write-Host "[OK] PyTorch 安装命令执行完毕" -ForegroundColor Green

    $torchVer  = (& $venvPython -c "import torch; print(torch.__version__)" 2>&1) -join ""
    $tvVer     = (& $venvPython -c "import torchvision; print(torchvision.__version__)" 2>&1) -join ""
    $taVer     = (& $venvPython -c "import torchaudio; print(torchaudio.__version__)" 2>&1) -join ""
    $cudaVer   = (& $venvPython -c "import torch; print(torch.version.cuda)" 2>&1) -join ""
    $cudaCheck = (& $venvPython -c "import torch; print(torch.cuda.is_available())" 2>&1) -join ""

    Write-Host ""
    Write-Host "torch:        $torchVer" -ForegroundColor Gray
    Write-Host "torchvision:  $tvVer" -ForegroundColor Gray
    Write-Host "torchaudio:   $taVer" -ForegroundColor Gray
    Write-Host "torch CUDA:   $cudaVer" -ForegroundColor Gray

    $verifyOk = $true
    if ($cudaCheck -ne "True") { $verifyOk = $false }
    if ($torchVer -notmatch [regex]::Escape($TorchCuda)) { $verifyOk = $false }
    if ($tvVer -notmatch [regex]::Escape($TorchCuda)) { $verifyOk = $false }
    if ($cudaVer -eq "None" -or [string]::IsNullOrEmpty($cudaVer)) { $verifyOk = $false }

    if ($verifyOk) {
        Write-Host "[OK] PyTorch 三元组校验通过，CUDA 分支 = $cudaVer" -ForegroundColor Green
    } else {
        Write-Host ""
        Write-Host "[错误] PyTorch CUDA 环境校验失败" -ForegroundColor Red
        Write-Host "       期望 CUDA 分支: $TorchCuda" -ForegroundColor Yellow
        Write-Host "       torch:        $torchVer" -ForegroundColor Yellow
        Write-Host "       torchvision:  $tvVer" -ForegroundColor Yellow
        Write-Host "       torchaudio:   $taVer" -ForegroundColor Yellow
        Write-Host "       cuda.is_available(): $cudaCheck" -ForegroundColor Yellow
        Write-Host ""
        Write-Host "可换用以下备选镜像重试：" -ForegroundColor Cyan
        Write-Host "  南京大学  https://mirrors.nju.edu.cn/pytorch/whl/$TorchCuda" -ForegroundColor Gray
        Write-Host "  阿里云    https://mirrors.aliyun.com/pytorch-wheels/$TorchCuda" -ForegroundColor Gray
        Write-Host ""
        Read-Host "按回车键退出"
        exit 1
    }
}

# ============================================================
#  步骤 4：安装项目依赖（uv 加速 + 锁定 torch 三元组）
# ============================================================
Write-Step "步骤 4/6：安装项目依赖（uv 并行加速）"

# ---------- 4.1 安装 uv ----------
Write-Host "正在安装 uv（Rust 编写的极速包管理器）..." -ForegroundColor Yellow
& $venvPip install uv --quiet
Write-Host "[OK] uv 安装完成" -ForegroundColor Green

$venvUv = "$VenvPath\Scripts\uv.exe"

# ---------- 4.2 用 uv 安装依赖（锁定 torch 三元组 + transformers） ----------
# 关键：
#   --index-url        PyTorch 镜像（提供 +cuXXX 本地标签的版本）
#   --extra-index-url  PyPI 国内镜像（提供其他依赖）
#   --index-strategy   unsafe-best-match 允许跨源取版本最优解
#   torch 三元组与 transformers 显式锁定，防止被 uv 擅自升级
Write-Host ""
Write-Host "使用 uv 安装核心依赖（锁定 torch==$TorchVersion+$TorchCuda / transformers==$RequiredTransformers）..."
Write-Host "（uv 会并行解析并下载依赖，速度约为 pip 的 5-10 倍）" -ForegroundColor Gray

$torchPin        = "torch==$TorchVersion+$TorchCuda"
$torchvisionPin  = "torchvision==$TorchvisionVer+$TorchCuda"
$torchaudioPin   = "torchaudio==$TorchaudioVer+$TorchCuda"
$transformersPin = "transformers==$RequiredTransformers"

& $venvUv pip install `
    --python $venvPython `
    --index-url $TorchOfficial `
    --extra-index-url $PipMirror `
    --extra-index-url $PipExtraMirror `
    --index-strategy unsafe-best-match `
    $torchPin `
    $torchvisionPin `
    $torchaudioPin `
    $transformersPin `
    "qwen-tts" `
    "fastapi" `
    "uvicorn[standard]" `
    "pydantic" `
    "python-multipart" `
    "modelscope"

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "[警告] uv 安装失败，回退到 pip 逐个安装..." -ForegroundColor Yellow
    & $venvPip install $torchPin $torchvisionPin $torchaudioPin --index-url $TorchOfficial
    & $venvPip install -U "qwen-tts" --quiet
    & $venvPip install "fastapi" "uvicorn[standard]" "pydantic" "python-multipart" --quiet
    & $venvPip install -U "modelscope" --quiet
    & $venvPip install --force-reinstall $transformersPin --quiet
}

Write-Host "[OK] 核心依赖安装完成" -ForegroundColor Green

# ============================================================
#  步骤 4.5：校验关键包版本（防止被 uv / pip 擅自升级）
# ============================================================
Write-Step "步骤 4.5/6：校验关键包版本"

$torchVer2  = (& $venvPython -c "import torch; print(torch.__version__)" 2>&1) -join ""
$tvVer2     = (& $venvPython -c "import torchvision; print(torchvision.__version__)" 2>&1) -join ""
$taVer2     = (& $venvPython -c "import torchaudio; print(torchaudio.__version__)" 2>&1) -join ""
$tfVer      = (& $venvPython -c "import transformers; print(transformers.__version__)" 2>&1) -join ""
$cudaCheck2 = (& $venvPython -c "import torch; print(torch.cuda.is_available())" 2>&1) -join ""

Write-Host "torch:        $torchVer2" -ForegroundColor Gray
Write-Host "torchvision:  $tvVer2" -ForegroundColor Gray
Write-Host "torchaudio:   $taVer2" -ForegroundColor Gray
Write-Host "transformers: $tfVer" -ForegroundColor Gray

$needFixTorch = $false
$needFixTf    = $false

if ($torchVer2 -notmatch [regex]::Escape("+$TorchCuda") -or
    $tvVer2    -notmatch [regex]::Escape("+$TorchCuda") -or
    $taVer2    -notmatch [regex]::Escape("+$TorchCuda")) {
    $needFixTorch = $true
}
if ($tfVer -ne $RequiredTransformers) {
    $needFixTf = $true
}

# ---------- 修复 torch 三元组 ----------
if ($needFixTorch) {
    Write-Host ""
    Write-Host "[!] torch 三元组版本被改动，强制重装..." -ForegroundColor Yellow

    $pipIniPath   = "$VenvPath\pip.ini"
    $pipIniBackup = "$VenvPath\pip.ini.fix-backup"
    $pipIniMoved  = $false
    if (Test-Path $pipIniPath) {
        if (Test-Path $pipIniBackup) { Remove-Item $pipIniBackup -Force }
        Rename-Item -Path $pipIniPath -NewName (Split-Path $pipIniBackup -Leaf)
        $pipIniMoved = $true
    }

    $savedIndexUrl    = $env:PIP_INDEX_URL
    $savedExtraIndex  = $env:PIP_EXTRA_INDEX_URL
    $savedTrustedHost = $env:PIP_TRUSTED_HOST
    $savedConfigFile  = $env:PIP_CONFIG_FILE
    $env:PIP_INDEX_URL       = $null
    $env:PIP_EXTRA_INDEX_URL = $null
    $env:PIP_TRUSTED_HOST    = $null
    $env:PIP_CONFIG_FILE     = "NUL"

    try {
        & $venvPip install --no-cache-dir --force-reinstall `
            "torch==$TorchVersion+$TorchCuda" `
            "torchvision==$TorchvisionVer+$TorchCuda" `
            "torchaudio==$TorchaudioVer+$TorchCuda" `
            --index-url $TorchOfficial
    } finally {
        $env:PIP_INDEX_URL       = $savedIndexUrl
        $env:PIP_EXTRA_INDEX_URL = $savedExtraIndex
        $env:PIP_TRUSTED_HOST    = $savedTrustedHost
        $env:PIP_CONFIG_FILE     = $savedConfigFile

        if ($pipIniMoved -and (Test-Path $pipIniBackup)) {
            Rename-Item -Path $pipIniBackup -NewName (Split-Path $pipIniPath -Leaf)
        }
    }

    $torchVer2  = (& $venvPython -c "import torch; print(torch.__version__)" 2>&1) -join ""
    $tvVer2     = (& $venvPython -c "import torchvision; print(torchvision.__version__)" 2>&1) -join ""
    $taVer2     = (& $venvPython -c "import torchaudio; print(torchaudio.__version__)" 2>&1) -join ""
    Write-Host "  重装后 torch:        $torchVer2" -ForegroundColor Gray
    Write-Host "  重装后 torchvision:  $tvVer2" -ForegroundColor Gray
    Write-Host "  重装后 torchaudio:   $taVer2" -ForegroundColor Gray
}

# ---------- 修复 transformers ----------
if ($needFixTf) {
    Write-Host ""
    Write-Host "[!] transformers 版本为 $tfVer，强制降级到 $RequiredTransformers ..." -ForegroundColor Yellow
    & $venvPip install --force-reinstall "transformers==$RequiredTransformers" --quiet

    $tfVer = (& $venvPython -c "import transformers; print(transformers.__version__)" 2>&1) -join ""
    Write-Host "  重装后 transformers: $tfVer" -ForegroundColor Gray
}

# ---------- 最终校验 ----------
$torchVer3  = (& $venvPython -c "import torch; print(torch.__version__)" 2>&1) -join ""
$tvVer3     = (& $venvPython -c "import torchvision; print(torchvision.__version__)" 2>&1) -join ""
$taVer3     = (& $venvPython -c "import torchaudio; print(torchaudio.__version__)" 2>&1) -join ""
$tfVer3     = (& $venvPython -c "import transformers; print(transformers.__version__)" 2>&1) -join ""
$cudaCheck3 = (& $venvPython -c "import torch; print(torch.cuda.is_available())" 2>&1) -join ""

$finalOk = $true
if ($torchVer3 -notmatch [regex]::Escape("+$TorchCuda")) { $finalOk = $false }
if ($tvVer3    -notmatch [regex]::Escape("+$TorchCuda")) { $finalOk = $false }
if ($taVer3    -notmatch [regex]::Escape("+$TorchCuda")) { $finalOk = $false }
if ($tfVer3    -ne $RequiredTransformers)                { $finalOk = $false }
if ($cudaCheck3 -ne "True")                              { $finalOk = $false }

# 验证 AutoProcessor 能否导入（transformers 与 torchvision 兼容性）
$autoProcOk = $false
try {
    $autoProcResult = (& $venvPython -c "from transformers import AutoProcessor; print('OK')" 2>&1) -join ""
    if ($autoProcResult -match 'OK') { $autoProcOk = $true }
} catch { }

if ($finalOk -and $autoProcOk) {
    Write-Host "[OK] 所有关键包版本校验通过，AutoProcessor 可正常导入" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "[错误] 关键包版本校验失败" -ForegroundColor Red
    Write-Host "       torch:        $torchVer3" -ForegroundColor Yellow
    Write-Host "       torchvision:  $tvVer3" -ForegroundColor Yellow
    Write-Host "       torchaudio:   $taVer3" -ForegroundColor Yellow
    Write-Host "       transformers: $tfVer3" -ForegroundColor Yellow
    Write-Host "       cuda.is_available(): $cudaCheck3" -ForegroundColor Yellow
    Write-Host "       AutoProcessor 导入: $autoProcOk" -ForegroundColor Yellow
    Write-Host ""
    Read-Host "按回车键退出"
    exit 1
}

# ---------- 安装 flash-attn（可选） ----------
Write-Host ""
Write-Host "正在安装 flash-attn（优先使用预编译 wheel）..." -ForegroundColor Yellow

$flashAttnOk = $false
$flashWheelMap = @{
    "2.6.0" = @{
        cp311 = "https://huggingface.co/lldacing/flash-attention-windows-wheel/resolve/main/flash_attn-2.7.4.post1+cu126torch2.6.0cxx11abiFALSE-cp311-cp311-win_amd64.whl"
    }
    "2.4.1" = @{
        cp311 = "https://huggingface.co/lldacing/flash-attention-windows-wheel/resolve/main/flash_attn-2.6.3+cu118torch2.4.0cxx11abiFALSE-cp311-cp311-win_amd64.whl"
    }
}

$wheelUrl = $null
if ($flashWheelMap.ContainsKey($TorchVersion) -and $flashWheelMap[$TorchVersion].ContainsKey("cp311")) {
    $wheelUrl = $flashWheelMap[$TorchVersion]["cp311"]
}

if ($wheelUrl) {
    Write-Host "匹配到预编译 wheel，直接下载安装..." -ForegroundColor Gray
    try {
        & $venvUv pip install $wheelUrl --python $venvPython
        if ($LASTEXITCODE -eq 0) { $flashAttnOk = $true }
    } catch { }
}

if (-not $flashAttnOk) {
    Write-Host "[提示] 预编译 wheel 安装失败，尝试源码编译方式..." -ForegroundColor Yellow
    try {
        & $venvUv pip install ninja --python $venvPython
        & $venvPip install flash-attn --no-build-isolation --quiet 2>&1 | Out-Null
        $flashAttnOk = $true
    } catch { }
}

if ($flashAttnOk) {
    Write-Host "[OK] flash-attn 安装成功" -ForegroundColor Green
} else {
    Write-Host "[提示] flash-attn 安装失败或跳过（不影响基本运行）" -ForegroundColor Yellow
}

Write-Host "[OK] 依赖安装完成" -ForegroundColor Green

# ============================================================
#  步骤 5：下载模型
# ============================================================
Write-Step "步骤 5/6：下载 Qwen3-TTS 模型（ModelScope 国内源）"

if (-not (Test-Path $ModelsPath)) {
    New-Item -ItemType Directory -Path $ModelsPath | Out-Null
}

$modelscopeExe = "$VenvPath\Scripts\modelscope.exe"
$hasCli = Test-Path $modelscopeExe
if ($hasCli) {
    Write-Host "[OK] 找到 modelscope CLI: $modelscopeExe" -ForegroundColor Green
} else {
    Write-Host "[提示] 未找到 modelscope.exe，将使用 Python SDK 方式下载" -ForegroundColor Yellow
}

$models = @(
    @{ Id = "Qwen/Qwen3-TTS-Tokenizer-12Hz";        Dir = "Qwen3-TTS-Tokenizer-12Hz" },
    @{ Id = "Qwen/Qwen3-TTS-12Hz-1.7B-Base";        Dir = "Qwen3-TTS-12Hz-1.7B-Base" },
    @{ Id = "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice"; Dir = "Qwen3-TTS-12Hz-1.7B-CustomVoice" },
    @{ Id = "Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"; Dir = "Qwen3-TTS-12Hz-1.7B-VoiceDesign" }
)

foreach ($m in $models) {
    $targetDir = Join-Path $ModelsPath $m.Dir
    $marker = Join-Path $targetDir "config.json"

    if (Test-Path $marker) {
        Write-Host "[跳过] $($m.Dir) 已存在" -ForegroundColor Green
        continue
    }

    Write-Host ""
    Write-Host ">>> 正在下载 $($m.Id) ..." -ForegroundColor Yellow

    $ok = $false

    if ($hasCli) {
        try {
            & $modelscopeExe download --model $m.Id --local_dir $targetDir
            if (Test-Path $marker) { $ok = $true }
        } catch {
            Write-Host "[警告] CLI 下载失败：$($_.Exception.Message)" -ForegroundColor Yellow
        }
    }

    if (-not $ok) {
        Write-Host "[回退] 使用 Python SDK snapshot_download ..." -ForegroundColor Yellow
        $pyCode = "from modelscope import snapshot_download; snapshot_download('$($m.Id)', local_dir=r'$targetDir')"
        try {
            & $venvPython -c $pyCode
            if (Test-Path $marker) { $ok = $true }
        } catch {
            Write-Host "[警告] SDK 下载失败：$($_.Exception.Message)" -ForegroundColor Yellow
        }
    }

    if (-not $ok) {
        Write-Host "[回退] 使用 HuggingFace 镜像 (hf-mirror.com) ..." -ForegroundColor Yellow
        $hfCode = "from huggingface_hub import snapshot_download; snapshot_download('$($m.Id)', local_dir=r'$targetDir')"
        try {
            & $venvPython -c $hfCode
            if (Test-Path $marker) { $ok = $true }
        } catch {
            Write-Host "[警告] HF 镜像下载失败：$($_.Exception.Message)" -ForegroundColor Yellow
        }
    }

    if ($ok) {
        Write-Host "[OK] $($m.Dir) 下载完成" -ForegroundColor Green
    } else {
        Write-Host "[警告] $($m.Dir) 下载失败，可重跑本脚本续传。" -ForegroundColor Red
    }
}

# ============================================================
#  步骤 6：完成
# ============================================================
Write-Step "步骤 6/6：部署完成"

Write-Host ""
Write-Host "环境部署成功！" -ForegroundColor Green
Write-Host ""
Write-Host "CUDA 分支：$TorchCuda | torch：$TorchVersion | transformers：$RequiredTransformers" -ForegroundColor Cyan
Write-Host ""
Write-Host "启动方式：" -ForegroundColor Cyan
Write-Host "  双击 run.cmd  或  在 PowerShell 中执行 .\run.cmd" -ForegroundColor White
Write-Host ""
Write-Host "API 文档地址（启动后）：" -ForegroundColor Cyan
Write-Host "  http://localhost:8001/docs" -ForegroundColor White
Write-Host ""

Read-Host "按回车键退出"