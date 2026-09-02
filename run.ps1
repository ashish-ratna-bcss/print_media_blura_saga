#!/usr/bin/env pwsh
#
# One command to run the project.
#
#   .\run.ps1              start the app at http://127.0.0.1:8080
#   .\run.ps1 dev          Vite dev server with HMR + auto-reloading backend
#   .\run.ps1 setup        create .venv, install Python and npm deps, make .env
#   .\run.ps1 build        rebuild the frontend bundle only
#   .\run.ps1 check        database and pipeline pre-flight, then exit
#
# `start` is the whole app in one process: FastAPI answers /api and serves the
# prebuilt React bundle from web/frontend/dist at /. The bundle is rebuilt
# first whenever a source file under web/frontend is newer than it, so editing
# the UI and re-running is enough -- there is no separate build step to forget.
#
# `dev` is the two-process arrangement instead, and is what you want while
# working on the UI: Vite serves the frontend on :5173 with hot reload and
# proxies /api through to the backend on :8080 (see web/frontend/vite.config.ts).
# Open :5173, not :8080 -- :8080 still serves the last built bundle.

param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'dev', 'build', 'setup', 'check')]
    [string]$Command = 'start',

    # Backend port. Vite's proxy target is hardcoded to 8080, so `dev` on a
    # different port needs vite.config.ts changed to match.
    [int]$Port = 8080,

    # 0.0.0.0 to accept connections from other machines. Named -Bind and not
    # -Host because $Host is a reserved PowerShell automatic variable.
    [string]$Bind = '127.0.0.1',

    [int]$VitePort = 5173,

    # Serve whatever is in dist/ without checking whether it is current.
    [switch]$NoBuild
)

$ErrorActionPreference = 'Stop'

$Root     = Split-Path -Parent $MyInvocation.MyCommand.Path
$Frontend = Join-Path $Root 'web\frontend'
$Dist     = Join-Path $Frontend 'dist'
$VenvPy   = Join-Path $Root '.venv\Scripts\python.exe'

function Say([string]$m)  { Write-Host "==> $m" -ForegroundColor Cyan }
function Warn([string]$m) { Write-Host "    $m" -ForegroundColor Yellow }

# Every external tool goes through here.
#
# Windows PowerShell turns whatever a native command writes to stderr into an
# ErrorRecord whenever the script's output is redirected, and under
# $ErrorActionPreference = 'Stop' that ends the run. Vite prints its build
# timings to stderr and uvicorn logs every request there, so a perfectly
# healthy build or a running server would abort the script the moment anyone
# piped it to a log file. The preference is function-scoped, so it reverts on
# return; the exit code is left in the global $LASTEXITCODE for the caller,
# which is the only signal from a native tool worth trusting.
function Invoke-Native([string]$File, [string[]]$Arguments, [string]$WorkDir) {
    $ErrorActionPreference = 'Continue'
    if ($WorkDir) { Push-Location $WorkDir }
    try { & $File @Arguments } finally { if ($WorkDir) { Pop-Location } }
}

function Get-Python {
    if (Test-Path $VenvPy) { return $VenvPy }
    $sys = Get-Command python -ErrorAction SilentlyContinue
    if ($sys) {
        Warn "no .venv -- using $($sys.Source). Run '.\run.ps1 setup' for an isolated one."
        return $sys.Source
    }
    throw "Python not found on PATH. Install Python 3.11+, then run '.\run.ps1 setup'."
}

function Invoke-Npm([string[]]$NpmArgs) {
    Invoke-Native 'npm.cmd' $NpmArgs $Frontend
    if ($LASTEXITCODE -ne 0) {
        throw "npm $($NpmArgs -join ' ') failed with exit code $LASTEXITCODE"
    }
}

function Install-Node {
    if (Test-Path (Join-Path $Frontend 'node_modules')) { return }
    if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) {
        throw 'npm not found on PATH. Install Node 20+ from https://nodejs.org'
    }
    Say 'installing frontend dependencies (first run only)'
    Invoke-Npm @('install')
}

# True when any frontend source is newer than the bundle, or there is no
# bundle. Only the inputs Vite actually reads are compared -- node_modules is
# far too large to walk on every start, and dist/ would compare against itself.
function Test-FrontendStale {
    $index = Join-Path $Dist 'index.html'
    if (-not (Test-Path $index)) { return $true }
    $built = (Get-Item $index).LastWriteTimeUtc
    $inputs = @('src', 'public', 'index.html', 'package.json', 'vite.config.ts',
                'tsconfig.json', 'tsconfig.app.json') |
              ForEach-Object { Join-Path $Frontend $_ } |
              Where-Object { Test-Path $_ }
    foreach ($p in $inputs) {
        $newest = Get-ChildItem $p -Recurse -File -ErrorAction SilentlyContinue |
                  Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
        if ($newest -and $newest.LastWriteTimeUtc -gt $built) { return $true }
    }
    return $false
}

