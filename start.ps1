# E-commerce Agent launcher.
# First run: finds Python, installs dependencies into .venv, downloads MySQL if needed,
# creates .env with random passwords and an empty LLM_API_KEY, initializes the database
# and loads demo data. Later runs just start MySQL and the app.
# This file is ASCII only so Windows PowerShell 5.1 reads it correctly on any system.
$ErrorActionPreference = 'Continue'
$root = $PSScriptRoot
$envFile = Join-Path $root '.env'
$MYSQL_VERSION = '8.4.13'
$MYSQL_MD5 = '7b94489d5524dea9b27097068bea25a8'
$MYSQL_URLS = @(
    "https://cdn.mysql.com/Downloads/MySQL-8.4/mysql-$MYSQL_VERSION-winx64.zip",
    "https://cdn.mysql.com/archives/mysql-8.4/mysql-$MYSQL_VERSION-winx64.zip"
)
Set-Location $root

function Fail([string]$message) {
    Write-Host ''
    Write-Host "[ERROR] $message" -ForegroundColor Red
    exit 1
}

function Step([string]$message) { Write-Host "==> $message" -ForegroundColor Cyan }

function New-Secret([int]$n) {
    $chars = 'abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789'.ToCharArray()
    $bytes = New-Object byte[] $n
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
}

function Read-EnvFile {
    $values = @{}
    if (Test-Path $envFile) {
        foreach ($line in Get-Content $envFile -Encoding UTF8) {
            if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') { $values[$Matches[1]] = $Matches[2].Trim() }
        }
    }
    $values
}

function Test-PortOpen([int]$port) {
    $client = New-Object Net.Sockets.TcpClient
    try { $client.ConnectAsync('127.0.0.1', $port).Wait(500) } catch { $false } finally { $client.Close() }
}

function Get-FreePort([int]$start) {
    $port = $start
    while (Test-PortOpen $port) { $port++ }
    $port
}

function Get-PortOwnerPath([int]$port) {
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $conn) { return $null }
    (Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue).Path
}

function Test-PythonExe([string]$exe, [string[]]$prefix = @()) {
    # The Microsoft Store placeholder prints an error instead of running code, so run it and check the result.
    # No quotes inside the code: PowerShell 5.1 strips double quotes from native command arguments.
    $code = 'import sys;print(int((3,11)<=sys.version_info[:2]<=(3,14)))'
    try { $out = & $exe @prefix -c $code 2>$null } catch { return $false }
    "$out".Trim() -eq '1'
}

function Find-Python {
    foreach ($version in '-3.12', '-3.11', '-3.13', '-3.14') {
        if ((Get-Command py -ErrorAction SilentlyContinue) -and (Test-PythonExe 'py' @($version))) {
            return (& py $version -c 'import sys;print(sys.executable)').Trim()
        }
    }
    $onPath = Get-Command python -ErrorAction SilentlyContinue
    if ($onPath -and (Test-PythonExe $onPath.Source)) { return $onPath.Source }
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
        "$env:USERPROFILE\anaconda3\python.exe",
        "$env:USERPROFILE\miniconda3\python.exe",
        "$env:LOCALAPPDATA\anaconda3\python.exe",
        "$env:LOCALAPPDATA\miniconda3\python.exe",
        'C:\ProgramData\anaconda3\python.exe',
        'C:\ProgramData\miniconda3\python.exe',
        'C:\Program Files\Python3*\python.exe',
        'C:\Python3*\python.exe'
    )
    foreach ($pattern in $candidates) {
        foreach ($item in Get-Item $pattern -ErrorAction SilentlyContinue) {
            if (Test-PythonExe $item.FullName) { return $item.FullName }
        }
    }
    $null
}

function Install-Dependencies([string]$venvPython) {
    Step 'Installing Python packages. The first run takes a few minutes...'
    $requirements = Join-Path $root 'requirements.txt'
    & $venvPython -m pip install --disable-pip-version-check -q -r $requirements
    if ($LASTEXITCODE) {
        Write-Host 'Default package index failed. Retrying with the Tsinghua mirror...' -ForegroundColor Yellow
        & $venvPython -m pip install --disable-pip-version-check -q -r $requirements -i 'https://pypi.tuna.tsinghua.edu.cn/simple'
        if ($LASTEXITCODE) { Fail 'Could not install Python packages. Check the network and run start.bat again.' }
    }
}

