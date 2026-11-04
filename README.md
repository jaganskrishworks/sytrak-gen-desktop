# SyTrak Gen

Windows desktop station for SyTrak device provisioning:

1. **Generate QR**: read the device MAC over J-Link, then generate a QR code with a 4-digit sequence, then save it to the Google Sheet.
2. **Verify QR**: a paired phone scans the QR. The station checks it against the J-Link MAC and the stored
   record, then tells the phone to **write the NFC tag**.

Ships as a single `SyTrakGen.exe`. Target PCs need no Python and no extra installs (see J-Link below).

## Project layout

```
main.py                     entry point (also PyInstaller entry)
sytrak_gen.spec             PyInstaller build (one-file, windowed)
build.bat / run.bat         build the exe / run from source
requirements.txt            runtime deps (bundled into the exe)
requirements-dev.txt        build + test deps
assets/
  icons/app.ico, app.png    app icon (regenerate: python tools/make_icon.py)
  bin/jlink/                optional bundled J-Link Commander (see README inside)
docs/MOBILE_PROTOCOL.md     protocol for the mobile app developer
tools/mobile_sim.py         phone simulator for testing
src/sytrak_gen/
  app.py, config.py, paths.py, logger.py
  core/jlink/               probe detection, Commander runner, MAC reading
  core/network/             embedded MQTT broker + LAN discovery beacon
  core/station.py           station <-> mobile protocol (pairing, scan, NFC)
  core/qr.py                QR payload + rendering
  core/sheets.py            Google Sheet records (read list, save row)
  ui/                       main window, J-Link bar, Generate / Verify / Settings tabs
tests/
```

## Run from source

```bat
run.bat
```

## Build the exe

```bat
build.bat          → dist\SyTrakGen.exe
```

## Notes for deployment

* **J-Link:** probes are detected with built-in Windows APIs. Reading the MAC needs SEGGER
  `JLink.exe`. The app uses, in order: the copy bundled in `assets/bin/jlink/` (inside the exe),
  a `jlink\` folder next to the exe, or an installed SEGGER J-Link. Any J-Link probe works.
  With several plugged in, pick one in the top bar.
* **MAC source:** the default is the nRF52 FICR `DEVICEADDR` (BLE static address), with target
  `nRF52833_xxAA`. If the firmware stores its own MAC, choose *Custom memory address* in Settings.
* **Firewall:** the first start shows a Windows Firewall prompt for the mobile server
  (TCP 1883, UDP 47474). Allow it on *Private networks* or phones cannot connect.
* **Multiple stations:** each PC is its own station (unique station ID derived from the PC name).
  Phones pick their station from the discovery list. Sequence numbers are **per station**.
* **Google Sheet is the only record store.** Columns:
  `SEQ | MAC | Date Time | Device Type` are written by Save, and
  `Verified | Manually Verified | NFC Written | Updated At` (`Yes` / empty) are written by Verify QR.
  NFC Written is set only after the phone reads the tag back and it matches. Save writes
  directly to it. If Google can't be reached the row is **not saved** and you click Save again.
  There is no local copy. The next sequence is the highest SEQ in the sheet + 1, shared by all stations.
* **Service account key:** `secrets\*.json` next to the app/exe, or pick it in Settings → Google Sheet
  (it is copied to `%APPDATA%\SyTrakGen\google_service_account.json`). Share the sheet with the
  service account email as **Editor**.
* User data lives in `%APPDATA%\SyTrakGen` (settings, logs).
