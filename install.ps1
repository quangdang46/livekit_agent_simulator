# Install livekit-agent-simulator from GitHub (git + uv/pipx). No PyPI / wheel.
#
#   irm "https://raw.githubusercontent.com/quangdang46/livekit-agent-simulator/main/install.ps1" | iex
#
#   .\install.ps1 -GitRef v0.1.0 -Verify
#   .\install.ps1 -Uninstall
#
#Requires -Version 5.1
[CmdletBinding()]
param(
    [Alias("Version")]
    [string]$GitRef = $(if ($env:LK_SIM_REF) { $env:LK_SIM_REF } else { "main" }),
    [switch]$NoMcp,
    [switch]$Verify,
    [switch]$Uninstall,
    [switch]$Quiet,
    # Accepted for back-compat with earlier installers; install is always from git.
    [switch]$FromGit
)

$ErrorActionPreference = "Stop"
$BinaryName = "lk-sim"
$McpServerName = "livekit-agent-simulator"
$PkgName = "livekit-agent-simulator"
$Owner = "quangdang46"
$Repo = "livekit-agent-simulator"

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    if ($Quiet -and $Level -eq "INFO") { return }
    $prefix = "[$BinaryName]"
    if ($Level -eq "WARN") { Write-Host "$prefix WARN: $Message" -ForegroundColor Yellow }
    elseif ($Level -eq "ERROR") { Write-Host "$prefix ERROR: $Message" -ForegroundColor Red }
    else { Write-Host "$prefix $Message" }
}

function Merge-JsonIntoFile {
    param(
        [string]$FilePath,
        [string]$Key,
        [hashtable]$Value
    )
    $dir = Split-Path -Parent $FilePath
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }

    $data = [ordered]@{}
    if (Test-Path $FilePath) {
        try {
            $raw = Get-Content -Path $FilePath -Raw -ErrorAction Stop
            if ($raw.Trim()) {
                $obj = $raw | ConvertFrom-Json
                $data = [ordered]@{}
                foreach ($p in $obj.PSObject.Properties) {
                    $data[$p.Name] = $p.Value
                }
            }
        } catch {
            $data = [ordered]@{}
        }
    }

    if (-not $data.Contains($Key)) {
        $data[$Key] = [ordered]@{}
    }

    $bucket = $data[$Key]
    $bucketMap = [ordered]@{}
    if ($bucket -is [System.Collections.IDictionary]) {
        foreach ($k in $bucket.Keys) { $bucketMap[$k] = $bucket[$k] }
    } elseif ($null -ne $bucket -and $bucket.PSObject) {
        foreach ($p in $bucket.PSObject.Properties) { $bucketMap[$p.Name] = $p.Value }
    }

    foreach ($k in $Value.Keys) {
        $bucketMap[$k] = $Value[$k]
    }
    $data[$Key] = $bucketMap

    ($data | ConvertTo-Json -Depth 12) + "`n" | Set-Content -Path $FilePath -Encoding UTF8
}

function Remove-McpFromFile {
    param([string]$FilePath, [string]$ParentKey = "mcpServers", [string]$ServerName)
    if (-not (Test-Path $FilePath)) { return }
    try {
        $obj = Get-Content -Path $FilePath -Raw | ConvertFrom-Json
        if ($null -eq $obj.$ParentKey) { return }
        $map = [ordered]@{}
        foreach ($p in $obj.PSObject.Properties) {
            if ($p.Name -eq $ParentKey) {
                $inner = [ordered]@{}
                foreach ($ip in $p.Value.PSObject.Properties) {
                    if ($ip.Name -ne $ServerName) { $inner[$ip.Name] = $ip.Value }
                }
                $map[$p.Name] = $inner
            } else {
                $map[$p.Name] = $p.Value
            }
        }
        ($map | ConvertTo-Json -Depth 12) + "`n" | Set-Content -Path $FilePath -Encoding UTF8
    } catch {
        Write-Log "Could not edit $FilePath : $_" "WARN"
    }
}

