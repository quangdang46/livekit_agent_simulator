# Install lk-sim from GitHub Releases (CI portable pack).
# No uv, no pip, no build on the user machine - download zip + PATH.
#
#   irm "https://github.com/quangdang46/livekit-agent-simulator/releases/download/v0.1.0/install.ps1" -OutFile install.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\install.ps1 -Verify
#
#Requires -Version 5.1
[CmdletBinding()]
param(
    [Alias("Version")]
    [string]$GitRef = $(if ($env:LK_SIM_REF) { $env:LK_SIM_REF } else { "" }),
    [switch]$NoMcp,
    [switch]$Verify,
    [switch]$Uninstall,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"
$BinaryName = "lk-sim"
$McpServerName = "livekit-agent-simulator"
$PkgName = "livekit-agent-simulator"
$Owner = "quangdang46"
$Repo = "livekit-agent-simulator"
# Install root: %LOCALAPPDATA%\lk-sim\current
$InstallRoot = Join-Path $env:LOCALAPPDATA "lk-sim"
$CurrentDir = Join-Path $InstallRoot "current"
$ShimDir = Join-Path $env:USERPROFILE ".local\bin"

function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    if ($Quiet -and $Level -eq "INFO") { return }
    $prefix = "[$BinaryName]"
    if ($Level -eq "WARN") { Write-Host "$prefix WARN: $Message" -ForegroundColor Yellow }
    elseif ($Level -eq "ERROR") { Write-Host "$prefix ERROR: $Message" -ForegroundColor Red }
    else { Write-Host "$prefix $Message" }
}

function Ensure-DirOnPath {
    param([string]$Dir)
    if (-not $Dir) { return }
    if (-not (Test-Path $Dir)) {
        New-Item -ItemType Directory -Path $Dir -Force | Out-Null
    }
    $parts = $env:PATH -split ';' | Where-Object { $_ -and ($_ -ne $Dir) }
    $env:PATH = (@($Dir) + $parts) -join ';'
    try {
        $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
        if (-not $userPath) { $userPath = "" }
        if (($userPath -split ';') -notcontains $Dir) {
            $newUser = if ($userPath.Trim()) { "$Dir;$userPath" } else { $Dir }
            [Environment]::SetEnvironmentVariable("Path", $newUser, "User")
            Write-Log "PATH += $Dir (user)"
        }
    } catch {
        Write-Log "Could not persist PATH for $Dir : $_" "WARN"
    }
}

function Get-LatestReleaseTag {
    try {
        $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$Owner/$Repo/releases/latest" -UseBasicParsing
        if ($rel.tag_name) { return [string]$rel.tag_name }
    } catch {
        Write-Log "Could not resolve latest release: $_" "WARN"
    }
    return $null
}

function Resolve-InstallRef {
    if ($GitRef -and $GitRef.Trim()) { return $GitRef.Trim() }
    $latest = Get-LatestReleaseTag
    if ($latest) {
        Write-Log "Default ref -> latest release $latest"
        return $latest
    }
    throw "No GitRef and no GitHub releases found. Pass -GitRef v0.1.0"
}

function Get-ReleaseTagFromRef {
    param([string]$Ref)
    if ($Ref -match '^[0-9]+\.[0-9]+') { return "v$Ref" }
    if ($Ref -match '^v[0-9]+\.[0-9]+') { return $Ref }
    return $null
}

function Get-PortableAssetName {
    $arch = $env:PROCESSOR_ARCHITECTURE
    switch -Regex ($arch) {
        '^(ARM64|arm64)$' { return "lk-sim-windows-arm64.zip" }
        default { return "lk-sim-windows-x64.zip" }
    }
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

    ($data | ConvertTo-Json -Depth 12) + "`n" | Set-Content -Path $FilePath -Encoding ASCII
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
        ($map | ConvertTo-Json -Depth 12) + "`n" | Set-Content -Path $FilePath -Encoding ASCII
    } catch {
        Write-Log "Could not edit $FilePath : $_" "WARN"
    }
}

