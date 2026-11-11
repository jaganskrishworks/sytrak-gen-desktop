# SyTrak Gen – Mobile App Protocol (v1)

How the mobile app talks to a SyTrak Gen station (the Windows PC app).

Every station PC runs its **own** MQTT broker. Several stations can share one
Wi-Fi network, and each operator pairs with their own phone. A phone talks to
one station at a time.

```
 Phone ──(1) UDP discovery──────▶ all stations on the LAN
 Phone ──(2) MQTT connect───────▶ chosen station  <pc-ip>:1883
 PC operator selects the phone and clicks Connect  ──▶ phone gets "paired"
 PC reads the device MAC (J-Link) and finds its SEQ in the sheet
 Station ──(3) verify_request───▶ phone shows SEQ + MAC, operator scans the sticker
 Phone ──(4) scan───────────────▶ station: sticker MAC == device MAC ?
 Station ──(5) verify_result────▶ phone shows Verified / Not verified
 Station ──(6) nfc_job──────────▶ phone: "hold the tag" (write or verify)
 Phone ──(7) nfc_tag────────────▶ station: UID + every page;  station signs it
 Station ──(8) nfc_job_reply────▶ phone: pages to write + the tag password
 Phone ──(9) nfc_done───────────▶ station: pages read afterwards; station judges the tag
 Station ──(10) nfc_verdict─────▶ phone shows every check
```

NFC work is **always started by the station**. The phone has no NFC write/verify buttons: it only
moves bytes between the tag and the station. The station signs, derives passwords and decides.
The only NFC button on the phone is the **NFC Data Viewer** (home screen): read only, no station,
it shows UID, chip, MAC, signature record (version, key ID, r/s), protection (AUTH0, ACCESS, lock
bits), every NDEF record and every page. It cannot show the password (an NTAG reads it back as 00)
or judge the signature (that needs the trusted key: the station does it in Verification 2).

## 1. Discover stations (UDP, port 47474)

* Stations broadcast a beacon every 2 s to `255.255.255.255:47474`.
* To get an answer straight away, the phone can also broadcast the ASCII bytes
  `SYTRAK_DISCOVER` to port 47474. Every station replies directly with its beacon.

Beacon (JSON):

```json
{"type":"sytrak-gen","v":1,"station":"SG-KRISHWORKSPC-19","name":"Line 1 PC",
 "mqtt_port":1883,"auth":false,"paired":""}
```

The station's IP address is the **source address of the UDP packet**. Show the
user a list of stations (`name`, `station`). `paired` holds the ID of the mobile
the station is currently paired with (empty means free).

## 2. Connect (MQTT 3.1.1 over TCP)

| Setting      | Value                                                        |
|--------------|--------------------------------------------------------------|
| Host / port  | beacon source IP / `mqtt_port`                               |
| Client ID    | a stable, unique ID for the phone, e.g. `mob-<uuid>`. **This is the mobile ID `M`** |
| Username/pwd | only if `auth` is true: user `sytrak` + the station password |
| Clean session| true                                                         |
| Last will    | topic `sytrak/S/mobile/M/status`, payload `offline`, retain   |

After connecting:

1. Subscribe to `sytrak/S/mobile/M/cmd/#` (QoS 1).
2. Publish `online` to `sytrak/S/mobile/M/status` (retain).
3. Publish hello: `sytrak/S/mobile/M/evt/hello` → `{"name":"Pixel 7 – Ravi"}`

`S` is the station ID from the beacon and `M` is your client ID. A phone may only
publish under its **own** `M`; anything else is ignored.

## 3. Topics

| Direction       | Topic (`sytrak/S/mobile/M/…`) | Payload |
|-----------------|-------------------------------|---------|
| phone → station | `evt/hello`      | `{"name": "<display name>"}` |
| phone → station | `evt/scan`       | `{"qr": "<exact scanned QR text>"}` |
| station → phone | `cmd/session`    | `{"state": "paired" \| "unpaired", "station": "S", "name": "..."}` (retained) |
| station → phone | `cmd/verify_request` | `{"mac": "E9EBA0ED7C7C", "seq": "0001", "device_type": "UWB-K4W"}`: the device the PC just read. Show it and ask the operator to scan its sticker. |
| station → phone | `cmd/scan_ack`   | `{"accepted": true, "reason": ""}` |
| station → phone | `cmd/verify_result` | `{"ok": true, "mac": "E9EBA0ED7C7C", "seq": "0001", "reason": ""}`: the PC's verdict on the sticker |
| station → phone | `cmd/nfc_job` | `{"job_id": "j-…", "mode": "write" \| "verify", "mac": "E9EBA0ED7C7C", "seq": "0001"}` (section 5) |
| phone → station | `evt/nfc_tag` | `{"job_id", "attempt", "uid", "version", "pages"}`: first touch of the tag |
| station → phone | `cmd/nfc_job_reply` | `{"job_id", "ok", "reason", "write": {"page": 4, "data": "<hex>"} \| null, "pwd", "pack", "lock": false}` |
| phone → station | `evt/nfc_progress` | `{"job_id", "step", "ok", "detail"}`: one per step, for the PC log |
| phone → station | `evt/nfc_done` | `{"job_id", "ok", "step", "error", "uid", "version", "pages", "password", "pwd_written", "locked", "steps"}` |
| station → phone | `cmd/nfc_verdict` | `{"job_id", "ok", "mode", "headline", "uid", "mac", "seq", "checks": [{"title", "state", "detail"}], "error", "sheet"}` (may come twice: before and after the sheet is saved) |
| station → phone | `cmd/nfc_cancel` | `{"job_id", "reason"}` |

