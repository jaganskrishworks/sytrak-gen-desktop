# Signed NFC sticker (v1): implementation plan

Source documents: `sytrak-mapping-mock/nfc_sticker_v1.md`, `mock_server.py`, `sticker_core.py`,
`simulate_flow.py`, `sytrak_mapping_flow.puml`.

## 0. Decisions

| Topic | Decision |
|---|---|
| Who starts NFC work | **Only the PC.** The phone is a remote NFC reader/writer: it does what the PC asks and reports raw tag data. It has no NFC buttons of its own. |
| Who signs | **The PC** acts as the "server" of the spec: it holds the signing key (P-192 PEM) and the password secret, signs `sytraknfcv1-<UID>-<MAC>`, derives PWD/PACK and builds the NDEF bytes. The private key and the secret never reach the phone. |
| Who judges | **The PC.** The phone sends back the raw pages it read; the PC decodes the NDEF, verifies the signature, checks the MAC, the password and the lock state. The PC sends the verdict to the phone so the operator sees it there too. |
| Lock | **On** (Settings › Lock tags, default on). After the password is validated the phone writes ACCESS, then AUTH0 = 02 (spec §6 steps 6-8): writes need the password, reads stay open. Never lock bits or CFGLCK, so a locked tag can be rewritten with its password. With locking on, an open tag fails the "Locked" check in Verification 1 and 2. |
| Secrets | Picked in Settings: local file (Browse…) or downloaded from a URL (From URL…). A private copy is kept in `%APPDATA%\SyTrakGen\secrets\`. Contents are never logged. |
| Phones | Android (raw NfcA / MifareUltralight commands). iPhone is not supported for the signed sticker: the iOS app has no NFC tag-reader entitlement. |

## 1. Sticker format (spec §2)

NTAG21x, fixed 7-byte UID. NDEF TLV from page 4:

1. Text record `T`, `02 'en'` + MAC (12 uppercase hex)
2. External record `sytrak:s`, payload = `0x1K` (version 1, key ID K) + ECDSA P-192 / SHA-256 signature, raw r‖s (48 B)

Password: `HMAC-SHA256(secret, "sytraknfcpwv1-<UID>")` → PWD = bytes 0-3, PACK = bytes 4-5.

## 2. Flow

```
PC: ① read MAC (J-Link) → sheet lookup → ② phone scans the QR sticker (unchanged)
PC: ③ "Write signed NFC" (Verification 1) or "Verify NFC" (Verification 2)
      PC ──cmd/nfc_job {job_id, mode, mac, seq}──────────────▶ phone opens "hold the tag"
      phone: tap → UID, GET_VERSION, read every page ──evt/nfc_tag──▶ PC
      PC: checks UID / chip / CFGLCK, signs, derives PWD/PACK
      PC ──cmd/nfc_job_reply {write: NDEF pages | none, pwd, pack, lock:false}──▶ phone
      phone (write): write pages 4.. (resume on dropout) → read back → write PWD/PACK
                     → reconnect → PWD_AUTH == PACK?  [lock: commented out]
      phone (verify): PWD_AUTH == PACK? (nothing written)
      phone: read every page again ──evt/nfc_done {pages, pwd_auth, steps, error}──▶ PC
      PC: decode + verify everything → shows all details → sheet → cmd/nfc_verdict ──▶ phone shows it
```

`evt/nfc_progress` reports each step to the PC log as it happens.

## 3. What the PC checks and shows

| Check | Pass when |
|---|---|
| Tag | NTAG213/215/216, 7-byte UID, CFGLCK not set |
| MAC record present | first Text record holds 12 hex |
| MAC matches device | tag MAC == J-Link MAC |
| Signature present | `sytrak:s` record present, 49 bytes |
| Signature valid | version 1, known key ID, ECDSA verifies for this UID + MAC |
| Password validated | PWD_AUTH answered the expected PACK |
| Locked | AUTH0 ≤ CFG0+3 (informational; currently always "No") |
| Written data (V1) | read-back pages == the NDEF bytes sent |

Plus: chip, memory size, CFG0/CFG1 bytes, AUTH0, ACCESS bits, NDEF records, signed string,
key ID, full page dump, every step the phone took.

## 4. Sheet

Each verification block gets four new columns:

```
A SEQ | B MAC | C Date Time | D Device Type
E-L  Verification 1: Verified | Manually Verified | NFC Written | Tag UID | Signature | Password | Locked | Updated At
M-T  Verification 2: Verified | Manually Verified | NFC Read    | Tag UID | Signature | Password | Locked | Updated At
```

Tabs in the 12-column layout (and the older one-header layout) are upgraded automatically.
`NFC Written` / `NFC Read` = Yes only when **every** check passes.

## 5. Robustness

- Every write is judged by reading it back (spec §8). A lost page: reconnect, resume from that page, max 3 tries.
- 20 ms pause after a field reset. A failed PWD_AUTH halts the tag: reconnect before reading again.
- Tag removed / wrong tag mid-job: the UID is checked again before every phase; another UID stops the job.
- Phone ↔ PC: job IDs on every message; the phone resends `evt/nfc_tag` up to 3× (5 s each); the PC answers a resend with the same reply. Stale or foreign messages are ignored and logged.
- PC job timeout (2 min) with a Cancel button; mobile offline/reconnect is reported.
- Missing or invalid key / secret: the NFC buttons explain what to set in Settings.
- PWD/PACK are never logged or stored; the phone wipes them after the job.

## 6. Work items

1. `core/nfc_sticker.py` – format, signing, password, NDEF build/parse, tag analysis (+ tests with the spec's test vector)
2. `core/secrets.py` – load / validate / download key and secret
3. `core/sheets.py` – 20-column layout, migration, extra fields
4. `core/station.py` – new job protocol
5. Verify tab – job state machine, details panel, page dump, scrollable + responsive layout
6. Settings tab – NFC signing secrets (Browse / URL), Google key from URL
7. `tools/mobile_sim.py` – simulated NTAG213 so the full flow runs without a phone
8. Mobile app – NTAG driver, job screen with live steps and the PC's verdict
9. Docs – MOBILE_PROTOCOL.md, README
