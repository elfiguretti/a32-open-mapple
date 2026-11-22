# a32-open-mapple (`port/`)

Open-source device, SoC, shim, and XNU overlay repository for porting **Open Mapple (XNU `xnu-8796.141.3` + iOS 16.7.16)** + **HakiOS MT6768** drivers to the **Samsung Galaxy A32 4G (`SM-A325M`, MediaTek Helio G80 `MT6769T` / `mt6768`, Mali-G52 MC2, eMMC)**.

> No Apple IPSW files, no vendor firmware binaries, and no personal phone dumps are stored in this repository.

## Silicon-Verified Milestones
- **Hito 1 (Done):** Full partition backup, `live.dtb` + `gpt.bin` + `bringup.py` (62 verified hardware facts).
- **Hito 2 (Done):** Bare-metal ARM64 execution in `EL1` (`fbdiag_a32` v1–v3), verified **S6E3FC3 Super AMOLED DSI CMD** live frame flush (`OVL0 @ 0x1400b000`, `pitch=4352`, `DISP_MUTEX0 @ 0x14001000`, `DSI0 @ 0x14014000`, DCS `0x002c3909`) and `ramoops` (`0x4d09f000`).
- **Hito 3 (In Progress):** Compile XNU `kernel` + `a32.adt` + `shim-a32.Image` + `xnu-a32.bin` via GitHub Actions (`macos-15`, free on public repos) and boot XNU live on the SM-A325M!

## Build via GitHub Actions
Push this `port/` directory to a **public** GitHub repository and run the **Build SM-A325M XNU (Open Mapple + MT6768)** workflow (`workflow_dispatch` or push to `main`).
Download the `a32-xnu-hito3-DEVELOPMENT` artifact and pack it locally with `tools/pack_xnu_boot_a32.py`.
