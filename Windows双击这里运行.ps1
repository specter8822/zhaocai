# =================================================================================
#  Hajimi - 自动化安装与启动脚本 (PowerShell版)
# =================================================================================
#
#  此脚本旨在提供一个一键式的解决方案，用于准备运行环境并启动 Hajimi 应用
#  它会自动处理 Python 的下载、配置以及项目依赖的安装
#  确保本脚本的文件编码一定为: UTF-8 with BOM
#
# =================================================================================

# 设置执行策略，仅对当前进程有效，以确保脚本能顺利运行，而无需用户手动修改系统设置
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force

# --- 初始化与通用设置 ---

# 获取当前脚本所在的目录
$ScriptRoot = $PSScriptRoot
# 设置错误处理行为，当任何命令出错时立即停止脚本，防止后续操作在错误状态下执行
$ErrorActionPreference = "Stop"

# 定义一个日志函数，用于彩色输出，增强可读性
function Write-Log {
    param([string]$Message, [ConsoleColor]$Color = "White")
    Write-Host $Message -ForegroundColor $Color
}

# 测试网络延迟函数
function Test-NetworkLatency {
    param(
        [string]$HostName,
        [int]$Count = 4
    )
    try {
        # 使用 Test-Connection 为每次 ping 尝试进行连接
        # -ErrorAction SilentlyContinue 处理无法解析主机或完全无响应的情况
        # 返回一个包含每次成功 ping 结果的对象数组。
        $pingResults = Test-Connection -ComputerName $HostName -Count $Count -ErrorAction SilentlyContinue

        # 如果 $pingResults 不是 $null，说明至少有一次 ping 成功了。
        if ($null -ne $pingResults) {
            # 使用 Measure-Object 直接计算返回结果中 ResponseTime 属性的平均值
            $averageTime = ($pingResults | Measure-Object -Property ResponseTime -Average).Average
            return $averageTime
        }
        
        # 如果所有 ping 都失败了，$pingResults 将为 $null，我们返回 $null。
        return $null
    } catch {
        # 捕获其他意外错误。
        Write-Warning "在测试 '$HostName' 时发生意外错误: $($_.Exception.Message)"
        return $null
    }
}

# 智能源选择函数
function Get-OptimalSources {
    Write-Log "正在检测网络环境..." -Color Yellow
    
    Write-Log "测试百度延迟..." -Color Yellow
    $baiduLatency = Test-NetworkLatency -HostName "baidu.com" -Count 4
    Write-Log "测试谷歌延迟..." -Color Yellow
    $googleLatency = Test-NetworkLatency -HostName "google.com" -Count 4
    
    if ($null -ne $baiduLatency) {
        Write-Log "百度延迟: $([math]::Round($baiduLatency, 2))ms" -Color Cyan
    } else {
        Write-Log "百度连接失败" -Color Red
    }
    
    if ($null -ne $googleLatency) {
        Write-Log "谷歌延迟: $([math]::Round($googleLatency, 2))ms" -Color Cyan
    } else {
        Write-Log "谷歌连接失败" -Color Red
    }
    
    # 判断使用哪个源
    if (($null -eq $googleLatency) -or (($null -ne $baiduLatency) -and ($baiduLatency -lt $googleLatency))) {
        Write-Log "将使用华为云镜像 + 清华PyPI" -Color Green
        return @{
            PythonUrl = "https://mirrors.huaweicloud.com/python/"
            PipIndex = "https://pypi.tuna.tsinghua.edu.cn/simple/"
            SourceName = "华为云镜像 + 清华PyPI"
        }
    } else {
        Write-Log "将使用官方源" -Color Green
        return @{
            PythonUrl = "https://www.python.org/ftp/python/"
            PipIndex = "https://pypi.org/simple/"
            SourceName = "官方源"
        }
    }
}

Write-Log "===================================" -Color Cyan
Write-Log " 欢迎来到 Hajimi 自动安装程序 (PS版)" -Color Cyan
Write-Log "===================================" -Color Cyan
""

