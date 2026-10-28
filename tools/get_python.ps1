# Downloads the latest stable CPython (python-build-standalone) for Windows,
# verifies its SHA256 checksum and extracts it into -Dest.
# Called by start.cmd - you do not need to run it yourself.
param(
    [Parameter(Mandatory = $true)][string]$Dest,
    [string]$Version = ""
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "aarch64" } else { "x86_64" }
$triple = "$arch-pc-windows-msvc"
$headers = @{ "User-Agent" = "AdvancedCtrlF" }

Write-Host "[Advanced Ctrl+F] Looking up the latest Python version ..."
$rel = Invoke-RestMethod -UseBasicParsing -Headers $headers `
    -Uri "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"

# Only stable install_only builds (no a/b/rc pre-releases, no free-threaded builds)
$pattern = '^cpython-(\d+)\.(\d+)\.(\d+)\+\d+-' + [regex]::Escape($triple) + '-install_only\.tar\.gz$'
$cands = foreach ($a in $rel.assets) {
    if ($a.name -match $pattern) {
        [pscustomobject]@{
            Name = $a.name; Url = $a.browser_download_url
            Ver  = [version]("{0}.{1}.{2}" -f $Matches[1], $Matches[2], $Matches[3])
        }
    }
}
if ($Version) {
    $cands = $cands | Where-Object { ("{0}.{1}.{2}" -f $_.Ver.Major, $_.Ver.Minor, $_.Ver.Build) -like "$Version*" }
}
$best = $cands | Sort-Object Ver -Descending | Select-Object -First 1
if (-not $best) { throw "No matching Python found in the release." }
if (-not $best.Url.StartsWith("https://")) { throw "Insecure download URL." }

$dl = Join-Path $Dest "_download"
if (Test-Path $dl) { Remove-Item -Recurse -Force $dl }
New-Item -ItemType Directory -Force -Path $dl | Out-Null
$file = Join-Path $dl "python.tar.gz"

Write-Host "[Advanced Ctrl+F] Downloading $($best.Name) ..."
Invoke-WebRequest -UseBasicParsing -Headers $headers -Uri $best.Url -OutFile $file

$sumsUrl = "https://github.com/astral-sh/python-build-standalone/releases/download/$($rel.tag_name)/SHA256SUMS"
$sums = (Invoke-WebRequest -UseBasicParsing -Headers $headers -Uri $sumsUrl).Content
if ($sums -is [byte[]]) { $sums = [Text.Encoding]::UTF8.GetString($sums) }
$line = ($sums -split "`n") | Where-Object { $_ -match ("\s" + [regex]::Escape($best.Name) + "\s*$") } | Select-Object -First 1
if (-not $line) { Remove-Item -Recurse -Force $dl; throw "No checksum found - aborting." }
$expected = ($line -split "\s+")[0].ToLower()
$actual = (Get-FileHash -Algorithm SHA256 -Path $file).Hash.ToLower()
if ($expected -ne $actual) { Remove-Item -Recurse -Force $dl; throw "Checksum mismatch - download discarded." }

Write-Host "[Advanced Ctrl+F] Checksum OK, extracting ..."
& tar.exe -xzf $file -C $Dest
if ($LASTEXITCODE -ne 0) { throw "Extraction failed (tar.exe)." }
Remove-Item -Recurse -Force $dl
