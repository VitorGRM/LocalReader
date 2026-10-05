<#
.SYNOPSIS
  Gera o instalador do Windows: release\TTSReader-Setup.exe (+ .sha256).

.DESCRIPTION
  1. grava _build_info.py (versão e repositório do GitHub)
  2. PyInstaller -> dist\TTSReader\TTSReader.exe
  3. embute o Tesseract (com idiomas) em dist\TTSReader\tesseract
  4. Inno Setup -> release\TTSReader-Setup.exe
  5. calcula o SHA-256 usado pelo atualizador

  Requisitos locais: Python 3.11+ no PATH. Tesseract e Inno Setup são
  instalados via Chocolatey se não existirem (o runner do GitHub já tem o choco).

.EXAMPLE
  .\build_windows.ps1 -Version v1.0.0 -Repo meuusuario/tts-reader
#>
param(
    [string]$Version = "",
    [string]$Repo = "",
    [switch]$SkipTesseract
)

# "Continue" de propósito: no Windows PowerShell 5.1, "Stop" transformaria qualquer
# linha em stderr do pip/PyInstaller em erro. Programas nativos são checados por Assert-Ok.
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

$TesseractDir = Join-Path $env:ProgramFiles "Tesseract-OCR"
$TesseractLangs = @("por", "spa", "fra", "deu", "ita")  # eng e osd já vêm no Tesseract
$InstallerName = "TTSReader-Setup.exe"

function Assert-Ok($step) {
    if ($LASTEXITCODE -ne 0) { throw "Falha em: $step (código $LASTEXITCODE)" }
}

# --- 1. versão ---------------------------------------------------------------
$semver = "0.0.0"
if ($Version -match '^v?(\d+\.\d+\.\d+)') { $semver = $Matches[1] }
$buildInfo = "__version__ = `"$semver`"`nGITHUB_REPO = `"$Repo`"`n"
[IO.File]::WriteAllText((Join-Path $PSScriptRoot "_build_info.py"), $buildInfo, (New-Object Text.UTF8Encoding $false))
Write-Host "Versão $semver, repositório '$Repo'"

# --- 2. PyInstaller ----------------------------------------------------------
if (-not (Test-Path ".venv-build\Scripts\python.exe")) {
    python -m venv .venv-build; Assert-Ok "criar venv de build"
}
$py = Join-Path $PSScriptRoot ".venv-build\Scripts\python.exe"
& $py -m pip install --upgrade pip; Assert-Ok "atualizar pip"
& $py -m pip install -r requirements.txt pyinstaller; Assert-Ok "instalar dependências"
& $py -m PyInstaller tts_reader.spec --noconfirm --clean; Assert-Ok "PyInstaller"

# --- 3. Tesseract embutido ---------------------------------------------------
if (-not $SkipTesseract) {
    if (-not (Test-Path "$TesseractDir\tesseract.exe")) {
        if (-not (Get-Command choco -ErrorAction SilentlyContinue)) {
            throw "Tesseract não encontrado em '$TesseractDir' e o Chocolatey não está instalado. Instale o Tesseract (build UB Mannheim) ou use -SkipTesseract."
        }
        choco install tesseract -y --no-progress; Assert-Ok "instalar Tesseract"
    }
    if (-not (Test-Path "$TesseractDir\tesseract.exe")) { throw "Tesseract não foi encontrado em '$TesseractDir' após a instalação." }

    $target = "dist\TTSReader\tesseract"
    Copy-Item $TesseractDir $target -Recurse -Force -ErrorAction Stop
    Remove-Item "$target\unins*" -Force -ErrorAction SilentlyContinue
    foreach ($lang in $TesseractLangs) {
        $file = "$target\tessdata\$lang.traineddata"
        if (-not (Test-Path $file)) {
            Invoke-WebRequest "https://github.com/tesseract-ocr/tessdata_fast/raw/main/$lang.traineddata" -OutFile $file -ErrorAction Stop
        }
    }
} else {
    Write-Host "Tesseract NÃO será embutido (-SkipTesseract): o OCR dependerá de uma instalação no PATH."
}

# --- 4. Inno Setup -----------------------------------------------------------
function Find-Iscc {
    $candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    return $null
}
$iscc = Find-Iscc
if (-not $iscc) {
    if (-not (Get-Command choco -ErrorAction SilentlyContinue)) {
        throw "Inno Setup 6 não encontrado. Instale em https://jrsoftware.org/isdl.php"
    }
    choco install innosetup -y --no-progress; Assert-Ok "instalar Inno Setup"
    $iscc = Find-Iscc
    if (-not $iscc) { throw "Inno Setup não foi encontrado após a instalação." }
}
& $iscc "/DAppVersion=$semver" "installer\tts-reader.iss"; Assert-Ok "Inno Setup"

# --- 5. checksum -------------------------------------------------------------
$installer = Join-Path "release" $InstallerName
if (-not (Test-Path $installer)) { throw "O instalador '$installer' não foi gerado." }
$hash = (Get-FileHash $installer -Algorithm SHA256 -ErrorAction Stop).Hash.ToLower()
[IO.File]::WriteAllText((Join-Path $PSScriptRoot "$installer.sha256"), "$hash  $InstallerName`n", (New-Object Text.UTF8Encoding $false))

Write-Host ""
Write-Host "Pronto: $installer"
Write-Host "SHA-256: $hash"