# --- 加载 .env 环境变量文件 ---
Write-Log "正在加载环境变量..."
$envFile = Join-Path $ScriptRoot ".env"
if (Test-Path $envFile) {
    # 与原版 bat 的 for /f 循环相比，此方法更适合处理文本文件
    # -Encoding UTF8 确保正确读取可能包含特殊字符的 .env 文件
    # 判断条件可安全地跳过注释行、空行或格式不正确的行，避免脚本崩溃
    Get-Content $envFile -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()
        if ($line -and !$line.StartsWith("#") -and $line.Contains("=")) {
            $key, $value = $line.Split('=', 2)
            # 使用 Set-Item 是动态设置环境变量的标准做法，因为 $env:$key 语法不支持动态变量名
            Set-Item -Path "Env:\$($key.Trim())" -Value $value.Trim()
            Write-Log "  已加载: $($key.Trim())" -Color Green
        }
    }
    Write-Log "环境变量加载成功!" -Color Green
} else {
    Write-Log "警告: 未找到 .env 文件。" -Color Yellow
}
""

# --- Python 环境准备 ---
$PythonVersion = "3.12.3"
$PythonDir = Join-Path $ScriptRoot "python"
$PythonExe = Join-Path $PythonDir "python.exe"
$GetPipPy = Join-Path $PythonDir "get-pip.py"
# 使用一个标记文件来判断依赖是否已安装，这比检查 venv 目录更适合当前场景
$InstallFlagFile = Join-Path $PythonDir ".dependencies_installed" 

# 检查是否需要执行换源逻辑
$needSourceSelection = (-not (Test-Path $PythonExe)) -or (-not (Test-Path $InstallFlagFile))

if ($needSourceSelection) {
    Write-Log "检测到需要下载 Python 或安装依赖，正在进行源选择..." -Color Yellow
    $sources = Get-OptimalSources
    Write-Log "已选择源: $($sources.SourceName)" -Color Green
    ""
} else {
    Write-Log "Python 和依赖均已安装，跳过源选择" -Color Green
    # 设置默认源（虽然不会用到）
    $sources = @{
        PythonUrl = "https://www.python.org/ftp/python/"
        PipIndex = "https://pypi.org/simple/"
        SourceName = "默认源"
    }
}

# 构建完整的 Python 下载 URL
$PythonUrl = "$($sources.PythonUrl)$PythonVersion/python-$PythonVersion-embed-amd64.zip"
$PythonZip = Join-Path $ScriptRoot "python-$PythonVersion-embed-amd64.zip"
$GetPipUrl = "https://bootstrap.pypa.io/get-pip.py"

# 检查 Python 是否已存在
if (Test-Path $PythonExe) {
    Write-Log "Python 已安装，跳过下载步骤。" -Color Green
} else {
    try {
        Write-Log "正在从 $($sources.SourceName) 下载 Python $PythonVersion..."
        Write-Log "下载地址: $PythonUrl" -Color Gray
        Invoke-WebRequest -Uri $PythonUrl -OutFile $PythonZip
        Write-Log "正在解压 Python..."
        Expand-Archive -Path $PythonZip -DestinationPath $PythonDir -Force
        Write-Log "正在清理下载的压缩包..."
        Remove-Item $PythonZip -Force
        
        Write-Log "正在配置 Python 环境以启用 pip..."
        # 这是使用嵌入式 Python 的关键步骤，必须取消 ._pth 文件中 import site 的注释
        $pthFile = Join-Path $PythonDir "python312._pth" # 注意: Python 版本更新时此文件名可能改变
        (Get-Content $pthFile) -replace '#import site', 'import site' | Set-Content $pthFile
        
        Write-Log "正在下载并安装 pip..."
        Write-Log "get-pip.py 下载地址: $($GetPipUrl)" -Color Gray
        Invoke-WebRequest -Uri $GetPipUrl -OutFile $GetPipPy
        
        # 安装 pip 时指定镜像源
        Write-Log "使用镜像源安装 pip: $($sources.PipIndex)" -Color Cyan
        & $PythonExe $GetPipPy --no-warn-script-location -i $($sources.PipIndex)
        
        Write-Log "Python 和 pip 安装完成!" -Color Green
    } catch {
        Write-Error "Python 安装过程中发生错误: $($_.Exception.Message)"
        Write-Log "提示：如果是网络问题，可以尝试:" -Color Yellow
        Write-Log "1. 检查网络连接" -Color Yellow
        Write-Log "2. 删除 python 文件夹后重试" -Color Yellow
        Write-Log "3. 手动下载 Python 并解压到 'python' 文件夹" -Color Yellow
        exit 1
    }
}
""

