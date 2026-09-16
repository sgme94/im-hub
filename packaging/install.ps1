[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\im-hub\0.4.0rc1'),
    [string]$PythonExe = 'python',
    [string]$ChatLabDirectory = '',
    [switch]$InstallBackend,
    [switch]$Desktop
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$bundle = $PSScriptRoot
$wheels = @(Get-ChildItem -LiteralPath $bundle -Filter 'im_hub-0.4.0rc1-*.whl' -File)
if ($wheels.Count -ne 1) { throw 'Expected exactly one im-hub wheel alongside install.ps1.' }
$wheel = $wheels[0]
$hashes = Get-Content -LiteralPath (Join-Path $bundle 'FILES.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$expected = $hashes.files.($wheel.Name)
$stream = [IO.File]::OpenRead($wheel.FullName)
$hasher = [Security.Cryptography.SHA256]::Create()
try { $actual = [BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
finally { $stream.Dispose(); $hasher.Dispose() }
if (-not $expected -or $actual -ne $expected) {
    throw 'Wheel checksum mismatch; installation stopped.'
}
if (Test-Path -LiteralPath $InstallDir) { throw 'Use a NEW versioned InstallDir; existing files are never overwritten.' }
& $PythonExe -c 'import sys;sys.exit(0 if sys.version_info >= (3,11) else 2)'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11 or newer is required.' }
if ($ChatLabDirectory -and $InstallBackend) { throw 'Choose an existing ChatLabDirectory OR InstallBackend, not both.' }
if ($ChatLabDirectory) {
    $meta = Get-Content -LiteralPath (Join-Path $ChatLabDirectory 'package.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($meta.name -ne 'chatlab-cli' -or $meta.version -ne '0.37.1') { throw 'Only the verified chatlab-cli 0.37.1 backend is accepted.' }
    $ChatLabDirectory = [IO.Path]::GetFullPath($ChatLabDirectory)
}
New-Item -ItemType Directory -Path $InstallDir | Out-Null
& $PythonExe -m venv (Join-Path $InstallDir 'runtime')
if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed; partial files retained for diagnosis.' }
$py = Join-Path $InstallDir 'runtime\Scripts\python.exe'
& $py -m pip install --no-index --no-deps $wheel.FullName
if ($LASTEXITCODE -ne 0) { throw 'Local wheel installation failed.' }
if ($Desktop) {
    & $py -m pip install 'pywin32>=306' 'uiautomation>=2.0.20,<3'
    if ($LASTEXITCODE -ne 0) { throw 'Optional desktop dependencies failed to install.' }
}
if ($InstallBackend) {
    $backend = Join-Path $InstallDir 'backend'
    New-Item -ItemType Directory -Path $backend | Out-Null
    & npm.cmd install --prefix $backend --no-save --package-lock=false 'chatlab-cli@0.37.1'
    if ($LASTEXITCODE -ne 0) { throw 'Pinned backend installation failed. Node/npm must already be installed.' }
    $ChatLabDirectory = Join-Path $backend 'node_modules\chatlab-cli'
}
$config = @{schema='im-hub-runtime/1';chatlab_directory=$ChatLabDirectory}
$config | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $InstallDir 'runtime\im-hub-runtime.json') -Encoding UTF8
# The native console launcher uses the versioned interpreter, not cmd.exe argument forwarding.
Copy-Item -LiteralPath (Join-Path $InstallDir 'runtime\Scripts\im-hub.exe') -Destination (Join-Path $InstallDir 'im-hub.exe')
Copy-Item -LiteralPath (Join-Path $bundle 'docs') -Destination (Join-Path $InstallDir 'docs') -Recurse
Copy-Item -LiteralPath (Join-Path $bundle 'examples') -Destination (Join-Path $InstallDir 'examples') -Recurse
& $py -m im_hub --version
if ($LASTEXITCODE -ne 0) { throw 'Installed CLI self-check failed.' }
@{schema='im-hub-install/1';version='0.4.0rc1';cli=(Join-Path $InstallDir 'im-hub.exe');desktop_extra=[bool]$Desktop;backend_configured=[bool]$ChatLabDirectory;services_installed=$false;scheduled_tasks_created=$false;global_path_modified=$false;production_desktop_accepted=$false} |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $InstallDir 'INSTALL.json') -Encoding UTF8
Write-Host ('Installed: ' + (Join-Path $InstallDir 'im-hub.exe'))
Write-Host 'No source profiles or timers are enabled automatically. See docs/DELIVERY.md for verified capabilities and remaining acceptance gates.'
