# Builds a clean zip to send to other people. Only the files listed here go in, so .env and .venv can never leak.
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$output = Join-Path (Split-Path $root) 'ecom-agent-dist.zip'
$staging = Join-Path ([IO.Path]::GetTempPath()) ("ecom-agent-dist-" + [guid]::NewGuid().ToString('N'))
$target = Join-Path $staging 'ecom-agent'
$files = @('start.bat', 'start.ps1', 'package.ps1', 'requirements.txt', 'README.md', 'LICENSE', '.env.example', '.gitignore')
$folders = @('app', 'static', 'tests', 'docs')
try {
    New-Item -ItemType Directory -Force $target | Out-Null
    foreach ($file in $files) { Copy-Item (Join-Path $root $file) $target }
    foreach ($folder in $folders) { Copy-Item (Join-Path $root $folder) $target -Recurse }
    Get-ChildItem $target -Recurse -Directory -Force -Filter '__pycache__' | Remove-Item -Recurse -Force
    if (Test-Path $output) { Remove-Item $output -Force }
    & tar.exe -a -c -f $output -C $staging 'ecom-agent'
    if ($LASTEXITCODE) { throw 'Could not create the zip.' }
} finally {
    Remove-Item $staging -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Host "Created $output"
