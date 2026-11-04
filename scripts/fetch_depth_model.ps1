$ErrorActionPreference = 'Stop'
$url = 'https://huggingface.co/open-noodle/depth-anything-v2-small/resolve/7eb4c709bd23da475b4b458582af1686110fd3ca/depth/model_int8.onnx'
$sha = '72e616a3443327db0d9af8b8231deb4f4d14e6fa62b9a4f7dd24d1a7da31df37'
$root = Join-Path $env:LOCALAPPDATA 'LuminaPendantPrep\models'
$model = Join-Path $root 'depth-anything-v2-small-int8.onnx'
New-Item -ItemType Directory -Force -Path $root | Out-Null
if (Test-Path -LiteralPath $model) {
    if ((Get-FileHash -LiteralPath $model -Algorithm SHA256).Hash.ToLowerInvariant() -eq $sha) { return }
    throw 'Existing model has an unexpected checksum. Replace it manually after inspecting the file.'
}
$partial = Join-Path $root ('download-' + [guid]::NewGuid().ToString('N') + '.part')
try {
    Invoke-WebRequest -Uri $url -OutFile $partial
    if ((Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash.ToLowerInvariant() -ne $sha) { throw 'Depth model checksum mismatch.' }
    Move-Item -LiteralPath $partial -Destination $model
} finally {
    if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial -Force }
}
