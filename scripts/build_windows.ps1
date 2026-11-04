$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Missing .venv; install the project and PyInstaller first.' }
Set-Location -LiteralPath $projectRoot
$version = & $python -c "from lumina_pendant_prep import __version__; print(__version__)"
if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid project version.' }
$revision = & git rev-parse --short HEAD
if ($LASTEXITCODE -ne 0 -or $revision -notmatch '^[0-9a-f]+$') { throw 'Unable to identify the current revision.' }
$installRoot = Join-Path $env:LOCALAPPDATA 'LuminaPendantPrep'
$versionsRoot = Join-Path $installRoot 'versions'
$buildRoot = Join-Path $installRoot ('staging-' + [guid]::NewGuid().ToString('N'))
$versionName = "$version-$revision"
$destination = Join-Path $versionsRoot $versionName
$currentFile = Join-Path $installRoot 'current.txt'
New-Item -ItemType Directory -Force -Path $versionsRoot | Out-Null
if (Test-Path -LiteralPath $destination) { throw "This version already exists: $destination" }

function Remove-ManagedPath([string]$path) {
    if (Test-Path -LiteralPath $path) {
        $root = (Resolve-Path -LiteralPath $installRoot).Path
        $resolved = (Resolve-Path -LiteralPath $path).Path
        if (-not $resolved.StartsWith($root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove a path outside the install directory: $resolved"
        }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}

$dist = Join-Path $buildRoot 'dist'
$work = Join-Path $buildRoot 'work'
& $python -m PyInstaller --noconfirm --distpath $dist --workpath $work LuminaPendantPrep.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed; staging kept at $buildRoot" }
$candidate = Join-Path $dist 'LuminaPendantPrep'
$exe = Join-Path $candidate 'LuminaPendantPrep.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw 'Candidate executable not found.' }
$photo = (Resolve-Path -LiteralPath 'tests\fixtures\cat-photo.jpg').Path
$smoke = Join-Path $buildRoot 'smoke.png'
$oldQtPlatform = $env:QT_QPA_PLATFORM
try {
    $env:QT_QPA_PLATFORM = 'offscreen'
    $proc = Start-Process -FilePath $exe -ArgumentList @('--smoke-test', ('"' + $photo + '"'), ('"' + $smoke + '"')) -WindowStyle Hidden -PassThru
    if (-not $proc.WaitForExit(120000)) { $proc.Kill(); throw 'Candidate smoke test timed out.' }
    if ($proc.ExitCode -ne 0) { throw "Candidate smoke test failed: $($proc.ExitCode)" }
    & $python -c "from PIL import Image; import sys; p=Image.open(sys.argv[1]); assert p.size==(1920,1080) and p.mode=='RGBA'; assert p.getpixel((0,0))[3]==0 and p.getpixel((960,130))[3]==0" $smoke
    if ($LASTEXITCODE -ne 0) { throw 'Candidate PNG validation failed.' }
} finally { $env:QT_QPA_PLATFORM = $oldQtPlatform }

$previous = if (Test-Path -LiteralPath $currentFile) { (Get-Content -LiteralPath $currentFile -Raw).Trim() } else { '' }
Move-Item -LiteralPath $candidate -Destination $destination
@{version=$version; revision=$revision; installed_at=(Get-Date).ToString('s'); managed_by='LuminaPendantPrep'} |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $destination 'BUILD_INFO.json') -Encoding utf8
$temporaryPointer = Join-Path $installRoot ('current-' + [guid]::NewGuid().ToString('N') + '.tmp')
[IO.File]::WriteAllText($temporaryPointer, $versionName, [Text.Encoding]::ASCII)
Move-Item -LiteralPath $temporaryPointer -Destination $currentFile -Force
Remove-ManagedPath $buildRoot
if ($previous -match '^\d+\.\d+\.\d+-[0-9a-f]+$' -and $previous -ne $versionName) {
    $old = Join-Path $versionsRoot $previous
    $marker = Join-Path $old 'BUILD_INFO.json'
    if ((Test-Path -LiteralPath $marker) -and ((Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json).managed_by -eq 'LuminaPendantPrep')) {
        Remove-ManagedPath $old
    }
}
Write-Host "Installed Lumina Pendant Prep $version ($revision) at $destination"
