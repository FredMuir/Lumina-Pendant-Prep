$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Missing .venv; install the project and PyInstaller first.' }
$version = & $python -c "from lumina_pendant_prep import __version__; print(__version__)"
if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid project version.' }
$distRoot = Join-Path $projectRoot 'dist'
$destination = Join-Path $distRoot 'LuminaPendantPrep'
$backup = Join-Path $distRoot 'LuminaPendantPrep.previous'
$candidateRoot = Join-Path $projectRoot ".artifacts\release-$version"
$candidate = Join-Path $candidateRoot 'LuminaPendantPrep'
$work = Join-Path $projectRoot "build\release-$version"

function Remove-ProjectPath([string]$path) {
    if (Test-Path -LiteralPath $path) {
        $resolved = (Resolve-Path -LiteralPath $path).Path
        if (-not $resolved.StartsWith($projectRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove a path outside the project: $resolved"
        }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}

Set-Location -LiteralPath $projectRoot
if (Test-Path -LiteralPath $backup) { throw "Previous backup exists; inspect it before continuing: $backup" }
& $python -m PyInstaller --noconfirm --distpath $candidateRoot --workpath $work LuminaPendantPrep.spec
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
$exe = Join-Path $candidate 'LuminaPendantPrep.exe'
if (-not (Test-Path -LiteralPath $exe)) { throw 'Candidate executable not found.' }
$photo = (Resolve-Path -LiteralPath 'tests\fixtures\cat-photo.jpg').Path
$smoke = Join-Path $candidateRoot 'smoke.png'
$oldQtPlatform = $env:QT_QPA_PLATFORM
try {
    $env:QT_QPA_PLATFORM = 'offscreen'
    $proc = Start-Process -FilePath $exe -ArgumentList @('--smoke-test', ('"' + $photo + '"'), ('"' + $smoke + '"')) -WindowStyle Hidden -PassThru
    if (-not $proc.WaitForExit(120000)) { $proc.Kill(); throw 'Candidate smoke test timed out.' }
    if ($proc.ExitCode -ne 0) { throw "Candidate smoke test failed: $($proc.ExitCode)" }
    & $python -c "from PIL import Image; import sys; p=Image.open(sys.argv[1]); assert p.size==(1920,1080) and p.mode=='RGBA'; assert p.getpixel((0,0))[3]==0 and p.getpixel((960,130))[3]==0" $smoke
    if ($LASTEXITCODE -ne 0) { throw 'Candidate PNG validation failed.' }
} finally { $env:QT_QPA_PLATFORM = $oldQtPlatform }
New-Item -ItemType Directory -Force -Path $distRoot | Out-Null
if (Test-Path -LiteralPath $destination) { Move-Item -LiteralPath $destination -Destination $backup }
try {
    Move-Item -LiteralPath $candidate -Destination $destination
    if (-not (Test-Path -LiteralPath (Join-Path $destination 'LuminaPendantPrep.exe'))) { throw 'Installed executable not found.' }
} catch {
    if ((-not (Test-Path -LiteralPath $destination)) -and (Test-Path -LiteralPath $backup)) {
        Move-Item -LiteralPath $backup -Destination $destination
    }
    throw
}
$revision = & git rev-parse --short HEAD
@{version=$version; revision=$revision; installed_at=(Get-Date).ToString('s')} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $destination 'BUILD_INFO.json') -Encoding utf8
Remove-ProjectPath $backup
Remove-ProjectPath $candidateRoot
Remove-ProjectPath $work
Write-Host "Installed Lumina Pendant Prep $version ($revision) at $destination"
