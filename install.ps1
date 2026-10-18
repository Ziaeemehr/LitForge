# One-command install (and update) of LitForge on Windows:
# powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/Ziaeemehr/LitForge/main/install.ps1 | iex"
# Run in a child scope: with `irm | iex` this script runs in the user's session, which it must leave as it was.
& {
$ErrorActionPreference = 'Stop'

$repo = 'https://github.com/Ziaeemehr/LitForge.git'
if ($env:LITFORGE_REPO) {
    $repo = $env:LITFORGE_REPO
}

# Use this checkout when no install directory is specified.
if ($env:LITFORGE_DIR) {
    $dir = $env:LITFORGE_DIR
} elseif ((Test-Path -LiteralPath 'pyproject.toml' -PathType Leaf) -and
          (Select-String -LiteralPath 'pyproject.toml' -Pattern '^name = "litforge"' -Quiet)) {
    $dir = (Get-Location).Path
} else {
    $dir = Join-Path $HOME 'LitForge'
}
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
if ((Test-Path -LiteralPath (Join-Path $dir '.git') -PathType Container) -and $gitAvailable) {
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
    Native { uv sync --extra embed --inexact }
    if ($LASTEXITCODE -ne 0) {
        Fail 'could not install dependencies'
    }
    Say 'Adding the LitForge icon'
    Native { uv run litforge shortcut }
    if ($LASTEXITCODE -ne 0) {
        Fail 'could not add the LitForge icon'
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

Say "LitForge is installed in $dir. Open it from the LitForge icon on your desktop."
Write-Host '    Run the same command again to update.'
Write-Host "    Optional tools: Settings > Optional tools, or run 'uv run litforge tools' in $dir."
}
