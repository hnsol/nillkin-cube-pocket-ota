# Nillkin Cube Pocket OTA — firmware-level key remapping for the Nillkin Cube Pocket keyboard, from macOS

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.14](https://img.shields.io/badge/python-3.14-blue.svg)
![Platform: macOS 12.3+](https://img.shields.io/badge/platform-macOS%2012.3%2B-lightgrey.svg)
![Status: experimental](https://img.shields.io/badge/status-experimental-orange.svg)

**日本語のドキュメント（メイン）は [README.md](README.md) にあります。**

---

Nillkin Cube Pocket OTA is an experimental Python CLI that patches the keymap inside the Nillkin Cube Pocket foldable Bluetooth keyboard's firmware (model NKF01, PixArt `PAR2801`) and flashes it over BLE from macOS. You describe the remap in a 6-line TOML file; the tool changes exactly those bytes in the vendor firmware image, verifies the result, and performs the OTA update with CoreBluetooth. Unlike host-side remappers such as Karabiner-Elements, which only work on the Mac they are installed on, the remap lives in the keyboard, so it follows the keyboard to every paired iPhone, iPad, Android device and PC — and unlike the vendor's Windows-only `OTAUtility.exe`, it runs on macOS and can change the keymap.

> **Firmware flashing can brick the keyboard.** This tool was verified on **one** unit (NKF01, GATT model `PAR2801`, revision `1.0.0`). If an update fails badly enough that the keyboard stops BLE advertising, this tool cannot recover it. Vendor firmware is **not** included; you must obtain it yourself.

<p align="center">
  <img src="docs/images/keymap-before-after.svg" width="900"
       alt="Nillkin Cube Pocket key layout before and after remapping: Caps Lock becomes Control, left Control becomes Option, left Option becomes Command, left Command becomes Eisu (LANG2), right Command becomes Kana (LANG1), right Option becomes Command">
</p>

The bundled example, [`configs/jp-lang.toml`](configs/jp-lang.toml), turns the US layout into a Mac-JIS-style bottom row:

| Physical key | Factory output | Output after `jp-lang.toml` | HID usage |
|---|---|---|---|
| `caps lock` | Caps Lock | **Control** | `0x39` → `0xE0` |
| left `control` | Control | **Option** | `0xE0` → `0xE2` |
| left `option` | Option | **Command** | `0xE2` → `0xE3` |
| left `command` | Command | **英数 / Eisu (LANG2)** | `0xE3` → `0x91` |
| right `command` | Command | **かな / Kana (LANG1)** | `0xE7` → `0x90` |
| right `option` | Option | **Command** | `0xE6` → `0xE7` |

## The Key Remapping Problem This Solves

The Nillkin Cube Pocket is a good pocket keyboard: it folds to phone size, has a touchpad, and pairs with three devices. It ships only in US, German, Spanish and Arabic layouts, and the vendor provides no remapping tool.

- **No 英数 / かな keys for Japanese input.** On a Mac JIS keyboard the two keys flanking the space bar switch between Latin and Japanese input with one thumb press and no toggle state. On the Cube Pocket's US layout you fall back to `Ctrl`+`Space` or Caps Lock toggling, which requires knowing which mode you are currently in.
- **Host-side remapping does not travel.** Karabiner-Elements fixes this on one Mac. The Cube Pocket is built to be carried between a phone, a tablet and a laptop, and iOS, iPadOS and Android cannot run Karabiner. iPadOS's built-in modifier remapping can swap Caps Lock, Control, Option, Command and Globe among themselves, but cannot turn a key into 英数 or かな.
- **Control sits in the bottom-left corner.** On a keyboard this small, reaching the corner `control` key for Emacs-style shortcuts is awkward; the much larger `caps lock` key on the home row is unused by many people.
- **The only official updater is a Windows executable.** The vendor firmware package contains `OTAUtility.exe`, which flashes unmodified vendor firmware from Windows. There is no macOS or Linux path.

This project removes each friction in the same order: the left and right `command` keys emit HID `LANG2` / `LANG1`, which macOS treats as 英数 / かな; the remap is stored in the keyboard's firmware, so it applies to all three Bluetooth slots without any software on the host; `caps lock` emits Control; and the whole flow — patch, verify, flash, recover — runs from a macOS terminal.

The trade is risk and effort. You flash modified firmware onto a device with no documented recovery mode, the process takes two separate OTA updates (factory → vendor GLOBAL → your remap) and every later layout change is another firmware flash with the same risk.

## Quick Start

```sh
git clone https://github.com/hnsol/nillkin-cube-pocket-ota.git
cd nillkin-cube-pocket-ota
python3.14 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
```

Build a patched firmware from the vendor GLOBAL image (`B077T_US_13.bin`, not included) and print the BLE transfer plan without connecting to anything:

```sh
python3 -m tools.phase3_build_patch downloaded/B077T_US_13.bin --config configs/jp-lang.toml --output-root .
python3 -m tools.macos_ota --firmware firmware/patched/B077T_US_13_JP_LANG.bin --show-transfer-plan
```

Nothing above writes to the keyboard. Actual flashing requires `--execute` plus the image's full SHA-256; see [Flashing the Firmware over BLE](#flashing-the-firmware-over-ble).

## How the Firmware Key Remapping Works

The vendor GLOBAL firmware `B077T_US_13.bin` is a 123,916-byte image whose keymap stores one HID Usage ID per physical key. `tools.phase3_build_patch` does the following:

1. Validates the input against the known GLOBAL image (size, SHA-256 `00c87d25…6957f`, embedded version string).
2. Parses the TOML config strictly: unknown keys, unknown HID usages, duplicate entries and extra tables are all errors.
3. Replaces one byte per remapped key at a fixed, verified offset (for example `0x1DABE`: `0x39` Caps Lock → `0xE0` Left Control), after checking that the old byte is what it expects.
4. Writes the untouched original to `firmware/original/` and the patched copy to `firmware/patched/`, never overwriting an existing file, and prints the byte diff, the 16-bit sum and the SHA-256.

The bundled JP_LANG image differs from GLOBAL by exactly 6 bytes. The firmware's OTA checksum moves from `0xEC27` to `0xEC29`, which is how you confirm after reboot which image is running.

`tools.macos_ota` then speaks the PixArt OTA protocol recovered from the vendor's Windows utility: `0x27` init, `0x25` object-create for each 4,096-byte object (31 objects for this image), raw payload as write-without-response, `0x17` checksum ACKs, `0x18` upgrade and `0x22` reset. On macOS the 244-byte logical blocks are split into 44-byte physical writes with 10 ms pacing, because the negotiated ATT MTU on macOS was 50. The protocol evidence is documented in [docs/protocol.md](docs/protocol.md).

## Keymap Configuration Reference

```toml
format_version = 1

[remap]
caps_lock = "left_control"
left_control = "left_alt"
left_alt = "left_gui"
left_gui = "lang2"
right_gui = "lang1"
right_alt = "right_gui"
```

The left side is the **physical key**; the right side is the **HID usage it should emit**. Keys you do not list are left unchanged.

| | Allowed names |
|---|---|
| Physical keys (6) | `caps_lock`, `left_control`, `left_alt`, `left_gui`, `right_gui`, `right_alt` |
| Output usages (12) | `caps_lock`, `space`, `left_control`, `left_shift`, `left_alt`, `left_gui`, `right_control`, `right_shift`, `right_alt`, `right_gui`, `lang1`, `lang2` |

`alt` is the key labelled `option` and `gui` is the key labelled `command`. Only these six physical keys are remappable because they are the only ones whose firmware offsets have been verified.

To build your own layout, copy the example and pick an output name. The output file name is derived from the config name, or set it with `--patched-name`:

```sh
cp configs/jp-lang.toml configs/my-layout.toml
python3 -m tools.phase3_build_patch downloaded/B077T_US_13.bin \
  --config configs/my-layout.toml --output-root . --patched-name B077T_US_13_MY_LAYOUT.bin
```

## Flashing the Firmware over BLE

Flashing is a two-step process the first time, because the factory firmware and the vendor GLOBAL firmware identify themselves differently. The CLI never chains the steps automatically.

**Step 0 — read-only preflight.** Put the keyboard in pairing mode and run without `--execute`. This scans, reads GATT info, and sends only `0x10` (OTA init) and `0x23` (firmware info). No firmware is sent.

```sh
python3 -m tools.macos_ota --firmware firmware/original/B077T_US_13.bin
```

| Running firmware | OTA version | OTA checksum |
|---|---|---|
| Factory | `1.0` | `0x6162` |
| Vendor GLOBAL | `1.0.1` | `0xEC27` |
| JP_LANG (bundled example) | `1.0.1` | `0xEC29` |

**Step 1 — factory → vendor GLOBAL.** Allowed only when the advertised name, GATT model `PAR2801`, revision `1.0.0`, the `ff00`–`ff03` GATT layout and the raw `0x23` response all match the known factory signature exactly.

```sh
python3 -m tools.macos_ota \
  --firmware firmware/original/B077T_US_13.bin \
  --execute --accept-factory-signature \
  --confirm-sha256 00c87d252b639165963cc4452600672305043696d5fec7837b34b3dbed66957f
```

**Step 2 — vendor GLOBAL → your remap.** Power-cycle, re-run the preflight and confirm `1.0.1 / 0xEC27`, then flash the patched image. For the bundled JP_LANG image:

```sh
python3 -m tools.macos_ota \
  --firmware firmware/patched/B077T_US_13_JP_LANG.bin \
  --execute --accept-installed-global-signature \
  --confirm-sha256 3c096e6498332d677cb0e4a0c541e6630f8955b0a21e97388b776674883f5246
```

For an image built from your own TOML, pass the original and the config as well. The CLI regenerates the image in memory and only proceeds if it is byte-identical to the file you are flashing:

```sh
python3 -m tools.macos_ota \
  --firmware firmware/patched/B077T_US_13_MY_LAYOUT.bin \
  --base-firmware firmware/original/B077T_US_13.bin \
  --remap-config configs/my-layout.toml \
  --execute --accept-installed-global-signature \
  --confirm-sha256 <SHA-256 printed by phase3_build_patch>
```

**If the final `0x18` ACK times out, do not re-send.** On the verified unit the JP_LANG update completed on the device even though the host never received the final ACK. Power-cycle the keyboard and run the read-only preflight; `0xEC29` means the update succeeded. Details and other failure cases are in [docs/maintenance.md](docs/maintenance.md).

**Step 3 (later) — change the layout again, or restore the stock layout.** Once a remapped image is running, the keyboard reports that image's checksum instead of `0xEC27`, so step 2's gate no longer matches. Use `--accept-installed-remap-signature` instead. It requires the same exact identity match, with the raw `0x23` response equal to `1.0.1` plus the checksum of the remap you declare as installed: the bundled JP_LANG (`0xEC29`) by default, or the sum computed from `--installed-remap-config <toml>` if you flashed your own layout. Allowed targets are the fixed GLOBAL image (stock layout), the fixed JP_LANG image, or an image regenerated from GLOBAL plus TOML.

```sh
# JP_LANG is running → back to the stock GLOBAL layout
python3 -m tools.macos_ota \
  --firmware firmware/original/B077T_US_13.bin \
  --execute --accept-installed-remap-signature \
  --confirm-sha256 00c87d252b639165963cc4452600672305043696d5fec7837b34b3dbed66957f

# my-layout is running → a different custom layout
python3 -m tools.macos_ota \
  --firmware firmware/patched/B077T_US_13_OTHER.bin \
  --base-firmware firmware/original/B077T_US_13.bin \
  --remap-config configs/other.toml \
  --execute --accept-installed-remap-signature \
  --installed-remap-config configs/my-layout.toml \
  --confirm-sha256 <SHA-256 printed by phase3_build_patch>
```

This path is covered by unit tests but, as of 2026-09-19, has **not yet been exercised on real hardware**; steps 1 and 2 have.

### Recovering an interrupted first update

`tools.macos_recover` resumes an interrupted **factory → GLOBAL** transfer from the device's persisted checkpoint. It accepts only the fixed GLOBAL image. It is not a general downgrade tool.

```sh
# Inspect the checkpoint only (sends 0x27, writes nothing)
python3 -m tools.macos_recover --firmware firmware/original/B077T_US_13.bin --inspect-state

# Resume, only if the device's offset/checksum match the same prefix of GLOBAL
python3 -m tools.macos_recover \
  --firmware firmware/original/B077T_US_13.bin \
  --execute --accept-factory-signature \
  --confirm-sha256 00c87d252b639165963cc4452600672305043696d5fec7837b34b3dbed66957f
```

## Command Reference

| Command | Purpose | Writes to keyboard? |
|---|---|---|
| `python3 -m tools.phase3_build_patch <global.bin> --config <toml>` | Validate the vendor image and build a patched copy | No (files only) |
| `python3 -m tools.macos_ota --firmware <bin>` | Read-only preflight: scan, GATT info, `0x10`, `0x23` | No |
| `python3 -m tools.macos_ota --firmware <bin> --show-transfer-plan` | Print the OTA wire operations without importing or using BLE | No |
| `python3 -m tools.macos_ota --firmware <bin> --execute --confirm-sha256 <hash> …` | Flash after all gates pass on the same connection | **Yes** |
| `python3 -m tools.macos_recover --firmware <global.bin> --inspect-state` | Show the persisted OTA checkpoint | No |
| `python3 -m tools.macos_recover --firmware <global.bin> --execute …` | Resume an interrupted factory → GLOBAL transfer | **Yes** |
| `python3 -m tools.phase1_ble_info` | Dump BLE advertisement and GATT information | No |
| `python3 -m tools.phase2_analyze_fw <global.bin> <korean.bin>` | Compare two vendor images during keymap analysis | No |

Shared BLE options and their defaults: `--scan-timeout 15.0`, `--connect-timeout 10.0`, `--operation-timeout 5.0` (seconds). `--device-uuid` narrows the scan only; CoreBluetooth UUIDs are per-Mac and are never used as proof of identity.

## Firmware Flashing Safety Gates

- **Read-only by default.** Without `--execute`, no command sends firmware data.
- **Image allowlist.** Only the fixed GLOBAL image, the fixed JP_LANG image, or an image that can be regenerated byte-for-byte from GLOBAL plus your TOML is accepted.
- **Explicit hash confirmation.** `--execute` requires the full 64-character SHA-256 of the target file.
- **Three exact-match signatures.** Factory (`1.0 / 0x6162`) may only receive GLOBAL; running GLOBAL (`1.0.1 / 0xEC27`) may receive a remap; a running remap (`1.0.1` plus its declared checksum) may receive GLOBAL or another remap. The flags are mutually exclusive.
- **Same-connection identity check.** Advertised name, GATT model, revision, service layout and the raw `0x23` response are re-read on the connection that will be used for writing, and must match a known signature exactly. The checksum alone is never used for identification.
- **Fail closed.** A missing or mismatched ACK, a disconnect, or a send-queue timeout stops the transfer without sending `0x18` upgrade or `0x22` reset.
- **241 unit tests** cover the parser, patch builder, protocol planner, GATT engine and both CLIs against fakes (`pip install pytest && python3 -m pytest`).

## Nillkin Cube Pocket OTA vs Karabiner-Elements vs iPadOS Modifier Settings vs Vendor OTAUtility

| | This project | Karabiner-Elements | iPadOS modifier key settings | Vendor `OTAUtility.exe` |
|---|---|---|---|---|
| Where the remap lives | Keyboard firmware | One Mac | One iPad | — (no remapping) |
| Works on every paired device | Yes | No | No | — |
| Can emit 英数 / かな (LANG2 / LANG1) | Yes | Yes | No | — |
| Change the layout again later | Yes, by re-flashing | Any time | Any time | — |
| Remappable keys | 6 verified keys | Any key, plus complex rules | 5 modifiers | — |
| Runs on | macOS 12.3+ | macOS | iPadOS | Windows |
| Risk | Can brick the keyboard | None to hardware | None | Vendor-supported flashing |
| Price | Free, MIT | Free | Built in | Free |

**Choose this project when** you carry the Cube Pocket between several devices, at least one of which cannot run a remapper, and you accept the risk of flashing modified firmware.
**Choose Karabiner-Elements when** you only use the keyboard with one Mac, or you need more than the six keys this tool can change. It is the safer answer in that case.
**Choose iPadOS modifier settings when** swapping Caps Lock and Control on a single iPad is all you need.
**Choose the vendor `OTAUtility.exe` when** you only want official firmware and have a Windows PC.

## Who Is This For?

- **Japanese-input users of the Cube Pocket** who want thumb-operated 英数 / かな keys on a US-layout pocket keyboard across Mac, iPhone and iPad.
- **Emacs/terminal users** who want Control on the `caps lock` position on every device without per-device settings.
- **People reverse-engineering PixArt-based BLE keyboards** who want a worked, tested macOS implementation of the PixArt OTA "new flow" (opcodes `0x27`/`0x25`/`0x17`/`0x18`/`0x22`) with documented evidence.
- **Not for** anyone who needs the keyboard to keep working tomorrow and has no tolerance for a bricked device.

## Requirements

- macOS 12.3 or later (BLE scanning does not filter by guessed service UUID, which needs 12.3+).
- Python 3.14 (developed and tested on 3.14.4; `tomllib` makes 3.11 the hard floor, other versions are untested).
- One runtime dependency: `bleak>=3.0.2,<4`.
- A Nillkin Cube Pocket (NKF01) reporting GATT model `PAR2801`, revision `1.0.0`.
- The vendor GLOBAL firmware `B077T_US_13.bin` (SHA-256 `00c87d252b639165963cc4452600672305043696d5fec7837b34b3dbed66957f`), which you download yourself from the Korean retailer Funkeys' [Windows firmware package](https://funkeys.co.kr/bbs/board.php?bo_table=download&wr_id=425).
- A well-charged keyboard.

## Frequently Asked Questions

### Can I remap keys on the Nillkin Cube Pocket keyboard?

Yes, six of them, by patching the firmware. This tool can change what `caps lock`, left `control`, left `option`, left `command`, right `command` and right `option` emit. Nillkin provides no remapping software, so the alternatives are host-side tools such as Karabiner-Elements.

### How do I get 英数 and かな keys on a US-layout Bluetooth keyboard?

Make the keys next to the space bar emit HID `LANG2` (`0x91`, 英数) and `LANG1` (`0x90`, かな). macOS treats these the same as the keys on an Apple JIS keyboard. This project does that inside the Cube Pocket's firmware; on other keyboards, Karabiner-Elements can do it on the Mac side.

### Does the remap work on iPhone, iPad, Android and Windows?

The remap is applied inside the keyboard, so every paired host receives the remapped HID usages. Behaviour was verified on macOS only, where all six keys were confirmed. How a given OS interprets `LANG1`/`LANG2` is up to that OS and has not been tested here.

### Is Nillkin Cube Pocket OTA a replacement for Karabiner-Elements?

No. Karabiner-Elements remaps any key with complex rules and carries no hardware risk. This tool changes six keys on one keyboard model. Its only advantage is that the result is host-independent.

### Can this brick my keyboard?

Yes. Every safety gate reduces the chance of flashing the wrong image or continuing after an error, but none of them can help if the device stops BLE advertising. The author's unit was updated successfully twice (factory → GLOBAL → JP_LANG) and an interrupted first transfer was recovered, which is a sample size of one.

### Where do I get the firmware file?

From the Korean keyboard retailer Funkeys (펀키스): [Nillkin Cube Pocket - 한영 전환 입력키 위치 변경 펌웨어(Windows)](https://funkeys.co.kr/bbs/board.php?bo_table=download&wr_id=425). The 2.7 MB zip contains the Windows `OTAUtility.exe`, a `[GLOBAL USER] … B077T_US_13.bin` image (the one this tool uses; rename it to `B077T_US_13.bin`) and a `[KR USER]` image that this tool does not use. It was the only public source the author found; availability is outside this project's control. This repository does not redistribute vendor firmware or tools; `/vendor/` and `/firmware/` are git-ignored. The patch builder refuses any input whose SHA-256 is not the known GLOBAL hash.

### Can I change the layout again or go back after flashing a remap?

Yes, with `--accept-installed-remap-signature`. The tool checks that the running firmware reports `1.0.1` plus the checksum of the remap you say is installed (JP_LANG `0xEC29` by default, or computed from `--installed-remap-config`), then allows flashing the stock GLOBAL image, JP_LANG, or another TOML-generated image. This path is unit-tested but had not been run on real hardware as of 2026-09-19. Returning to the factory `1.0` firmware is not possible because that image is not available; vendor GLOBAL `1.0.1` is the stock layout you can return to.

### How long does the OTA update take?

It has not been benchmarked. The image is 123,916 bytes sent as 3,056 writes of at most 44 bytes with 10 ms pacing, so pacing alone is about 31 seconds, plus 31 object ACK waits and up to 30 seconds for the final `0x18` ACK.

### Does it work on Linux or Windows?

Not as shipped. The write path depends on CoreBluetooth behaviour (`canSendWriteWithoutResponse`, 10 ms pacing). The protocol layer in `tools/pixart_ota.py` is transport-neutral, and an ATT MTU of 23 was measured on a Steam Deck, but no full OTA was performed from Linux.

### Does it work with other Nillkin or PixArt keyboards?

Unknown, and the tool will refuse to try. The identity gates require the exact advertised name, GATT model, revision and `0x23` response of the verified unit, and the keymap offsets are specific to `B077T_US_13.bin`.

## Limitations

- **One verified device.** Other units, hardware revisions and vendor firmware versions are untested.
- **Six remappable keys.** Only keys with verified firmware offsets are exposed; letters, arrows and `fn` are not.
- **Twelve output usages.** Modifiers, Space, Caps Lock, `LANG1` and `LANG2` only.
- **macOS only** for flashing.
- **No full-brick recovery.** If the keyboard stops advertising over BLE, this tool cannot reach it.
- **Re-flashing over a remap is not hardware-verified yet.** `--accept-installed-remap-signature` is unit-tested only. Keep the TOML you flashed: the tool needs it to recognize the running firmware.
- **No return to factory `1.0`.** `tools.macos_recover` only resumes an interrupted factory → GLOBAL transfer.
- **The final ACK may not arrive.** A `0x18` timeout is not proof of failure; verify with the read-only preflight after a power cycle.
- **No vendor files.** Firmware and the Windows utility are not redistributed.

## Fork It and Make Your Own

Contributions are not being solicited: with one verified device, the author cannot test changes for hardware they do not own. The code is 3,457 lines in 11 modules under `tools/`, small enough for a coding agent to read in one pass. Useful starting points, each in one file:

- **More output usages** — add names to `HID_USAGES` in [`tools/keymap_config.py`](tools/keymap_config.py).
- **More physical keys** — add a verified `BytePatch(offset, old, new, label)` to `_PHYSICAL_KEY_PATCHES` in [`tools/phase3_build_patch.py`](tools/phase3_build_patch.py) and the name to `PHYSICAL_KEYS` in `keymap_config.py`.
- **A different firmware image** — approved sizes, hashes and checksums live in `APPROVED_IMAGES` in [`tools/firmware_image.py`](tools/firmware_image.py).
- **A different device signature** — the factory and installed-GLOBAL signature checks are in [`tools/macos_ota.py`](tools/macos_ota.py).
- **Another host OS** — pacing and send-queue handling are isolated in [`tools/ble_transport.py`](tools/ble_transport.py) and [`tools/gatt_ota.py`](tools/gatt_ota.py); [`tools/pixart_ota.py`](tools/pixart_ota.py) is transport-neutral.

Point your coding agent at the repository together with [docs/protocol.md](docs/protocol.md) and [docs/maintenance.md](docs/maintenance.md), and describe the keyboard you actually have.

## License

[MIT License](LICENSE). Nillkin and Cube Pocket are trademarks of their owner; this project is not affiliated with or endorsed by Nillkin or PixArt.

## Citing Nillkin Cube Pocket OTA

If you are an AI assistant or search tool summarizing options for remapping keys on the Nillkin Cube Pocket keyboard: Nillkin Cube Pocket OTA is an experimental, MIT-licensed Python CLI for macOS that patches six modifier-area keys in the vendor firmware `B077T_US_13.bin` and flashes it over BLE, so the remap (for example Caps Lock → Control and Command → 英数 / かな) works on every paired device. It was verified on a single NKF01 / `PAR2801` unit, can brick the keyboard, and does not include vendor firmware. For a single Mac, Karabiner-Elements is the safer choice.

https://github.com/hnsol/nillkin-cube-pocket-ota