* Scans are accepted only while the phone is **paired** (`cmd/session` state `paired`).
  Otherwise `scan_ack` returns `accepted:false`.
* After a reconnect, send `evt/hello` again. The station re-sends the session state.

## 4. QR content

QR codes contain only the MAC, upper-case hex without colons:

```
E9EBA0ED7C7C
```

Send the scanned text **unchanged** in `evt/scan`. The station looks up the SEQ by MAC.

## 5. NFC jobs: the signed sticker (v1)

Sticker format: `docs/NFC_STICKER_PLAN.md` and `nfc_sticker_v1.md`. NTAG213/215/216, fixed
7-byte UID; NDEF record 1 = Text with the MAC, record 2 = `sytrak:s` with an ECDSA P-192
signature of `sytraknfcv1-<UID>-<MAC>`; per-tag password PWD/PACK. Raw commands: GET_VERSION `60`,
READ `30 pg`, WRITE `A2 pg d0..d3`, PWD_AUTH `1B p0..p3`. Android only (NfcA).

When `cmd/nfc_job` arrives (ignore a repeat of the job that is already open):

1. Open the NFC screen and start reading. The operator holds the tag to the phone.
2. On a tap: GET_VERSION, READ every page from 0 to PACK. Publish `evt/nfc_tag`
   (`uid`, `version`, `pages` as hex). Wait 5 s for `cmd/nfc_job_reply`; resend the same data up
   to 3 times (the station answers a resend with the same reply).
3. `ok:false`: show `reason`; the operator may tap another tag (back to step 2).
4. `mode` write (Verification 1):
   1. If CFGLCK is set: stop. If AUTH0 ≤ PACK page (already protected): PWD_AUTH first.
   2. WRITE `write.data` page by page from `write.page`. On an error: reconnect (+ PWD_AUTH if
      protected) and resume from the failed page, max 3 tries.
   3. Read back and compare; rewrite from the first differing page, max 3 rounds.
   4. WRITE PWD to page CFG0+2, `PACK0 PACK1 00 00` to CFG0+3. Reconnect, PWD_AUTH: the answer
      must equal `pack` → `password: "validated"`, else `"failed"`.
   5. If `lock` is true (Settings › Lock tags, on by default) and the password was validated:
      WRITE CFG0+1 = `ACCESS & 0x38` + CFG1 bytes 1-3, then CFG0 = CFG0 bytes 0-2 + `02` (last),
      read CFG0 back → `locked`. Never lock with an unvalidated password; never lock bits or CFGLCK.
5. `mode` verify (Verification 2): **nothing is written**. PWD_AUTH with `pwd`; a refusal halts
   the tag, so reconnect before the final read.
6. READ every page again and publish `evt/nfc_done` (`ok:false` + `step` + `error` when a step
   failed). Send `evt/nfc_progress` for each step on the way.
7. Show `cmd/nfc_verdict`: every check (signature present / valid, MAC = device, password,
   locked, …) and the sheet note.

`pwd`/`pack` are kept in memory only for the job, wiped afterwards, never logged or stored.
Closing the screen early sends `evt/nfc_done` with `ok:false`. `cmd/nfc_cancel` closes the job.

The station judges the tag from the pages (`core/nfc_job.py`) and writes Verification 1 ›
`NFC Written` or Verification 2 › `NFC Read` (Yes only if every check passed), plus Tag UID,
Signature, Password and Locked.

## Testing without a phone

`python tools/mobile_sim.py` behaves exactly like the phone (needs `pip install -r requirements-dev.txt`).
NFC jobs run on a virtual NTAG213 (`core/ntag_sim.py`), which keeps its memory between jobs.
Failure options: `--swap-tag`, `--tag-dropout 9`, `--wrong-password`, `--random-uid`, `--cfglck`.