function Build-Frontend {
    Install-Node
    Say 'building frontend'
    Invoke-Npm @('run', 'build')
}

# Bind the port ourselves before handing it to uvicorn. Otherwise the failure
# arrives as a traceback from deep inside asyncio, which does not say that the
# most likely cause is the copy of the server already running in another window.
function Test-PortFree([int]$p, [string]$bind) {
    try { $addr = [System.Net.IPAddress]::Parse($bind) }
    catch { return $true }
    $listener = $null
    try {
        $listener = New-Object System.Net.Sockets.TcpListener($addr, $p)
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($listener) { try { $listener.Stop() } catch { } }
    }
}

function Assert-PortFree([int]$p, [string]$bind, [string]$what) {
    $free = Test-PortFree $p $bind
    # On this machine Vite binds ::1 and nothing else, so a dev server already
    # running leaves 127.0.0.1 free and the IPv4 test alone would call the port
    # available. Both loopback families have to be clear.
    if ($free -and $bind -eq '127.0.0.1') { $free = Test-PortFree $p '::1' }
    if ($free) { return }
    throw "port $p is already in use ($what). Stop the other process, or pass -Port <n>."
}

switch ($Command) {

    'setup' {
        if (-not (Test-Path $VenvPy)) {
            if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
                throw 'Python not found on PATH. Install Python 3.11+ from https://python.org'
            }
            Say 'creating .venv'
            Invoke-Native 'python' @('-m', 'venv', (Join-Path $Root '.venv')) $Root
            if ($LASTEXITCODE -ne 0) { throw 'venv creation failed' }
        }
        Say 'installing Python dependencies'
        Invoke-Native $VenvPy @('-m', 'pip', 'install', '--upgrade', 'pip', '--quiet') $Root
        Invoke-Native $VenvPy @('-m', 'pip', 'install', '-r', (Join-Path $Root 'requirements.txt')) $Root
        if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
        Install-Node
        $envFile = Join-Path $Root '.env'
        if (-not (Test-Path $envFile)) {
            Copy-Item (Join-Path $Root '.env.example') $envFile
            Say '.env created from .env.example'
            Warn 'it points at mongodb://localhost:27017 -- edit it for Atlas.'
        }
        Say "setup complete. Run '.\run.ps1' to start."
        # The GPU half is deliberately not installed here: torch, the YOLO
        # weights and the vision model are a multi-gigabyte download, and the
        # app runs without them -- uploads and extraction are what need them,
        # not the archive, the beats or the catalogue. See README "Local backend".
        Warn 'extraction also needs torch, the YOLO weights and Ollama -- see README.'
    }

    'build' { Build-Frontend }

    'check' {
        $py = Get-Python
        Invoke-Native $py @((Join-Path $Root 'scripts\db_check.py')) $Root
        Invoke-Native $py @((Join-Path $Root 'scripts\health.py')) $Root
    }

    'dev' {
        $py = Get-Python
        Install-Node
        Assert-PortFree $Port $Bind 'the backend'
        Assert-PortFree $VitePort '127.0.0.1' 'the Vite dev server'
        Say "frontend  http://localhost:$VitePort   <- open this one"
        Say "backend   http://${Bind}:$Port"
        $vite = Start-Process -FilePath 'npm.cmd' `
                              -ArgumentList 'run', 'dev', '--', '--port', "$VitePort" `
                              -WorkingDirectory $Frontend -PassThru -NoNewWindow
        try {
            Invoke-Native $py @((Join-Path $Root 'web\app.py'),
                                '--port', "$Port", '--host', $Bind, '--reload') $Root
        } finally {
            # npm.cmd spawns node as a child, so killing the id we hold leaves
            # Vite running and the port taken. /T takes the tree with it.
            if ($vite -and -not $vite.HasExited) {
                Invoke-Native 'taskkill' @('/PID', "$($vite.Id)", '/T', '/F') $Root | Out-Null
            }
        }
    }

    'start' {
        $py = Get-Python
        if (-not $NoBuild -and (Test-FrontendStale)) { Build-Frontend }
        Assert-PortFree $Port $Bind 'another server'
        Invoke-Native $py @((Join-Path $Root 'web\app.py'),
                            '--port', "$Port", '--host', $Bind) $Root
    }
}
