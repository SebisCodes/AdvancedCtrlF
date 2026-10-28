# Downloads the latest stable official CPython for Windows (the Python Software
# Foundation's NuGet package), verifies it and extracts it into -Dest\python.
#   1. SHA512 of the package must match the hash published in the NuGet catalog
#   2. python.exe must carry a valid Authenticode signature from the
#      Python Software Foundation (signed binaries are not blocked by
#      Smart App Control / Defender reputation checks)
# Called by start.cmd - you do not need to run it yourself.
param(
    [Parameter(Mandatory = $true)][string]$Dest,
    [string]$Version = ""
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$id = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "pythonarm64" } else { "python" }
$headers = @{ "User-Agent" = "AdvancedCtrlF" }
$api = "https://api.nuget.org/v3-flatcontainer/$id"

Write-Host "[Advanced Ctrl+F] Looking up the latest Python version ..."
$versions = (Invoke-RestMethod -UseBasicParsing -Headers $headers -Uri "$api/index.json").versions |
    Where-Object { $_ -match '^\d+\.\d+\.\d+$' }                     # stable releases only
if ($Version) { $versions = $versions | Where-Object { $_ -like "$Version*" } }
$ver = $versions | Sort-Object { [version]$_ } -Descending | Select-Object -First 1
if (-not $ver) { throw "No matching Python version found." }

$tmp = Join-Path $Dest "_download"
if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$pkg = Join-Path $tmp "python.zip"

Write-Host "[Advanced Ctrl+F] Downloading official Python $ver ..."
Invoke-WebRequest -UseBasicParsing -Headers $headers -Uri "$api/$ver/$id.$ver.nupkg" -OutFile $pkg

# 1) package hash from the NuGet catalog
$reg = Invoke-RestMethod -UseBasicParsing -Headers $headers `
    -Uri "https://api.nuget.org/v3/registration5-semver1/$id/$ver.json"
$leaf = Invoke-RestMethod -UseBasicParsing -Headers $headers -Uri $reg.catalogEntry
if ($leaf.packageHashAlgorithm -ne "SHA512") { throw "Unexpected hash algorithm." }
$sha = [Security.Cryptography.SHA512]::Create()
$stream = [IO.File]::OpenRead($pkg)
try { $actual = [Convert]::ToBase64String($sha.ComputeHash($stream)) } finally { $stream.Dispose() }
if ($actual -ne $leaf.packageHash) { Remove-Item -Recurse -Force $tmp; throw "Checksum mismatch - download discarded." }
Write-Host "[Advanced Ctrl+F] Checksum OK, extracting ..."

$unpacked = Join-Path $tmp "pkg"
New-Item -ItemType Directory -Force -Path $unpacked | Out-Null
try { & tar.exe -xf $pkg -C $unpacked 2>$null } catch { }        # fast (Windows 10 1803+)
if (-not (Test-Path (Join-Path $unpacked "tools\python.exe"))) {    # fallback: built-in cmdlet
    Expand-Archive -LiteralPath $pkg -DestinationPath $unpacked -Force
}
$exe = Join-Path $unpacked "tools\python.exe"

# 2) Authenticode signature of python.exe
$sig = Get-AuthenticodeSignature -FilePath $exe
if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch "Python Software Foundation") {
    Remove-Item -Recurse -Force $tmp
    throw "python.exe is not validly signed by the Python Software Foundation - aborting."
}
Write-Host "[Advanced Ctrl+F] Signature OK (Python Software Foundation)."

$target = Join-Path $Dest "python"
if (Test-Path $target) { Remove-Item -Recurse -Force $target }
Move-Item -LiteralPath (Join-Path $unpacked "tools") -Destination $target
Remove-Item -Recurse -Force $tmp
