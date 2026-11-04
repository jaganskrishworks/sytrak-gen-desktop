# Bundled J-Link Commander (optional)

Copy SEGGER J-Link Commander into this folder to ship it **inside** the exe,
so target PCs need no SEGGER installation:

```
assets/bin/jlink/
  JLink.exe
  JLink_x64.dll        (and/or JLinkARM.dll, depending on version)
  JLinkDevices.xml     (if present in your install)
```

Copy them from e.g. `C:\Program Files\SEGGER\JLink_V924a\`.

Alternatively place a `jlink\` folder **next to** `SyTrakGen.exe` - it is
searched before the system install.

> Check the SEGGER J-Link software license terms before redistributing these
> files to third parties.