# --- 项目依赖安装 ---

# 与原版 bat 的差异:
# 原版 bat 试图创建虚拟环境 (venv)，但因为原bat脚本下载的那个是 Python 嵌入式版(embeddable)
# 精简之后不包含 venv 模块
# 所以始终出错:
# > \hajimi\python\python.exe: No module named venv
# > Installing project dependencies...
# > 系统找不到指定的路径。
# 
# 但这个 Python 本来就是相对系统 PATH 的 Python 独立存在的, 完全可以作为一个隔离的, 包含了 Python解释器的大号 venv
# 所以将依赖直接安装到这个独立的 Python 环境中，变成完全自包含的应用
# 这也符合嵌入式 Python 的设计初衷
# 因此，此脚本放弃了创建 venv，而是直接将包装入便携版 Python 中

if (-not (Test-Path $InstallFlagFile)) {
    Write-Log "正在安装项目依赖 (直接安装到便携版 Python 中)..."
    Write-Log "使用 pip 源: $($sources.PipIndex)" -Color Cyan
    try {
        # 直接使用便携版 Python 的可执行文件来操作 pip 和 uv
        Write-Log "  升级 pip..."
        & $PythonExe -m pip install --upgrade pip -i $($sources.PipIndex)
        
        Write-Log "  安装 uv 加速器..."
        & $PythonExe -m pip install uv -i $($sources.PipIndex)
        
        Write-Log "  使用 uv 安装依赖..."
        & $PythonExe -m uv pip install -r (Join-Path $ScriptRoot "requirements.txt") -i $($sources.PipIndex)
        
        # 创建标记文件，表示安装已成功完成，下次运行时将跳过此步骤
        New-Item -Path $InstallFlagFile -ItemType File -Force | Out-Null
        
        Write-Log "依赖安装完成!" -Color Green
    } catch {
        Write-Error "依赖安装过程中发生错误: $($_.Exception.Message)"
        Write-Error "请检查 requirements.txt 文件是否正确，或尝试删除 'python' 文件夹后重试。"
        exit 1
    }
} else {
    Write-Log "依赖已安装，跳过安装步骤。" -Color Green
}
""

# --- 启动应用 ---
Write-Log "正在启动 Hajimi 应用..." -Color Cyan
Write-Log "您现在可以通过 http://localhost:7860 或 http://<您的IP>:7860 访问" -Color Yellow
Write-Log "按 Ctrl+C 停止应用。" -Color Yellow

try {
    # 与原版 bat 依赖 call activate.bat 不同，这里直接调用 Scripts 目录下的可执行文件
    # 这种方式更明确、更可靠，不受 PowerShell 会话作用域的影响
    # 1. 明确构建可执行文件的完整路径
    $UvicornExe = Join-Path $PythonDir "Scripts/uvicorn.exe"
    # 2. 检查文件是否存在
    if (-not (Test-Path $UvicornExe)) {
        Throw "找不到 uvicorn.exe。请删除目录中的 '$InstallFlagFile' 文件并重新运行脚本以重新安装依赖。"
    }
    # 3. 使用 '&' 调用操作符直接执行该路径
    & $UvicornExe app.main:app --host 0.0.0.0 --port 7860
} catch {
    # 捕获所有可能的错误
    Write-Error "启动应用失败: $($_.Exception.Message)"
    exit 1
}