param(
    [string]$Version = "0.1.0"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

Write-Host "==> Installing PyInstaller"
& $py -m pip install --upgrade pyinstaller

Write-Host "==> Generating icon"
& $py "packaging\make_icon.py"

Write-Host "==> Cleaning"
foreach ($dir in @("build", "dist")) {
    if (Test-Path $dir) { Remove-Item $dir -Recurse -Force }
}

Write-Host "==> Building (onedir)"
& $py -m PyInstaller --noconfirm --clean "packaging\LAFhvst.spec" --distpath "dist" --workpath "build"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }

$zip = Join-Path $root "dist\LAFhvst-$Version-win64.zip"
for ($attempt = 1; $attempt -le 3; $attempt++) {
    try {
        if (Test-Path $zip) { Remove-Item $zip -Force }
        Compress-Archive -Path "dist\LAFhvst\*" -DestinationPath $zip -ErrorAction Stop
        break
    } catch {
        if ($attempt -eq 3) { throw }
        Write-Host "==> Zip retry $attempt ($($_.Exception.Message))"
        Start-Sleep -Seconds 3
    }
}

Write-Host "==> Done: $zip"