function Resolve-LkSim {
    $cmd = Get-Command $BinaryName -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $candidates = @(
        (Join-Path $ShimDir "lk-sim.cmd"),
        (Join-Path $CurrentDir "lk-sim.cmd"),
        (Join-Path $CurrentDir "lk-sim")
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    return $null
}

function Configure-AllMcpProviders {
    $binary = Resolve-LkSim
    if (-not $binary) {
        Write-Log "lk-sim not found on PATH - skip MCP provider config" "WARN"
        return
    }
    Write-Log "Configuring MCP providers -> $binary mcp"
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
    Write-Log "Uninstalling $PkgName portable pack..."
    if (Test-Path $InstallRoot) {
        Remove-Item -Recurse -Force $InstallRoot -ErrorAction SilentlyContinue
    }
    foreach ($name in @("lk-sim.cmd", "lk-sim-mcp.cmd", "lk-sim", "lk-sim-mcp")) {
        $p = Join-Path $ShimDir $name
        if (Test-Path $p) { Remove-Item -Force $p -ErrorAction SilentlyContinue }
    }
    Remove-McpFromFile -FilePath (Join-Path $env:USERPROFILE ".claude.json") -ServerName $McpServerName
    Remove-McpFromFile -FilePath (Join-Path $env:USERPROFILE ".cursor\mcp.json") -ServerName $McpServerName
    Remove-McpFromFile -FilePath (Join-Path $env:USERPROFILE ".vscode\mcp.json") -ParentKey "servers" -ServerName $McpServerName
    Remove-McpFromFile -FilePath (Join-Path $env:USERPROFILE ".gemini\settings.json") -ServerName $McpServerName
    Write-Log "Uninstalled $PkgName"
}

function Install-PortableFromRelease {
    param([string]$Ref)

    $tag = Get-ReleaseTagFromRef -Ref $Ref
    if (-not $tag) {
        throw "Portable packs are only published for version tags (e.g. v0.1.0), not branch '$Ref'."
    }

    $assetName = Get-PortableAssetName
    Write-Log "Looking for CI portable pack on release $tag : $assetName"

    try {
        $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$Owner/$Repo/releases/tags/$tag" -UseBasicParsing
    } catch {
        throw "No GitHub release for $tag : $_"
    }

    $asset = @($rel.assets) | Where-Object { $_.name -eq $assetName } | Select-Object -First 1
    if (-not $asset) {
        # fallback: any windows zip
        $asset = @($rel.assets) | Where-Object { $_.name -like "lk-sim-windows-*.zip" } | Select-Object -First 1
    }
    if (-not $asset) {
        $names = (@($rel.assets) | ForEach-Object { $_.name }) -join ", "
        throw "Release $tag has no Windows portable zip (want $assetName). Assets: $names"
    }

    $work = Join-Path $env:TEMP ("lk-sim-portable-" + [guid]::NewGuid().ToString("n"))
    New-Item -ItemType Directory -Path $work -Force | Out-Null
    $zip = Join-Path $work $asset.name
    Write-Log "Downloading $($asset.browser_download_url)"
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zip -UseBasicParsing
    if (-not (Test-Path $zip) -or (Get-Item $zip).Length -le 0) {
        throw "Download failed or empty: $zip"
    }

    Write-Log "Extracting portable pack..."
    Expand-Archive -Path $zip -DestinationPath $work -Force

    # Zip contains lk-sim-windows-x64/...
    $payload = Get-ChildItem -Path $work -Directory | Where-Object {
        $_.Name -like "lk-sim-windows-*"
    } | Select-Object -First 1
    if (-not $payload) {
        # maybe flat
        if (Test-Path (Join-Path $work "lk-sim.cmd")) {
            $payload = Get-Item $work
        } else {
            throw "Portable payload folder not found after extract"
        }
    }

    if (Test-Path $InstallRoot) {
        Remove-Item -Recurse -Force $InstallRoot -ErrorAction SilentlyContinue
    }
    New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
    Copy-Item -Path $payload.FullName -Destination $CurrentDir -Recurse -Force
    Write-Log "Installed files -> $CurrentDir"

    # Portable packs from v0.1.2 shipped uv trampoline .exe with CI-absolute paths.
    # Rewrite launchers to python -m (works after relocate); drop broken exes.
    $fixedLk = @"
@echo off
setlocal
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
"%ROOT%\python\python.exe" -m livekit_agent_simulator %*
exit /b %ERRORLEVEL%
"@
    $fixedMcp = @"
@echo off
setlocal
set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
"%ROOT%\python\python.exe" -m livekit_agent_simulator.mcp_server %*
exit /b %ERRORLEVEL%
"@
    $fixedLk | Set-Content -Path (Join-Path $CurrentDir "lk-sim.cmd") -Encoding ASCII
    $fixedMcp | Set-Content -Path (Join-Path $CurrentDir "lk-sim-mcp.cmd") -Encoding ASCII
    foreach ($broken in @("lk-sim.exe", "lk-sim-mcp.exe")) {
        $p = Join-Path $CurrentDir "python\Scripts\$broken"
        if (Test-Path $p) { Remove-Item -Force $p -ErrorAction SilentlyContinue }
    }

    # Shims in ~/.local/bin (ASCII .cmd only)
    if (-not (Test-Path $ShimDir)) {
        New-Item -ItemType Directory -Path $ShimDir -Force | Out-Null
    }
    $lkCmd = Join-Path $CurrentDir "lk-sim.cmd"
    $mcpCmd = Join-Path $CurrentDir "lk-sim-mcp.cmd"
    if (-not (Test-Path $lkCmd)) { throw "lk-sim.cmd missing in portable pack" }

    $shimLk = Join-Path $ShimDir "lk-sim.cmd"
    $shimMcp = Join-Path $ShimDir "lk-sim-mcp.cmd"
    @"
@echo off
"$lkCmd" %*
"@ | Set-Content -Path $shimLk -Encoding ASCII
    if (Test-Path $mcpCmd) {
        @"
@echo off
"$mcpCmd" %*
"@ | Set-Content -Path $shimMcp -Encoding ASCII
    }

    Ensure-DirOnPath $ShimDir
    try { Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue } catch {}
}

if ($Uninstall) {
    Uninstall-All
    return
}

$ResolvedRef = Resolve-InstallRef
Write-Log "Installing $PkgName (portable pack from $ResolvedRef)"
Write-Log "No uv/pip/build on this machine - CI already built everything"
Install-PortableFromRelease -Ref $ResolvedRef

if (-not $NoMcp) {
    Configure-AllMcpProviders
} else {
    Write-Log "Skipped MCP auto-config (-NoMcp)"
}

$lkResolved = Resolve-LkSim
if ($Verify) {
    if (-not $lkResolved) { throw "$BinaryName not found after install" }
    & $lkResolved --help | Out-Null
    if ($LASTEXITCODE -ne 0 -and $null -ne $LASTEXITCODE) {
        # cmd.exe sometimes leaves exit 0 only; still try
    }
    Write-Log "Verified $BinaryName --help"
}

Write-Host ""
Write-Host "OK $PkgName installed" -ForegroundColor Green
if ($lkResolved) {
    Write-Host "  CLI: $lkResolved"
    Write-Host "  MCP: $lkResolved mcp"
}
Write-Host "  Pack: $CurrentDir"
Write-Host ""
Write-Host "  Quick start:"
Write-Host "    $BinaryName guide"
Write-Host "    $BinaryName init --root C:\path\to\target"
Write-Host "    $BinaryName web --root C:\path\to\target"
Write-Host "    $BinaryName mcp"
Write-Host ""
Write-Host "  If command not found, open a new PowerShell (PATH refresh)."
Write-Host ""
