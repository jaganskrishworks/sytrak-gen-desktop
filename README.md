# SyTrak Gen (sytrak-gen-desktop)

Windows desktop station for provisioning SyTrak devices:

1. **Generate QR**: read the device MAC over a SEGGER J-Link, assign a 4-digit sequence (SEQ),
   and save the row to a Google Sheet. Selected labels can be exported as an A4 PDF label sheet.
2. **Verify QR**: read the device MAC again, look up its SEQ in the sheet, let a paired phone
   (the *SyTrak Mobile* app) scan the sticker, then tell the phone to **write the NFC tag**.

The companion phone app lives in `../sytrak_mobile` (Flutter, Android + iOS).

---

## Contents

1. [How the code starts](#1-how-the-code-starts)
2. [Requirements](#2-requirements)
3. [Install SEGGER J-Link](#3-install-segger-j-link)
4. [Set up the code](#4-set-up-the-code)
5. [Google Sheet access](#5-google-sheet-access)
6. [Run the software](#6-run-the-software)
7. [First-run settings](#7-first-run-settings)
8. [Daily use](#8-daily-use)
9. [Build the standalone exe](#9-build-the-standalone-exe)
10. [Developer tools and tests](#10-developer-tools-and-tests)
11. [Troubleshooting](#11-troubleshooting)
12. [Project layout](#12-project-layout)

---

## 1. How the code starts

```
main.py                              entry point (also the PyInstaller entry)
 └─ sytrak_gen.app.main()            src/sytrak_gen/app.py
     ├─ setup_logging()              %APPDATA%\SyTrakGen\logs\sytrak_gen.log
     ├─ high-DPI + taskbar identity  (Windows)
     ├─ Settings.load()              %APPDATA%\SyTrakGen\settings.json
     └─ MainWindow(settings)         src/sytrak_gen/ui/main_window.py
         ├─ header                   status badges: sheet tab, station, mobile
         ├─ ProbeBar                 J-Link dropdown; polls USB every 2 s, reads MAC (ui/probe_bar.py)
         ├─ GenerateView             Generate QR tab (ui/views/generate_view.py)
         ├─ VerifyView               Verify QR tab   (ui/views/verify_view.py)
         ├─ SettingsView             Settings tab    (ui/views/settings_view.py)
         └─ StationService.start()   built-in MQTT server + LAN discovery for phones (core/station.py)
```

Main building blocks:

| Module | Job |
|---|---|
| `core/jlink/` | finds J-Link probes (`detector.py`), finds `JLink.exe` (`locator.py`), runs J-Link Commander scripts (`commander.py`), reads the MAC (`mac.py`) |
| `core/sheets.py` | Google Sheet: read rows, save with unique SEQ, status columns, duplicate audit |
| `core/qr.py` | QR content (the MAC without colons) and on-screen label images |
| `core/pdf.py` | fixed-layout A4 label sheet (vector PDF) |
| `core/network/` | embedded MQTT 3.1.1 broker and UDP discovery beacon |
| `core/station.py` | station ↔ phone protocol (pairing, verify request, sticker scan, NFC write) |

All network and J-Link work runs on background threads. Results come back to the UI through
`ui/dispatcher.py`, so the window never freezes.

---

## 2. Requirements

| | |
|---|---|
| OS | Windows 10 / 11 (64-bit) |
| Python | 3.12 or newer (developed on 3.14). Only needed to run from source or build the exe. |
| J-Link | any SEGGER J-Link probe + **J-Link Software and Documentation Pack** (section 3) |
| Target | default `nRF52833_xxAA` over SWD (changeable in Settings) |
| Network | internet for Google Sheets; the same Wi-Fi as the phones |
| Google | the service account key (JSON) and access to the spreadsheet (section 5) |

---

## 3. Install SEGGER J-Link

The app detects probes through Windows itself. To **read the MAC** it needs SEGGER's
**J-Link Commander** (`JLink.exe`), which comes with the J-Link Software pack.

### 3.1 Download and install

1. Open **https://www.segger.com/downloads/jlink/**.
2. Under **J-Link Software and Documentation Pack**, download the **Windows 64-bit Installer**
   (e.g. `JLink_Windows_V924a_x86_64.exe`) and accept the licence terms.
3. Run the installer (administrator rights needed):
   * accept the licence,
   * keep **"Install USB Driver for J-Link"** ticked,
   * keep the default destination, e.g. `C:\Program Files\SEGGER\JLink_V924a\`.
4. Finish. If an older version is already installed, both can coexist; the app uses the newest.

### 3.2 Connect the probe and the device

1. Plug the J-Link into USB. Windows lists it as **J-Link** (Device Manager → *Universal Serial Bus devices*).
2. Wire the probe to the target over **SWD**:

   | J-Link | Device |
   |---|---|
   | VTref | VDD (3.3 V) |
   | SWDIO | SWDIO |
   | SWCLK | SWCLK |
   | GND | GND |
   | RESET (optional) | nRESET |

3. **Power the device.** The J-Link senses the target voltage on VTref (about 3.3 V).

### 3.3 Check it works (optional)

Open **J-Link Commander** (Start menu → SEGGER → J-Link Commander), then:

```
J-Link> connect
Device> nRF52833_xxAA
TIF>    S            (SWD)
Speed>  4000
J-Link> mem32 0x100000A4, 2      <- the two words the app turns into the MAC
J-Link> exit
```

If Commander offers to update the probe firmware, you can accept.

### 3.4 How the app finds `JLink.exe`

The app searches in this order. The result is shown in the top bar ("Target … · JLink_V924a")
and in **Settings → J-Link & MAC**.

1. the path set in Settings
2. `assets\bin\jlink\JLink.exe`, bundled into the exe (see `assets/bin/jlink/README.md`)
3. a `jlink\` folder next to `SyTrakGen.exe`
4. `C:\Program Files\SEGGER\JLink*\`, newest version first
5. the system `PATH`

> Bundling SEGGER files (options 2 and 3) lets target PCs work without installing the pack.
> Check SEGGER's licence terms before you redistribute them.

---

## 4. Set up the code

From a command prompt in the project folder:

```bat
cd C:\KRISHWORKS\AssetTracking\qr_gen\sytrak_gen

python -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

* `requirements.txt` holds the runtime packages: ttkbootstrap, qrcode, Pillow, reportlab,
  google-auth, requests.
* `requirements-dev.txt` adds the build and test tools: PyInstaller, pytest, paho-mqtt.
  Install it if you build the exe or run the tests:

  ```bat
  .venv\Scripts\python.exe -m pip install -r requirements-dev.txt
  ```

---

## 5. Google Sheet access

The spreadsheet is the **only record store**; nothing is kept locally. Access is through a
Google **service account**.

1. Place the service account key in **`secrets\`** (any `*.json` file), e.g.
   `sytrak_gen\secrets\sytrak-gen-xxxx.json`.
   * `secrets/` is in `.gitignore`. **Never commit or share this file.**
   * Alternatively pick the key in **Settings → Google Sheet → Key file → Browse**. It is copied to
     `%APPDATA%\SyTrakGen\google_service_account.json`.
2. In Google Sheets, **Share** the spreadsheet with the service account email
   (e.g. `sytrak-gen@sytrak-gen.iam.gserviceaccount.com`) as **Editor**.
3. The **Google Sheets API** must be enabled in the Google Cloud project.
4. **Settings → Google Sheet → Test connection** should show "✔ Connected".

Sheet columns (the header row is written automatically):

```
row 1:                                    | Verification 1 (E-L)                                    | Verification 2 (M-T)
row 2:  SEQ | MAC | Date Time | Device Type
        | Verified | Manually Verified | NFC Written | Tag UID | Signature | Password | Locked | Updated At
        | Verified | Manually Verified | NFC Read    | Tag UID | Signature | Password | Locked | Updated At
```

Data starts at row 3. Each verification fills only its own block. `NFC Written` / `NFC Read` = Yes only
when every NFC check passed; Tag UID, Signature (Valid / Invalid / Missing …), Password (Validated /
Failed) and Locked (Yes / No) hold the last NFC result.

Older tabs are upgraded automatically the first time the app opens them (the 12-column layout gets
the four NFC columns per block; the oldest one-header layout gets the second header row too).
**Update every station before using an upgraded tab**: an old version would write into the wrong columns.
To be safe, open an older tab on **one** station first after updating.

---

## 6. Run the software

```bat
run.bat
```

or

```bat
.venv\Scripts\python.exe main.py
```

On the **first start**, Windows Firewall asks whether to allow the app on the network.
Choose **Allow on Private networks**, otherwise phones cannot connect. The app uses
TCP 1883 (phone connection) and UDP 47474 (station discovery).

Close the window normally (✕) so the phone connection shuts down cleanly.

---

## 7. First-run settings

Open the **Settings** tab, check the values and click **💾 Save settings**.
Settings do not save automatically.

| Section | Setting | Default / notes |
|---|---|---|
| J-Link & MAC | JLink.exe | empty = auto-detect (section 3.4) |
| | Target device | `nRF52833_xxAA` |
| | Interface / Speed | SWD / 4000 kHz |
| | MAC source | *nRF52 FICR DEVICEADDR* (BLE static address). Use *Custom memory address* if the firmware stores its own MAC. |
| Station | Station name / ID | empty = PC name. **The station ID must be unique per PC.** |
| | Broker port / Password | 1883 / empty (optional login `sytrak` + password for phones) |
| Google Sheet | Sheet URL, Key file | section 5 (key: **Browse…** or **From URL…**) |
| | Device types | comma separated, e.g. `UWB-K4W, UWB-K5` |
| | First sequence | used only while a sheet tab is empty |
| NFC signing | Signing key | ECDSA P-192 private key (`.pem`). Empty = `secrets\*.pem` next to the app |
| | Password secret | hex secret (`.secret`) for the per-tag password. Empty = `secrets\*.secret` |
| | Key ID | 1-15, stored in the sticker; the mappers must trust it. **Test signing** checks the files |

Secret files (Google key, signing key, password secret): **Browse…** picks a local file, **From URL…**
downloads it (direct download link). Both check the file first and keep a private copy in
`%APPDATA%\SyTrakGen\secrets\`. Their contents are never logged or sent to the phone. The mock's
`dev_nfc_key.pem` is shown as a **DEV key**: stickers signed with it pass dev mappers only.

Settings and logs are stored in `%APPDATA%\SyTrakGen` (Settings → *Data folder* / *Logs*).

---

## 8. Daily use

### 8.1 Top bar: J-Link

Plug in any J-Link. With one probe it is selected automatically; with several, pick one.
"● Connected S/N …" means it is ready.

### 8.2 Generate QR

1. Choose the **Sheet tab**. Each tab is a group with its own SEQ numbering. Use **+ New tab** to create one.
2. Click **Read MAC Address**. If the device is already registered, its existing QR is shown.
3. Choose the **Device type** (required), then click **Generate** and **Save**.
   The final SEQ is fixed at Save. If another station took the same number a moment earlier,
   the app moves to the next free one automatically.
4. **Saved QR codes** list: use **▲ Hide generator** for a full-height list, and tick labels
   (or **Select all**).
5. **⬇ Generate PDF**:
   1. The **whole tab is checked first**: duplicate SEQs, duplicate MACs and invalid rows block
      the PDF, and the message lists the rows to fix.
   2. The A4 sheet uses a fixed layout: 7 × 10 labels, edge to edge, margins left 25 / top 31.
   3. Print at **Actual size (100 %)**.

### 8.3 Verify QR

1. **Mobile connection**: on the phone open SyTrak Mobile and pick this station. On the PC select
   the phone and click **Connect**.
2. Check the **Sheet tab** (shared with Generate QR).
3. **① Read MAC from device**. The app reads the MAC, finds the **SEQ** in the tab, checks it for
   duplicates, and sends SEQ + MAC to the phone.
4. On the phone, **Scan sticker**, check "✔ Sticker matches", and tap **Confirm & send to PC**.
5. When device, sheet and sticker all match you see **VERIFIED** and the sheet gets `Verified = Yes`.
   If a check fails, **Verified Manually** lets you override it, and the sheet gets `Manually Verified = Yes`.
6. **NFC (signed sticker)**: choose the mode with the switch at the **top right of the Verification box**
   (remembered). The NFC button below does what the mode says, and the phone opens its NFC screen by itself:
   * **Verification 1 · Write signed sticker** → **① Write signed NFC sticker**. Hold the tag to the phone.
     The PC signs the sticker for this tag's UID + the device MAC, the phone writes it, reads it back,
     sets the tag password and checks it, then **locks** the tag (AUTH0 = 02): writing needs the tag's
     password from then on, reading stays open for the mappers. A locked tag can be written again with
     Verification 1 (the phone authenticates first). **Settings › Lock tags** switches locking off for test tags.
   * **Verification 2 · Read only** → **② Verify NFC sticker (read only)**. The tag is read and its
     password checked; nothing is written.
   * The PC then checks everything: tag type and UID, MAC record = device, signature present and valid
     for this UID + MAC, written data, password, lock state. All of it shows in the **NFC sticker**
     panel (Checks & details · Tag memory (all pages) · Phone steps) and on the phone.
   * Sheet: `NFC Written` / `NFC Read` = Yes only when every check passed, plus Tag UID, Signature,
     Password and Locked. `Verified` and `Manually Verified` go into the block of the selected mode.
   * **One tag per device.** The tag UID that passes Verification 1 is registered to that MAC (sheet:
     Verification 1 › Tag UID). Verification 1 refuses, before writing, a tag that belongs to another MAC,
     or a second tag for a MAC that already has one (clear its Tag UID in the sheet to give it a new tag).
     Verification 2 fails ("Tag UID ↔ MAC") on any tag other than the registered one, even if its
     signature is valid, and when no tag was registered yet. The sheet is read fresh at every tap.
   * A tag that cannot be used (random UID, not NTAG21x, frozen configuration, lock bits, not NDEF
     formatted) is refused with the reason: tap another one. **Cancel NFC job** stops a job; a job
     with no answer from the phone times out after 2 minutes.
   * NFC is always started from the PC. The phone has no NFC button of its own. Android phones only.
   * The signing key and password secret are set in **Settings › NFC signing** (section 7).

### 8.4 Several stations

Each PC is its own station. Phones pick their station from the list, and each station pairs
with one phone. SEQs stay unique per sheet tab even when several stations save at the same
moment.

### 8.5 MQTT tab (developer option)

The MQTT tab is hidden. To show it, click the **SyTrak Gen** title in the top-left corner
**10 times in a row** (no more than 1.5 s between clicks). From the 5th click the status bar
counts down. The tab then stays visible until the app is closed, and is hidden again at the
next start. It records in the background from the start, so the messages from before you
enabled it are already there.

The **MQTT** tab shows every message between this PC and the phones as a topic tree, like
MQTT Explorer:

* **Topics:** expand or collapse each level (`sytrak › <station> › mobile › <phone> › cmd / evt / status`).
  Each topic shows its latest value, message count and time, and flashes yellow when a message arrives.
  Colours show the direction: blue = PC → phone, green = phone → PC, orange = last will (phone lost).
* **Message:** click a topic to see its direction, client (and whether it is the paired phone),
  QoS, retained flag, time, size and the payload as formatted JSON.
* **History:** the last 100 messages of that topic. Click one to view it; *Show latest* goes back.
* **Clients:** each connected phone with its address, online or offline, paired, connected time,
  messages sent and subscriptions.
* **Toolbar:** *Filter* (topic or value text), *Pause / Resume*, *Expand / Collapse all*,
  *Clear*, and *Save…* (all messages as a JSON file).

Tag passwords (`pwd` / `pack` in `nfc_job_reply`) are replaced by `•••• (hidden)` before they
are stored, so they never show on screen or in a saved file.

---

## 9. Build the standalone exe

```bat
build.bat
```

The script:

1. creates `.venv` if needed and installs `requirements-dev.txt`,
2. runs the test suite,
3. builds **`dist\SyTrakGen.exe`** (one file, no console) with PyInstaller (`sytrak_gen.spec`).

Target PCs need **no Python**. Give them:

* `SyTrakGen.exe`
* `secrets\<key>.json` next to the exe (or pick the key in Settings on that PC)
* SEGGER J-Link Software installed (section 3), unless you bundled `JLink.exe`

### 9.1 Linux .deb package

Build **on Linux** (PyInstaller cannot build a Linux program from Windows). Use the oldest
Ubuntu you install on, because a build from a newer Ubuntu may not start on an older one.

```bash
sudo apt install python3-venv python3-tk dpkg-dev
chmod +x build_deb.sh
./build_deb.sh                                   # -> dist/sytrak-gen_0.1.0_amd64.deb
```

On each operator PC:

```bash
sudo apt install ./sytrak-gen_0.1.0_amd64.deb    # menu entry "SyTrak Gen", command: sytrak-gen
sudo cp sytrak-gen-xxxx.json /opt/sytrak-gen/secrets/   # or pick the key in Settings
sudo apt remove sytrak-gen                       # uninstall
```

The SEGGER J-Link Software for Linux (`JLink_Linux_*_x86_64.deb`) is installed separately.
The version number comes from `src/sytrak_gen/__init__.py`.

### 9.2 Android app (SyTrak Mobile)

```bat
cd C:\KRISHWORKS\AssetTracking\qr_gen\sytrak_mobile
flutter build apk --release
```

Output: `build\app\outputs\flutter-apk\app-release.apk`. Copy it to the phone and install it
(allow "Install unknown apps"). The version is `version:` in `pubspec.yaml`; raise it for each release.
The build is signed with the debug key, which is fine for in-house phones. Play Store needs a release key.

---

## 10. Developer tools and tests

```bat
.venv\Scripts\python.exe -m pytest -q tests                 :: unit + broker tests
.venv\Scripts\python.exe tools\mobile_sim.py --auto-sticker :: fake phone (needs paho-mqtt)
.venv\Scripts\python.exe tools\mock_pdf.py --open           :: label PDF from random MACs, no sheet needed
.venv\Scripts\python.exe tools\make_icon.py                 :: rebuild app icons from company-logo.png
```

* `mobile_sim.py` behaves like the phone; NFC jobs run on a virtual NTAG213. Useful options:
  `--host 127.0.0.1 --station <ID>`, `--auto-sticker`, `--sticker <text>` (wrong sticker),
  `--swap-tag`, `--tag-dropout 9`, `--wrong-password`, `--random-uid`, `--cfglck`.
* The signed-sticker design and checks: **`docs/NFC_STICKER_PLAN.md`**.
* The phone protocol is documented in **`docs/MOBILE_PROTOCOL.md`**.
* PDF label sizes are constants at the top of `src/sytrak_gen/core/pdf.py`.

---

## 11. Troubleshooting

| Symptom | Fix |
|---|---|
| Top bar: **JLink.exe NOT FOUND** | Install the SEGGER pack (section 3) or set the path in Settings. |
| **No J-Link detected** | Re-plug the probe; check Device Manager; reinstall the pack with the USB driver ticked. |
| MAC read fails: "Cannot connect to target" | Is the device powered (VTref about 3.3 V)? Check SWD wiring, the target device name, and try speed 1000. |
| **Station port 1883 in use** | Another program (e.g. Mosquitto) or a second copy of the app is using it. Close it or change the port in Settings. |
| Phone does not see the station | Same Wi-Fi? Firewall allowed? Use **Add PC by IP** on the phone (always needed on iPhone). |
| Google Sheet **403** | Share the sheet with the service account email as Editor. |
| Google Sheet **429** / slow saves | Google's per-minute read limit (shared by all stations). The app retries automatically; raise the quota in Google Cloud if it happens often. |
| "Tab '…' not found" | The tab was renamed or deleted. Pick a tab again (⟳ reloads the list). |
| Generate PDF blocked | The message lists the rows with duplicate SEQ / MAC or invalid entries. Fix them in the sheet and try again. |
| Where are the logs? | `%APPDATA%\SyTrakGen\logs\sytrak_gen.log` (Settings → Logs) |

---

## 12. Project layout

```
main.py                     entry point (also PyInstaller entry)
run.bat / build.bat         run from source / build the exe
build_deb.sh                build the Linux .deb
requirements.txt            runtime packages (bundled into the exe)
requirements-dev.txt        build + test tools
sytrak_gen.spec             PyInstaller build (one file, windowed)
assets/
  icons/                    app.ico, app.png, company-logo.png (+ .svg)
  fonts/                    IBMPlexSans-Medium.ttf (PDF labels)
  bin/jlink/                optional bundled J-Link Commander
secrets/                    Google service account key (git-ignored)
docs/MOBILE_PROTOCOL.md     station <-> phone protocol
tools/                      mobile_sim.py, mock_pdf.py, make_icon.py
src/sytrak_gen/
  app.py                    startup (logging, DPI, settings, main window)
  config.py, paths.py, logger.py
  core/jlink/               probe detection, JLink.exe lookup, Commander runner, MAC read
  core/network/             embedded MQTT broker + discovery beacon
  core/station.py           phone protocol
  core/sheets.py            Google Sheet store
  core/qr.py, core/pdf.py   QR content/images, A4 label PDF
  ui/                       main window, J-Link bar, Generate / Verify / Settings tabs
tests/                      pytest suite
```
