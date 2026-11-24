# Throughline Companion installer for Windows. Needs no GUI: run it in an
# OpenSSH session; the companion then runs as a logon task in your desktop
# session.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File install.ps1 -From SOURCE [-Extras LIST] [setup arguments...]
#
# SOURCE: a folder of wheels, a companion checkout, or a pip/uv requirement.
# Remaining arguments go to `throughline-companion setup` (e.g. --pair -
# --detected --service); `--pair -` reads the code from stdin.
param(
    [Parameter(Mandatory = $true)][string]$From,
    [string]$Extras = "capture,cookies,desktop-plugins",
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Setup
)
$ErrorActionPreference = "Stop"
$UvVersion = "0.13.9"

function Say($text) { [Console]::Error.WriteLine("throughline-companion: $text") }

$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $local = Join-Path $env:USERPROFILE ".local\bin\uv.exe"; if (Test-Path $local) { $uv = $local } }
if (-not $uv) {
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "aarch64" } else { "x86_64" }
    $asset = "uv-$arch-pc-windows-msvc.zip"
    $sums = @{
        "uv-x86_64-pc-windows-msvc.zip" = "edd61f7f5728a56cc62455386661860bdfc7bab65872f39b0c25bdcc1d8e1beb"
        "uv-aarch64-pc-windows-msvc.zip" = "72eed595983a4d7d22d79f21065b0f4479cc7e5c7a89104bb410c8f47559a878"
    }
    $tmp = Join-Path $env:TEMP ("uv-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $tmp | Out-Null
    Say "installing uv $UvVersion"
    Invoke-WebRequest -UseBasicParsing "https://github.com/astral-sh/uv/releases/download/$UvVersion/$asset" -OutFile "$tmp\uv.zip"
    if ((Get-FileHash "$tmp\uv.zip" -Algorithm SHA256).Hash.ToLower() -ne $sums[$asset]) { throw "uv download failed its checksum" }
    Expand-Archive "$tmp\uv.zip" -DestinationPath $tmp
    $bin = Join-Path $env:USERPROFILE ".local\bin"
    New-Item -ItemType Directory -Force -Path $bin | Out-Null
    Copy-Item (Get-ChildItem $tmp -Recurse -Filter uv.exe | Select-Object -First 1).FullName (Join-Path $bin "uv.exe")
    Remove-Item -Recurse -Force $tmp
    $uv = Join-Path $bin "uv.exe"
}

# Windows locks a running program: stop the companion so an update can
# replace it (settings and pairing live in %APPDATA% and are kept).
$wasRunning = $false
& schtasks /Query /TN "Throughline Companion" *> $null
if ($LASTEXITCODE -eq 0) {
    $wasRunning = $true
    & schtasks /End /TN "Throughline Companion" *> $null
    Start-Sleep -Seconds 2
}

if ((Test-Path $From -PathType Container) -and (Get-ChildItem $From -Filter "throughline_companion-*.whl")) {
    Say "installing from wheels in $From"
    & $uv tool install --force --python 3.12 --find-links $From "throughline-companion[$Extras]"
} elseif (Test-Path $From -PathType Container) {
    & $uv tool install --force --python 3.12 "$From[$Extras]"
} else {
    & $uv tool install --force --python 3.12 "throughline-companion[$Extras] @ $From"
}
if ($LASTEXITCODE -ne 0) { throw "uv tool install failed" }
$exe = Join-Path (& $uv tool dir --bin) "throughline-companion.exe"
& $exe --version | Out-Null
Say "installed $exe"
if ($Setup) { & $exe setup @Setup; exit $LASTEXITCODE }
if ($wasRunning) {
    & schtasks /Run /TN "Throughline Companion" *> $null
    Say "service restarted on the new version"
}
