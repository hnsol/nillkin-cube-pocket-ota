# fwupd PixArt OTA GATT adapter 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. TDDで実装する。

**Goal:** fwupdのPixArt OTA状態機械とWindows OTAUtilityのGATT wire formatを組み合わせ、実機へ書く直前まで検証できるmacOS dry-run/simulatorを作る。

**Architecture:** `tools/pixart_ota.py`を副作用のないprotocol/plannerとし、既存`tools/macos_ota.py`は承認済みFWの検証結果と静的な転送計画だけを表示する。状態変更BLE transportは、Characteristic・notify・timeoutが確定する次段階まで追加しない。

**Tech Stack:** Python 3.14、Bleak 3、`unittest`

**Spec:** `docs/superpowers/specs/2026-09-17-macos-ota-recovery-design.md`

## Global Constraints

- 実機へstate-changing opcode、firmware payload、resetを送らない。
- GLOBALとJP LANGの承認済みSHA-256以外を拒否する。
- Windows OTAUtilityで確認したGATT wire formatを優先し、fwupdのHID report IDを混入しない。
- integerはlittle-endian、checksumは全byteのsum16 mod 65536。
- 不明なversion[10]、専用retransmit endpoint、notify semanticsは推測で実行可能にしない。

---

### Task 1: transport-neutral PixArt OTA protocol

**Files:**
- Create: `tools/pixart_ota.py`
- Create: `tests/test_pixart_ota.py`

**Interfaces:**
- Produces: `OtaState`, `TransferOperation`, `build_init_new`, `parse_init_new_response`, `build_object_create`, `build_upgrade`, `build_reset`, `iter_transfer_operations`。

- [ ] golden vector testを先に書き、未実装でREDを確認する。
- [ ] Windows OTAUtility確認値を実装する: `28 00`, `10 00`, `27|size:u32|00`, `25|addr:u32|size:u32`, raw payload, `18|size:u32|sum16:u16|version[10]`, `22 00`。
- [ ] 0x27 GATT response `0e 10 27` + 15-byte stateを厳密にparseし、status=0/new_flow=1/spec_result=1、max object/MTU/PRNの妥当性を検証する。
- [ ] object最大4096、device MTU chunking、PRN/object末尾ACK位置、running sum16を再現する。
- [ ] resume offset/checksum不一致はゼロから再開する計画にする。
- [ ] malformed state、MTU 0、PRN 0、範囲外offset、version長超過をfail-closedにする。

### Task 2: macOS dry-run表示

**Files:**
- Modify: `tools/macos_ota.py`
- Modify: `tests/test_macos_ota.py`
- Modify: `docs/protocol.md`

**Interfaces:**
- Consumes: 承認済み`ValidatedImage`とTask 1のprotocol/planner。
- Produces: `--show-transfer-plan`。BLE scan/connect/writeを行わず、target hash/size/sum16と確定済みwire operation、未確定gateを表示する。

- [ ] CLI testを先に書きREDを確認する。
- [ ] `--show-transfer-plan`時はファイル検証だけを行い、BLE import・scan・connectをしない。
- [ ] state依存のMTU/PRN/resumeは「実機0x27応答で決定」と表示する。
- [ ] version[10]とretransmit endpointが未確定である限り`Executable: no`と表示する。
- [ ] 既存read-only CLIの挙動を維持し、全testを実行する。

### Task 3: GATT writer（実装のみ。実機実行は別承認）

OTAUtility ILから次を確認した。

- control/data/notifyは`ff01`。notify CCCDも`ff01`で有効化する。
- `0x28` retransmit/reset-stateは`ff02`へwith-responseで送る。
- `0x27`、`0x25`、raw payload、`0x18`、`0x22`は`ff01`を使う。
- `0x27`/`0x25`/`0x18`はwith-response、payload/`0x22`はwithout-response。
- upgradeのversion[10]は同梱`setting.ini`の`OTA_FW_VERSION=1.0.1`をASCII/NUL paddingした値。
- object/PRN/upgrade ACKは`ff01` notifyで受ける。

**Files:**
- Create: `tools/gatt_ota.py`
- Create: `tests/test_gatt_ota.py`
- Modify: `tools/macos_ota.py`
- Modify: `tests/test_macos_ota.py`
- Modify: `docs/protocol.md`

- [ ] fake GATTでendpoint、write mode、順序、ACK checksum、timeout、disconnectをTDDする。
- [ ] notifyを先に購読し、`0x28`→`0x27`→object/payload/ACK→`0x18`→`0x22`を実装する。
- [ ] 全BLE操作を有限timeoutにし、unexpected/malformed notifyをfail-closedにする。
- [ ] `--execute`は承認済みSHA-256とB077T実機preflightに加え、対象SHA-256の明示確認を必須にする。
- [ ] `--recover-global`はGLOBALだけを許可し、まず`0x27`でresumeを検証する。不一致なら`0x28`後にoffset/checksum 0を再確認して全転送する。
- [ ] CLI実装とfake testまでは実施してよいが、実機へのstate-changing writeはユーザーの別途明示許可まで行わない。
