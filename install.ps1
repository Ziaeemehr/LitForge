# One-command install (and update) of LitForge on Windows:
# powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/Ziaeemehr/LitForge/main/install.ps1 | iex"
# Run in a child scope: with `irm | iex` this script runs in the user's session, which it must leave as it was.
& {
$ErrorActionPreference = 'Stop'

$repo = 'https://github.com/Ziaeemehr/LitForge.git'
if ($env:LITFORGE_REPO) {
    $repo = $env:LITFORGE_REPO
}

function Is-Checkout([string]$Path) {
    return (Test-Path -LiteralPath (Join-Path $Path '.git')) -and
        (Test-Path -LiteralPath (Join-Path $Path 'pyproject.toml')) -and
        (Select-String -LiteralPath (Join-Path $Path 'pyproject.toml') -Pattern '^name = "litforge"' -Quiet)
}
$saved = $null
$configHome = $env:APPDATA
if (-not $configHome) { $configHome = [Environment]::GetFolderPath('ApplicationData') }
if (-not $configHome) { $configHome = Join-Path $HOME 'AppData\Roaming' }
$configFile = Join-Path $configHome 'LitForge\desktop.json'
try {
    $settings = Get-Content -LiteralPath $configFile -Raw | ConvertFrom-Json
    if ($settings.workspace -is [string]) { $saved = $settings.workspace }
} catch { }
if ($env:LITFORGE_DIR) {
    $dir = $env:LITFORGE_DIR
} elseif (Is-Checkout (Get-Location).Path) {
    $dir = (Get-Location).Path
} elseif (Is-Checkout (Join-Path $HOME 'LitForge')) {
    $dir = Join-Path $HOME 'LitForge'
} elseif ($saved -and (Is-Checkout $saved)) {
    $dir = $saved
} else {
    $localData = $env:LOCALAPPDATA
    if (-not $localData) { $localData = [Environment]::GetFolderPath('LocalApplicationData') }
    if (-not $localData) { $localData = Join-Path $HOME 'AppData\Local' }
    $dir = Join-Path $localData 'LitForge\app'
}
if ($dir -eq '~') { $dir = $HOME }
elseif ($dir.StartsWith('~/') -or $dir.StartsWith('~\')) { $dir = Join-Path $HOME $dir.Substring(2) }
$dir = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($dir)

function Say([string]$Message) {
    Write-Host "==> $Message"
}

function Fail([string]$Message) {
    throw "error: $Message"
}

function Native([scriptblock]$Command) {
    # git and uv print progress on stderr, which Windows PowerShell 5.1 can treat as an error under 'Stop'.
    $ErrorActionPreference = 'Continue'
    & $Command
}

$gitAvailable = $null -ne (Get-Command git -ErrorAction SilentlyContinue)
if ((Test-Path -LiteralPath (Join-Path $dir '.git') ) -and $gitAvailable) {
    Say "Updating $dir"
    Native { git -C $dir pull --ff-only }
    if ($LASTEXITCODE -ne 0) {
        Fail "could not update $dir (local changes?); run 'git pull' there yourself"
    }
} elseif (Test-Path -LiteralPath $dir) {
    if (-not (Test-Path -LiteralPath (Join-Path $dir 'pyproject.toml') -PathType Leaf)) {
        Fail "$dir exists but is not LitForge; set LITFORGE_DIR to another folder"
    }
    Say "Using $dir as it is (not a git checkout, so it is not updated)"
} elseif ($gitAvailable) {
    Say "Downloading LitForge to $dir"
    New-Item -ItemType Directory -Path (Split-Path -Parent $dir) -Force | Out-Null
    Native { git clone $repo $dir }
    if ($LASTEXITCODE -ne 0) {
        Fail "could not clone $repo to $dir"
    }
} else {
    Say "Downloading LitForge to $dir (no git: using the ZIP of the main branch)"
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ([System.Guid]::NewGuid().ToString())
    New-Item -ItemType Directory -Path $tmp | Out-Null
    try {
        $zip = Join-Path $tmp 'LitForge.zip'
        Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/Ziaeemehr/LitForge/archive/refs/heads/main.zip' -OutFile $zip
        Expand-Archive -LiteralPath $zip -DestinationPath $tmp
        $parent = Split-Path -Parent $dir
        if (-not (Test-Path -LiteralPath $parent)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
        Move-Item -LiteralPath (Join-Path $tmp 'LitForge-main') -Destination $dir
    } finally {
        Remove-Item -LiteralPath $tmp -Recurse -Force
    }
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Say 'Installing uv (Python package manager)'
    Invoke-RestMethod 'https://astral.sh/uv/install.ps1' | Invoke-Expression
    $env:Path = (Join-Path $HOME '.local\bin') + ';' + $env:Path
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        Fail 'could not install uv'
    }
}

Push-Location -LiteralPath $dir
try {
    Say 'Installing dependencies (the first time takes a few minutes)'
    # Keep extras installed later, such as formulas.
    Native { uv sync --extra embed --extra documents --extra desktop --inexact }
    if ($LASTEXITCODE -ne 0) {
        Fail 'could not install dependencies'
    }
    $existing = $null
    if ($saved -and (Test-Path -LiteralPath (Join-Path $saved 'data') -PathType Container)) {
        $existing = $saved
    } elseif (Test-Path -LiteralPath (Join-Path $dir 'data') -PathType Container) {
        $existing = $dir
    }
    $documents = [Environment]::GetFolderPath('MyDocuments')
    if (-not $documents) { $documents = Join-Path $HOME 'Documents' }
    $defaultLibrary = Join-Path $documents 'LitForge'
    if ($existing) { $defaultLibrary = $existing }
    $library = $defaultLibrary
    if ($env:LITFORGE_LIBRARY) { $library = $env:LITFORGE_LIBRARY }
    $canPrompt = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected -and
                 $env:LITFORGE_NO_PROMPT -ne '1'
    $attempt = 0
    while ($true) {
        if ($canPrompt) {
            $library = Read-Host "Library folder [$defaultLibrary]"
            if (-not $library) { $library = $defaultLibrary }
        }
        if ($library -eq '~') { $library = $HOME }
        elseif ($library.StartsWith('~/') -or $library.StartsWith('~\')) {
            $library = Join-Path $HOME $library.Substring(2)
        }
        $library = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($library)
        Native { uv run litforge library set $library }
        if ($LASTEXITCODE -eq 0) { break }
        $attempt++
        if (-not $canPrompt) { Fail "cannot use library folder $library" }
        if ($attempt -ge 3) {
            $library = $defaultLibrary
            Native { uv run litforge library set $library }
            if ($LASTEXITCODE -ne 0) { Fail "cannot use default library folder $library" }
            break
        }
    }
    if ($existing -and $library -ne $existing) {
        Say "The old library stays at $existing. No files have been moved or copied."
    }
    # LitForge's own window uses Microsoft Edge WebView2, part of Windows 11 and of updated Windows 10.
    $webview2 = 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
    $hasWebView2 = (Test-Path "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}") -or
        (Test-Path "HKLM:\$webview2") -or (Test-Path "HKCU:\$webview2")
    if ($hasWebView2) {
        Native { uv run litforge desktop-setup }
    } else {
        Write-Warning ("Microsoft Edge WebView2 Runtime is required to open the LitForge window. " +
            "Install it from https://go.microsoft.com/fwlink/p/?LinkId=2124703 and run this installer again.")
    }
    Say 'Adding the LitForge icon'
    Native { uv run litforge shortcut --mode desktop }
    if ($LASTEXITCODE -ne 0) {
        Fail 'could not add the LitForge icon'
    }

    Say 'Installing Tectonic, the LaTeX engine of the Writing Studio (~22 MB)'
    Native { uv run litforge tools install tectonic --yes }
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Tectonic installation failed; retry with: cd '$dir'; uv run litforge tools install tectonic"
    }

    $extras = @()
    if ($env:LITFORGE_EXTRAS) {
        $extras = @($env:LITFORGE_EXTRAS.Split(',') | ForEach-Object { $_.Trim().ToLowerInvariant() })
    }
    $canPrompt = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected -and
                 $env:LITFORGE_NO_PROMPT -ne '1'
    foreach ($optional in @(
        @{ Name = 'mineru'; Prompt = 'Install MinerU for accurate PDF reading (~4 GB)? [y/N]' },
        @{ Name = 'pix2text'; Prompt = 'Install Pix2Text for better math in exports (~2 GB)? [y/N]' }
    )) {
        $install = $extras -contains $optional.Name
        if (-not $install -and $canPrompt) {
            $answer = Read-Host $optional.Prompt
            $install = $answer -match '^(?i:y|yes)$'
        }
        if (-not $install) {
            continue
        }
        $tool = $optional.Name
        Native { uv run litforge tools install $tool --yes }
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Optional $tool installation failed; retry with: cd '$dir'; uv run litforge tools install $tool"
        }
    }
} finally {
    Pop-Location
}

Say "Open LitForge from the LitForge icon on your desktop."
Write-Host "Program: $dir"
Write-Host "Library: $library"
Write-Host '    Run the same command again to update.'
Write-Host "    Optional tools: Settings > Optional tools, or run 'uv run litforge tools' in $dir."
}
