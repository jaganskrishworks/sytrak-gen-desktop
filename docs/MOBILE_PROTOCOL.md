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
 Station ──(6) nfc_write────────▶ phone, user taps tag, presses WRITE
 Phone ──(7) nfc_result─────────▶ station (with read-back)
```

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
| phone → station | `evt/nfc_result` | `{"seq": "0001", "ok": true, "error": "", "read_back": "E9EBA0ED7C7C"}` |
| phone → station | `evt/nfc_read_result` | `{"ok": true, "text": "E9EBA0ED7C7C", "error": ""}`: content of a tag read for verification (no write). Also sent when the operator starts the read on the phone. |
| station → phone | `cmd/nfc_read` | `{"seq": "0001", "mac": "E9EBA0ED7C7C", "expected": "E9EBA0ED7C7C"}`: read the tag (nothing is written) and report its text |
| station → phone | `cmd/session`    | `{"state": "paired" \| "unpaired", "station": "S", "name": "..."}` (retained) |
| station → phone | `cmd/verify_request` | `{"mac": "E9EBA0ED7C7C", "seq": "0001", "device_type": "UWB-K4W"}`: the device the PC just read. Show it and ask the operator to scan its sticker. |
| station → phone | `cmd/scan_ack`   | `{"accepted": true, "reason": ""}` |
| station → phone | `cmd/verify_result` | `{"ok": true, "mac": "E9EBA0ED7C7C", "seq": "0001", "reason": ""}`: the PC's verdict on the sticker |
| station → phone | `cmd/nfc_write`  | `{"seq": "0001", "mac": "E9:EB:A0:ED:7C:7C", "data": "<text to write>"}` |

* Scans are accepted only while the phone is **paired** (`cmd/session` state `paired`).
  Otherwise `scan_ack` returns `accepted:false`.
* After a reconnect, send `evt/hello` again. The station re-sends the session state.

## 4. QR content

QR codes contain only the MAC, upper-case hex without colons:

```
E9EBA0ED7C7C
```

Send the scanned text **unchanged** in `evt/scan`. The station looks up the SEQ by MAC.

## 5. NFC write (with read-back verification)

When `cmd/nfc_write` arrives:

1. Show "WRITE TO NFC" with the MAC (and the SEQ if it isn't empty), then wait for the user to press **WRITE** and tap the tag.
2. Write `data` to the tag as one NDEF text record (UTF-8, language `en`). It is the same text as the QR.
3. **While the tag is still in range, read it back** and decode the text record.
4. Publish `evt/nfc_result`:
   * success: `ok:true` and `read_back` set to the text that was read from the tag
   * failure: `ok:false` and `error`. This includes a read-back that doesn't match `data`.

The station accepts the write only if `read_back` equals the requested `data`.
A missing `read_back` counts as a failure.

## 6. NFC read (verification only)

When `cmd/nfc_read` arrives, or the operator taps **Verify NFC tag (read only)**:

1. Ask the operator to tap the tag. **Nothing is written.**
2. Read the first NDEF text record and show it next to `expected` (✔ match / ✖ wrong tag).
3. Publish `evt/nfc_read_result` with `ok:true` and the `text` read, or `ok:false` plus `error`
   (e.g. an empty or non-NDEF tag).

The station accepts the tag only if `text` equals the device MAC without colons, and then sets
`NFC Verified = Yes` in the sheet. With **Write + Verify NFC** on the PC, `cmd/nfc_read` follows
automatically after a successful write. If the write screen is still open, the phone queues the
read and opens it when that screen closes.

## Testing without a phone

`python tools/mobile_sim.py` behaves exactly like the phone (needs `pip install -r requirements-dev.txt`).
