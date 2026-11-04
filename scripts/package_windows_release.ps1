param(
    [Parameter(Mandatory=$true)][string]$BundleDir,
    [Parameter(Mandatory=$true)][string]$OutputDir
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $root '.venv\Scripts\python.exe'
$compiler = Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'
if (-not (Test-Path -LiteralPath $compiler)) { throw 'Inno Setup 6 compiler not found in the current user profile.' }
$bundle = (Resolve-Path -LiteralPath $BundleDir).Path
if (-not (Test-Path -LiteralPath (Join-Path $bundle 'LuminaPendantPrep.exe'))) { throw 'Application executable missing from bundle.' }
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$output = (Resolve-Path -LiteralPath $OutputDir).Path
$version = & $python -c "from lumina_pendant_prep import __version__; print(__version__)"
if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid project version.' }
$staging = Join-Path $env:TEMP ('lpp-release-' + [guid]::NewGuid().ToString('N'))
try {
    New-Item -ItemType Directory -Path $staging | Out-Null
    Copy-Item -Path (Join-Path $bundle '*') -Destination $staging -Recurse
    Remove-Item -LiteralPath (Join-Path $staging 'BUILD_INFO.json') -Force -ErrorAction SilentlyContinue
    $notices = Join-Path $staging 'Third-party licenses'
    New-Item -ItemType Directory -Path $notices | Out-Null
    $pythonLicense = & $python -c "import os,sys; print(os.path.join(sys.base_prefix, 'LICENSE.txt'))"
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pythonLicense)) { throw 'Python license not found.' }
    Copy-Item -LiteralPath $pythonLicense -Destination (Join-Path $notices 'Python-LICENSE.txt')
    $site = Join-Path $root '.venv\Lib\site-packages'
    $names = @('numpy-*dist-info','opencv_python_headless-*dist-info','pillow-*dist-info','pillow_heif-*dist-info','pyside6-*dist-info','pyside6_essentials-*dist-info','shiboken6-*dist-info')
    foreach ($name in $names) {
        foreach ($info in Get-ChildItem -Path (Join-Path $site $name) -Directory) {
            $target = Join-Path $notices $info.Name
            New-Item -ItemType Directory -Path $target | Out-Null
            Copy-Item -LiteralPath (Join-Path $info.FullName 'METADATA') -Destination $target
            Get-ChildItem -LiteralPath $info.FullName -File | Where-Object { $_.Name -match '^(LICENSE|COPYING|NOTICE)' } | Copy-Item -Destination $target
            $licenses = Join-Path $info.FullName 'licenses'
            if (Test-Path -LiteralPath $licenses) { Copy-Item -LiteralPath $licenses -Destination $target -Recurse }
        }
    }
    Invoke-WebRequest 'https://www.gnu.org/licenses/lgpl-3.0.txt' -OutFile (Join-Path $notices 'LGPL-3.0.txt')
    Invoke-WebRequest 'https://www.gnu.org/licenses/gpl-3.0.txt' -OutFile (Join-Path $notices 'GPL-3.0.txt')
    Copy-Item -LiteralPath (Join-Path $root 'docs\THIRD_PARTY.md') -Destination $notices
    Copy-Item -LiteralPath (Join-Path $root 'README.md') -Destination $staging
    & $compiler "/DAppVersion=$version" "/DBundleDir=$staging" "/DOutputDir=$output" (Join-Path $PSScriptRoot 'windows_installer.iss')
    if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed.' }
} finally {
    if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
}
