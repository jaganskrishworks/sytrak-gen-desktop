# Read the nRF52833 MAC address and device ID over J-Link.
# Needs only the SEGGER J-Link software (no Python, no other files).
#
#   powershell -ExecutionPolicy Bypass -File read_mac.ps1
#   powershell -ExecutionPolicy Bypass -File read_mac.ps1 -Serial 69730362
#   powershell -ExecutionPolicy Bypass -File read_mac.ps1 -JLink "C:\Program Files\SEGGER\JLink_V880\JLink.exe"
#
# Reads two FICR registers in one J-Link session:
#   DEVICEADDR  mem32 0x100000A4, 2  -> BLE static address (the MAC used by SyTrak Gen)
#               e.g. "100000A4 = A0ED7C7C 0000E9EB"  ->  E9:EB:A0:ED:7C:7C  ->  E9EBA0ED7C7C
#   DEVICEID    mem32 0x10000060, 2  -> 64-bit unique chip ID (same as
#               "nrfutil device read --address 0x10000060 --bytes 8")
param(
    [string]$Serial = "",
    [string]$Device = "nRF52833_xxAA",
    [string]$Speed = "4000",
    [string]$JLink = "",
    [switch]$Raw
)

if (-not $JLink) {
    # SEGGER's JLink.exe (not Java's jlink.exe); newest installed version first
    $JLink = Get-ChildItem "$env:ProgramFiles\SEGGER", "${env:ProgramFiles(x86)}\SEGGER" -Filter JLink.exe -Recurse -ErrorAction SilentlyContinue |
        Sort-Object { $_.Directory.Name } -Descending | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $JLink -or -not (Test-Path $JLink)) {
    Write-Host "SEGGER J-Link software not found. Install it, or pass -JLink <path to JLink.exe>" -ForegroundColor Red
    exit 1
}

$script = Join-Path $env:TEMP "sytrak_read_mac.jlink"
[IO.File]::WriteAllText($script, "mem32 0x100000A4, 2`r`nmem32 0x10000060, 2`r`nexit`r`n")

$jlinkArgs = @("-NoGui", "1", "-ExitOnError", "1")
if ($Serial) { $jlinkArgs += @("-USB", $Serial) }
$jlinkArgs += @("-Device", $Device, "-If", "SWD", "-Speed", $Speed, "-AutoConnect", "1", "-CommandFile", $script)

Write-Host "J-Link: $JLink"
$output = & $JLink @jlinkArgs 2>&1 | Out-String
try { [IO.File]::Delete($script) } catch {}
if ($Raw) { Write-Host $output }

function Read-Words([string]$address) {
    $m = [regex]::Match($output, "$address\s*=\s*([0-9A-Fa-f]{8})\s+([0-9A-Fa-f]{8})")
    if ($m.Success) { return @([Convert]::ToUInt32($m.Groups[1].Value, 16), [Convert]::ToUInt32($m.Groups[2].Value, 16)) }
    return $null
}

# Bytes in memory order (little-endian words), as nrfutil prints them with --width 8
function Memory-Bytes([uint32[]]$words) {
    ($words | ForEach-Object { [BitConverter]::GetBytes($_) | ForEach-Object { "{0:X2}" -f $_ } }) -join " "
}

$addr = Read-Words "100000A4"
$id = Read-Words "10000060"
if (-not $addr -or -not $id) {
    Write-Host "FAILED: could not read the device. J-Link output:" -ForegroundColor Red
    Write-Host $output
    exit 2
}

$top = ($addr[1] -band 0xFFFF) -bor 0xC000       # BLE static address: two MSBs set
$mac = "{0:X4}{1:X8}" -f $top, $addr[0]
$withColons = ($mac -split "(..)" -ne "") -join ":"
$deviceId = "{0:X8}{1:X8}" -f $id[1], $id[0]       # DEVICEID[1] = high word, DEVICEID[0] = low word

Write-Host ""
Write-Host "MAC        $withColons   ->   $mac" -ForegroundColor Green
Write-Host "DEVICE ID  $deviceId   (bytes in memory: $(Memory-Bytes $id))" -ForegroundColor Green
exit 0
