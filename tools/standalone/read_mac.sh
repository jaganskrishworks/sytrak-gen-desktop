#!/usr/bin/env bash
# Read the nRF52833 MAC address and device ID over J-Link (Linux).
# Needs only the SEGGER J-Link software (no Python, no other files).
#
#   ./read_mac.sh                 first J-Link found
#   ./read_mac.sh 69730362        a specific J-Link serial
#   RAW=1 ./read_mac.sh           also print the J-Link output
#   JLINK=/opt/SEGGER/JLink_V880/JLinkExe ./read_mac.sh
#
# Reads two FICR registers in one J-Link session:
#   DEVICEADDR  mem32 0x100000A4, 2  -> BLE static address (the MAC used by SyTrak Gen)
#               e.g. "100000A4 = A0ED7C7C 0000E9EB"  ->  E9:EB:A0:ED:7C:7C  ->  E9EBA0ED7C7C
#   DEVICEID    mem32 0x10000060, 2  -> 64-bit unique chip ID (same as
#               "nrfutil device read --address 0x10000060 --bytes 8")
SERIAL="${1:-}"
DEVICE="${DEVICE:-nRF52833_xxAA}"
SPEED="${SPEED:-4000}"
JLINK="${JLINK:-$(command -v JLinkExe || ls -d /opt/SEGGER/JLink*/JLinkExe 2>/dev/null | sort -V | tail -n 1)}"

if [ -z "$JLINK" ] || [ ! -x "$JLINK" ]; then
    echo "SEGGER J-Link software not found. Install the J-Link .deb, or set JLINK=<path to JLinkExe>"
    exit 1
fi

script="$(mktemp)"
printf 'mem32 0x100000A4, 2\nmem32 0x10000060, 2\nexit\n' > "$script"
args=(-NoGui 1 -ExitOnError 1)
[ -n "$SERIAL" ] && args+=(-USB "$SERIAL")
args+=(-Device "$DEVICE" -If SWD -Speed "$SPEED" -AutoConnect 1 -CommandFile "$script")

echo "J-Link: $JLINK"
output="$("$JLINK" "${args[@]}" 2>&1)"
rm -f "$script"
[ -n "${RAW:-}" ] && echo "$output"

# "<address> = WORD0 WORD1" -> "WORD0 WORD1"
words() { echo "$output" | grep -ioE "$1 *= *[0-9a-f]{8} +[0-9a-f]{8}" | head -n 1 | sed -E 's/^[^=]*= *//'; }
# little-endian bytes of a 32-bit word, as nrfutil prints them with --width 8
le_bytes() { printf '%s %s %s %s' "${1:6:2}" "${1:4:2}" "${1:2:2}" "${1:0:2}"; }

addr="$(words 100000A4)"
id="$(words 10000060)"
if [ -z "$addr" ] || [ -z "$id" ]; then
    echo "FAILED: could not read the device. J-Link output:"
    echo "$output"
    exit 2
fi
read -r low high <<< "$addr"
read -r id0 id1 <<< "$id"
id0="${id0^^}"; id1="${id1^^}"

top=$(( (0x$high & 0xFFFF) | 0xC000 ))       # BLE static address: two MSBs set
mac="$(printf '%04X%08X' "$top" "0x$low")"
echo
echo "MAC        $(echo "$mac" | sed -E 's/(..)/\1:/g; s/:$//')   ->   $mac"
echo "DEVICE ID  $id1$id0   (bytes in memory: $(le_bytes "$id0") $(le_bytes "$id1"))"