function Resolve-LkSim {
    $cmd = Get-Command $BinaryName -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidates = @(
        (Join-Path $env:USERPROFILE ".local\bin\$BinaryName.exe"),
        (Join-Path $env:USERPROFILE ".local\bin\$BinaryName"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Scripts\$BinaryName.exe")
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    return $null
}

function Configure-AllMcpProviders {
    $binary = Resolve-LkSim
    if (-not $binary) {
        Write-Log "lk-sim not found on PATH — skip MCP provider config" "WARN"
        return
    }
    Write-Log "Configuring MCP providers → $binary mcp"
    $entry = @{
        $McpServerName = @{
            command = $binary
            args    = @("mcp")
            env     = @{}
        }
    }

    Merge-JsonIntoFile -FilePath (Join-Path $env:USERPROFILE ".claude.json") -Key "mcpServers" -Value $entry
    Merge-JsonIntoFile -FilePath (Join-Path $env:USERPROFILE ".cursor\mcp.json") -Key "mcpServers" -Value $entry

    $cline = Join-Path $env:APPDATA "Code\User\globalStorage\saoudrizwan.claude-dev\settings\cline_mcp_settings.json"
    if (Test-Path (Split-Path $cline)) {
        Merge-JsonIntoFile -FilePath $cline -Key "mcpServers" -Value $entry
    }

    Merge-JsonIntoFile -FilePath (Join-Path $env:USERPROFILE ".codeium\windsurf\mcp_config.json") -Key "mcpServers" -Value $entry
    Merge-JsonIntoFile -FilePath (Join-Path $env:USERPROFILE ".vscode\mcp.json") -Key "servers" -Value $entry
    Merge-JsonIntoFile -FilePath (Join-Path $env:USERPROFILE ".gemini\settings.json") -Key "mcpServers" -Value $entry
    Merge-JsonIntoFile -FilePath (Join-Path $env:USERPROFILE ".aws\amazonq\mcp.json") -Key "mcpServers" -Value $entry
    Merge-JsonIntoFile -FilePath (Join-Path $env:USERPROFILE ".aws\amazonq\default.json") -Key "mcpServers" -Value $entry

    $opencode = Join-Path $env:USERPROFILE ".opencode.json"
    if ((Test-Path $opencode) -or (Test-Path (Join-Path $env:USERPROFILE ".config\opencode"))) {
        $ocEntry = @{
            $McpServerName = @{
                type    = "stdio"
                command = $binary
                args    = @("mcp")
                env     = @()
            }
        }
        Merge-JsonIntoFile -FilePath $opencode -Key "mcpServers" -Value $ocEntry
    }

    $codexDir = Join-Path $env:USERPROFILE ".codex"
    $codex = Join-Path $codexDir "config.toml"
    if (Test-Path $codexDir) {
        if (-not (Test-Path $codex)) { New-Item -ItemType File -Path $codex -Force | Out-Null }
        $content = Get-Content -Path $codex -Raw -ErrorAction SilentlyContinue
        if ($content -notmatch "\[mcp_servers\.$([regex]::Escape($McpServerName))\]") {
            Add-Content -Path $codex -Value @"

[mcp_servers.$McpServerName]
type = "stdio"
command = "$binary"
args = ["mcp"]
"@
        }
    }
}

function Uninstall-All {
    Write-Log "Uninstalling $PkgName..."
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        try { uv tool uninstall $PkgName 2>$null } catch {}
    }
    if (Get-Command pipx -ErrorAction SilentlyContinue) {
        try { pipx uninstall $PkgName 2>$null } catch {}
    }
    Remove-McpFromFile -FilePath (Join-Path $env:USERPROFILE ".claude.json") -ServerName $McpServerName
    Remove-McpFromFile -FilePath (Join-Path $env:USERPROFILE ".cursor\mcp.json") -ServerName $McpServerName
    Remove-McpFromFile -FilePath (Join-Path $env:USERPROFILE ".vscode\mcp.json") -ParentKey "servers" -ServerName $McpServerName
    Remove-McpFromFile -FilePath (Join-Path $env:USERPROFILE ".gemini\settings.json") -ServerName $McpServerName
    Remove-McpFromFile -FilePath (Join-Path $env:USERPROFILE ".aws\amazonq\mcp.json") -ServerName $McpServerName
    Remove-McpFromFile -FilePath (Join-Path $env:USERPROFILE ".aws\amazonq\default.json") -ServerName $McpServerName
    Write-Log "Uninstalled $PkgName"
}

function Install-Package {
    $hasUv = [bool](Get-Command uv -ErrorAction SilentlyContinue)
    $hasPipx = [bool](Get-Command pipx -ErrorAction SilentlyContinue)
    if (-not $hasUv -and -not $hasPipx) {
        throw "Need uv or pipx. Install uv: https://docs.astral.sh/uv/getting-started/installation/"
    }

    $spec = "git+https://github.com/$Owner/$Repo.git@$GitRef"
    Write-Log "Source: $spec (git only — no PyPI)"

    if ($hasUv) {
        Write-Log "uv tool install --force $spec"
        & uv tool install --force $spec
        if ($LASTEXITCODE -ne 0) { throw "uv tool install failed" }
    } else {
        Write-Log "pipx install --force $spec"
        & pipx install --force $spec
        if ($LASTEXITCODE -ne 0) { throw "pipx install failed" }
    }
}

if ($Uninstall) {
    Uninstall-All
    return
}

Write-Log "Installing $PkgName (CLI $BinaryName | MCP: $BinaryName mcp)"
Install-Package

if (-not $NoMcp) {
    Configure-AllMcpProviders
} else {
    Write-Log "Skipped MCP auto-config (-NoMcp)"
}

if ($Verify) {
    $lk = Get-Command $BinaryName -ErrorAction SilentlyContinue
    if (-not $lk) { throw "$BinaryName not on PATH after install" }
    & $BinaryName --help | Out-Null
    Write-Log "Verified $BinaryName --help"
}

Write-Host ""
Write-Host "✓ $PkgName installed" -ForegroundColor Green
$lkCmd = Get-Command $BinaryName -ErrorAction SilentlyContinue
if ($lkCmd) {
    Write-Host "  CLI: $($lkCmd.Source)"
    Write-Host "  MCP: $($lkCmd.Source) mcp"
}
Write-Host ""
Write-Host "  Quick start:"
Write-Host "    $BinaryName guide"
Write-Host "    $BinaryName init --root C:\path\to\target"
Write-Host "    $BinaryName web --root C:\path\to\target"
Write-Host "    $BinaryName mcp"
Write-Host ""
Write-Host "  Report player is prebuilt in the git tree (no Node/pnpm required)."
Write-Host ""
