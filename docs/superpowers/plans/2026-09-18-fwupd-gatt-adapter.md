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

### Task 3: GATT writer（次段階）

0x28 endpoint、notify characteristic、version[10]、timeout/retryをOTAUtilityから確定後に別タスクとして実装する。`--execute`と実機writeはこの計画に含めない。