function Get-AppPython([hashtable]$cfg) {
    $custom = if ($env:ECOM_PYTHON) { $env:ECOM_PYTHON } else { $cfg['ECOM_PYTHON'] }
    if ($custom) {
        # A Python chosen by the user is used as is. It is checked, never changed.
        if (-not (Test-Path $custom)) { Fail "ECOM_PYTHON does not exist: $custom" }
        & $custom -c 'import fastapi,uvicorn,sqlalchemy,pymysql,cryptography,openai,jwt,multipart,jieba,rank_bm25,numpy' 2>$null
        if ($LASTEXITCODE) { Fail "Packages are missing in ${custom}. Run: `"$custom`" -m pip install -r requirements.txt" }
        return $custom
    }
    $venvPython = Join-Path $root '.venv\Scripts\python.exe'
    if (-not (Test-Path $venvPython)) {
        Step 'Looking for Python 3.11 - 3.14...'
        $base = Find-Python
        if (-not $base) {
            Fail "Python 3.11 - 3.14 was not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run start.bat again."
        }
        Step "Creating .venv with $base"
        & $base -m venv (Join-Path $root '.venv')
        if ($LASTEXITCODE -or -not (Test-Path $venvPython)) { Fail 'Could not create .venv.' }
    }
    $stamp = Join-Path $root '.venv\requirements.installed'
    $current = (Get-FileHash (Join-Path $root 'requirements.txt') -Algorithm MD5).Hash
    if (-not (Test-Path $stamp) -or (Get-Content $stamp) -ne $current) {
        Install-Dependencies $venvPython
        Set-Content -Path $stamp -Value $current -Encoding ascii
    }
    $venvPython
}

function Get-InstanceDir {
    # MySQL can fail on paths with non-English characters, so it never lives inside the project folder.
    if ($env:ECOM_INSTANCE_DIR) { return $env:ECOM_INSTANCE_DIR }
    $dir = Join-Path $env:LOCALAPPDATA 'EcomAgent'
    if ($dir -match '^[\x00-\x7F]+$') { return $dir }
    'C:\EcomAgent'
}

function Install-MySql([string]$mysqlHome) {
    if (Test-Path (Join-Path $mysqlHome 'bin\mysqld.exe')) { return }
    if (-not (Test-Path "$env:WINDIR\System32\vcruntime140_1.dll")) {
        Fail 'MySQL needs the Microsoft Visual C++ runtime. Install it from https://aka.ms/vs/17/release/vc_redist.x64.exe, then run start.bat again.'
    }
    $parent = Split-Path $mysqlHome
    New-Item -ItemType Directory -Force $parent | Out-Null
    $zip = Join-Path $parent "mysql-$MYSQL_VERSION-winx64.zip"
    $verified = (Test-Path $zip) -and ((Get-FileHash $zip -Algorithm MD5).Hash.ToLower() -eq $MYSQL_MD5)
    if (-not $verified) {
        Step "Downloading MySQL $MYSQL_VERSION (about 270 MB)..."
        foreach ($url in $MYSQL_URLS) {
            & curl.exe -L --fail --retry 3 -A 'Mozilla/5.0' -o $zip $url
            $verified = ($LASTEXITCODE -eq 0) -and ((Get-FileHash $zip -Algorithm MD5).Hash.ToLower() -eq $MYSQL_MD5)
            if ($verified) { break }
        }
    }
    if (-not $verified) {
        if (Test-Path $zip) { Remove-Item $zip -Force }
        Fail "Could not download MySQL. Download the $MYSQL_VERSION Windows ZIP from https://dev.mysql.com/downloads/mysql/ , unzip it, add MYSQL_HOME=<that folder> to .env, then run start.bat again."
    }
    Step 'Unpacking MySQL...'
    & tar.exe -xf $zip -C $parent
    if ($LASTEXITCODE) { Fail 'Could not unpack MySQL.' }
    $unpacked = Join-Path $parent "mysql-$MYSQL_VERSION-winx64"
    if ($unpacked -ne $mysqlHome) { Move-Item $unpacked $mysqlHome }
    Remove-Item $zip -Force
}

# ---------- Configuration ----------
if ($PSVersionTable.PSVersion.Major -lt 5) { Fail 'PowerShell 5.1 or newer is required.' }
$cfg = Read-EnvFile
$instance = Get-InstanceDir
$mysqlHome = if ($env:ECOM_MYSQL_HOME) { $env:ECOM_MYSQL_HOME } elseif ($cfg['MYSQL_HOME']) { $cfg['MYSQL_HOME'] } else { Join-Path $instance "mysql-$MYSQL_VERSION" }
$dataDir = if ($cfg['MYSQL_DATA']) { $cfg['MYSQL_DATA'] } elseif ($cfg['MYSQL_HOME']) { Join-Path $mysqlHome 'data' } else { Join-Path $instance 'data' }
$firstRun = -not (Test-Path $dataDir)

if (-not (Test-Path $envFile)) {
    if (-not $firstRun) {
        Fail "A database already exists at $dataDir but this folder has no .env. Copy back the old .env, or delete that data folder to start fresh (this deletes its data)."
    }
    $dbPort = Get-FreePort 33061
    $appPort = Get-FreePort 8000
    $dbPassword = New-Secret 24
    $adminPassword = New-Secret 16
    $lines = @(
        "DATABASE_URL=mysql+pymysql://ecom:$dbPassword@127.0.0.1:$dbPort/ecom_agent?charset=utf8mb4"
        "MYSQL_ROOT_PASSWORD=$(New-Secret 24)"
        "MYSQL_HOME=$mysqlHome"
        "MYSQL_DATA=$dataDir"
        "APP_PORT=$appPort"
        "JWT_SECRET=$(New-Secret 48)"
        "ADMIN_PASSWORD=$adminPassword"
        'LLM_BASE_URL=https://token-plan.maas.qianwenaiapi.com/compatible-mode/v1'
        'LLM_MODEL=qwen3.8-flash'
        'LLM_API_KEY='
    )
    Set-Content -Path $envFile -Value $lines -Encoding ascii
    Write-Host ''
    Write-Host 'Created .env. Your admin login:' -ForegroundColor Green
    Write-Host '  user: admin'
    Write-Host '  password: see ADMIN_PASSWORD in the .env file'
    Write-Host 'To turn on the agents, fill in LLM_API_KEY in the .env file by hand and restart.'
    Write-Host ''
    $cfg = Read-EnvFile
}

foreach ($required in 'DATABASE_URL', 'JWT_SECRET', 'ADMIN_PASSWORD') {
    if (-not $cfg[$required]) { Fail "$required is missing in .env. See .env.example." }
}
$portMatch = [regex]::Match($cfg['DATABASE_URL'], '@[^:/]+:(\d+)/')
if (-not $portMatch.Success) { Fail 'DATABASE_URL in .env must include a port.' }
$dbPort = [int]$portMatch.Groups[1].Value
$appPort = if ($cfg['APP_PORT']) { [int]$cfg['APP_PORT'] } else { 8000 }

$py = Get-AppPython $cfg
Install-MySql $mysqlHome
$mysqld = Join-Path $mysqlHome 'bin\mysqld.exe'
$mysqlClient = Join-Path $mysqlHome 'bin\mysql.exe'
if (-not (Test-Path $mysqld)) { Fail "mysqld.exe was not found under $mysqlHome" }

# ---------- MySQL ----------
if ($firstRun) {
    if (Test-PortOpen $dbPort) { Fail "Port $dbPort is used by another program. Change the port in DATABASE_URL in .env, or free it." }
    Step 'Initializing the database...'
    New-Item -ItemType Directory -Force (Split-Path $dataDir) | Out-Null
    & $mysqld --initialize-insecure "--basedir=$mysqlHome" "--datadir=$dataDir" --character-set-server=utf8mb4
    if ($LASTEXITCODE) { Fail 'MySQL initialization failed.' }
}

if (Test-PortOpen $dbPort) {
    if ((Get-PortOwnerPath $dbPort) -ne $mysqld) {
        Fail "Port $dbPort is used by another program, not this project's MySQL. Stop that program, or change the port in DATABASE_URL in .env."
    }
} else {
    Step "Starting MySQL on port $dbPort..."
    Start-Process $mysqld -WindowStyle Hidden -ArgumentList @(
        "--basedir=`"$mysqlHome`"", "--datadir=`"$dataDir`"", '--bind-address=127.0.0.1', "--port=$dbPort",
        '--mysqlx=OFF', '--character-set-server=utf8mb4')
    foreach ($i in 1..120) { if (Test-PortOpen $dbPort) { break }; Start-Sleep -Milliseconds 500 }
    if (-not (Test-PortOpen $dbPort)) { Fail "MySQL did not start. See the .err file in $dataDir" }
}

if ($firstRun) {
    $dbPassword = [regex]::Match($cfg['DATABASE_URL'], '://[^:]+:([^@]+)@').Groups[1].Value
    $rootPassword = $cfg['MYSQL_ROOT_PASSWORD']
    $sql = "CREATE DATABASE ecom_agent CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; " +
           "CREATE DATABASE ecom_agent_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; " +
           "CREATE USER 'ecom'@'localhost' IDENTIFIED BY '$dbPassword'; " +
           "CREATE USER 'ecom'@'127.0.0.1' IDENTIFIED BY '$dbPassword'; " +
           "GRANT ALL ON ecom_agent.* TO 'ecom'@'localhost'; GRANT ALL ON ecom_agent.* TO 'ecom'@'127.0.0.1'; " +
           "GRANT ALL ON ecom_agent_test.* TO 'ecom'@'localhost'; GRANT ALL ON ecom_agent_test.* TO 'ecom'@'127.0.0.1'; " +
           "ALTER USER 'root'@'localhost' IDENTIFIED BY '$rootPassword';"
    & $mysqlClient -h 127.0.0.1 -P $dbPort -u root -e $sql
    if ($LASTEXITCODE) { Fail 'Could not create the database.' }
    Step 'Loading demo data...'
    & $py -m app.seed
    if ($LASTEXITCODE) { Fail 'Could not load demo data.' }
}

# ---------- App ----------
$url = "http://127.0.0.1:$appPort"
if (Test-PortOpen $appPort) {
    # An older copy of this app may still be running, possibly with no window. Restart it so .env changes apply.
    $conn = Get-NetTCPConnection -LocalPort $appPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    $owner = if ($conn) { Get-CimInstance Win32_Process -Filter "ProcessId=$($conn.OwningProcess)" }
    if (-not $owner -or $owner.CommandLine -notlike '*app.main:app*') {
        Fail "Port $appPort is used by another program. Stop that program, or change APP_PORT in .env."
    }
    Step 'Restarting the app that is already running...'
    Stop-Process -Id $owner.ProcessId -Force
    foreach ($i in 1..20) { if (-not (Test-PortOpen $appPort)) { break }; Start-Sleep -Milliseconds 500 }
    if (Test-PortOpen $appPort) { Fail "Could not stop the running app on port $appPort." }
}
if (-not ($cfg['LLM_API_KEY'] -or $env:LLM_API_KEY -or $env:DASHSCOPE_API_KEY)) {
    Write-Host 'No model API key is set. Agents and knowledge base Q&A stay off until LLM_API_KEY is added to .env.' -ForegroundColor Yellow
}
if (-not $env:ECOM_NO_BROWSER) {
    $open = "foreach (`$i in 1..60) { try { `$c = New-Object Net.Sockets.TcpClient; `$c.Connect('127.0.0.1', $appPort); `$c.Close(); Start-Process '$url'; break } catch { Start-Sleep 1 } }"
    Start-Process powershell -WindowStyle Hidden -ArgumentList '-NoProfile', '-Command', $open
}
Write-Host ''
Write-Host "Open $url  (user: admin, password: ADMIN_PASSWORD in .env)" -ForegroundColor Green
Write-Host 'Close this window to stop the app. MySQL keeps running in the background until the computer restarts.'
& $py -m uvicorn app.main:app --host 127.0.0.1 --port $appPort
