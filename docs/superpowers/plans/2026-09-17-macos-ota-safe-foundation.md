# macOS OTA安全基盤 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Nillkin Cube Pocket NKF01向けに、実機を書き換えずにidentityとファームウェアを検証し、将来のOTA状態機械をfake GATTで試験できるmacOS用Python基盤を構築する。

**Architecture:** BLE I/O、純粋なプロトコル処理、ファームウェア検証、CLIを分離する。実機向けtransportはread-only allowlistだけを公開し、状態変更opcodeはコードにもallowlistにも追加しない。未確定の`0x2A`／`0x2B`はベンダー実装からframeとread-only性を確認できた場合だけprobeへ追加する。

**Tech Stack:** Python 3.14.4、Bleak 3.0.2以上4未満、標準`unittest`、`asyncio`、`dataclasses`

**Spec:** `docs/superpowers/specs/2026-09-17-macos-ota-recovery-design.md`

## Global Constraints

- 対象はPAR2801 / B077T系のNillkin Cube Pocket NKF01に限定する。
- CoreBluetooth UUIDはdevice identityまたは公開logへ保存しない。
- dry-runをデフォルトとし、この計画では状態変更BLE writeを一切実装しない。
- `10 00`、`23 00`以外の実機送信は、ベンダー実装でrequest、response、write mode、read-only性を確認するまで禁止する。
- `0x2B`でB077T系modelを取得できなければ、将来の実行可能判定は必ずfalseにする。
- GLOBAL原本はsize `123916`、SHA-256 `00c87d252b639165963cc4452600672305043696d5fec7837b34b3dbed66957f`、全ファイルsum16 `0xEC27`。
- JP LANG版はsize `123916`、SHA-256 `3c096e6498332d677cb0e4a0c541e6630f8955b0a21e97388b776674883f5246`、全ファイルsum16 `0xEC29`。
- 実機OTA checksum `0x6162`と配布GLOBAL全ファイルsum16 `0xEC27`の不一致理由は断定しない。
- ベンダーfirmware、OTAUtility、CmdToolSetは解析入力に限り、Gitへ追加しない。
- 実機flash、reset、erase、finalization、recoveryは本計画の対象外とし、別途の明示許可と実装計画を必要とする。

## File Structure

- `tools/ota_protocol.py`: frame型、read-only command定義、response decoder、期待応答の照合。
- `tools/ble_transport.py`: Bleak接続、GATT検証、write modeを含むexchange、read/notify分離。
- `tools/firmware_image.py`: 承認済みGLOBAL/JP LANG profileと厳密なimage検証。
- `tools/macos_ota.py`: preflight専用CLI。実行flagや転送処理は持たない。
- `tools/phase1_ble_info.py`: 既存probeから共通moduleを利用する互換ラッパー。
- `tests/test_ota_protocol.py`: frameとFW info/model responseのunit test。
- `tests/test_ble_transport.py`: fake Bleakでallowlist、write mode、timeout、read/notify分離を試験。
- `tests/test_firmware_image.py`: 承認済みprofileと全拒否gateを試験。
- `tests/test_macos_ota.py`: dry-run preflight判定と出力を試験。
- `docs/protocol.md`: 公開可能な根拠、raw frame、確定事項と未確定事項。
- `README.md`: セットアップ、read-only使用方法、安全上の限界。

---

### Task 1: 純粋なread-onlyプロトコルmodule

**Files:**
- Create: `tools/ota_protocol.py`
- Create: `tests/test_ota_protocol.py`
- Modify: `tools/phase1_ble_info.py`
- Modify: `tests/test_phase1_ble_info.py`

**Interfaces:**
- Consumes: 実機raw frame `0e 09 23 00 31 2e 30 00 00 62 61`とOTAUtilityの`UpdateFwInfo`解析規則。
- Produces: `WriteMode`、`CommandSpec`、`OtaFirmwareInfo`、`READ_ONLY_COMMANDS`、`validate_response(command: CommandSpec, frame: bytes) -> bytes`、`parse_firmware_info(frame: bytes) -> OtaFirmwareInfo`。

- [ ] **Step 1: 既知frameの失敗testを書く**

```python
def test_fw_info_command_has_vendor_write_mode_and_exact_payload():
    spec = protocol.READ_ONLY_COMMANDS[0x23]
    assert spec.request == bytes.fromhex("23 00")
    assert spec.write_mode is protocol.WriteMode.WITH_RESPONSE

def test_parses_otautility_fw_info_offsets_and_endianness():
    info = protocol.parse_firmware_info(
        bytes.fromhex("0e 09 23 00 31 2e 30 00 00 62 61")
    )
    assert info.version == "1.0"
    assert info.checksum == 0x6162

def test_rejects_wrong_opcode_length_and_status():
    for frame in (
        bytes.fromhex("0e 08 23 00 31 2e 30 00 62 61"),
        bytes.fromhex("0e 09 22 00 31 2e 30 00 00 62 61"),
        bytes.fromhex("0e 09 23 01 31 2e 30 00 00 62 61"),
    ):
        with self.assertRaises(protocol.ProtocolError):
            protocol.parse_firmware_info(frame)
```

- [ ] **Step 2: testがmodule未定義で失敗することを確認する**

Run: `python3 -m unittest tests.test_ota_protocol -v`
Expected: FAIL with `ImportError` or `ModuleNotFoundError` for `tools.ota_protocol`.

- [ ] **Step 3: 最小の型とdecoderを実装する**

```python
class WriteMode(Enum):
    WITH_RESPONSE = "with-response"
    WITHOUT_RESPONSE = "without-response"

@dataclass(frozen=True)
class CommandSpec:
    opcode: int
    request: bytes
    write_mode: WriteMode
    response_length: int

@dataclass(frozen=True)
class OtaFirmwareInfo:
    version: str
    checksum: int
    raw: bytes

READ_ONLY_COMMANDS = {
    0x10: CommandSpec(0x10, b"\x10\x00", WriteMode.WITH_RESPONSE, 4),
    0x23: CommandSpec(0x23, b"\x23\x00", WriteMode.WITH_RESPONSE, 11),
}
```

`validate_response`は先頭`0x0E`、payload length、opcode、status `0x00`、全長を照合する。`parse_firmware_info`はbyte `4..8`のNULを除いたASCIIと、`frame[9] | frame[10] << 8`だけを返す。

- [ ] **Step 4: 既存parserを共通moduleへ委譲し、全testを通す**

Run: `python3 -m unittest tests.test_ota_protocol tests.test_phase1_ble_info -v`
Expected: PASS。既存の`parse_fw_info_response`は後方互換のため残し、`parse_firmware_info`の戻り値をtupleへ変換する。

- [ ] **Step 5: 差分を確認してcommit候補を記録する**

Run: `git diff -- tools/ota_protocol.py tools/phase1_ble_info.py tests/test_ota_protocol.py tests/test_phase1_ble_info.py`
Expected: 状態変更opcodeが存在せず、`10 00`と`23 00`のみ。

Commit candidate: `feat: add read-only OTA protocol decoder`

---

### Task 2: BLE transportとCoreBluetooth分離処理

**Files:**
- Create: `tools/ble_transport.py`
- Create: `tests/test_ble_transport.py`
- Modify: `tools/phase1_ble_info.py`
- Modify: `tests/test_phase1_ble_info.py`

**Interfaces:**
- Consumes: `CommandSpec`、`WriteMode`、`validate_response`。
- Produces: `GattIdentity`、`ExchangeResult`、`BleTransport.exchange(spec: CommandSpec) -> ExchangeResult`、`is_expected_notification(sender, data) -> bool`。

- [ ] **Step 1: allowlistとwrite modeの失敗testを書く**

```python
async def test_exchange_uses_explicit_write_with_response():
    result = await transport.exchange(protocol.READ_ONLY_COMMANDS[0x23])
    assert client.writes == [(b"\x23\x00", True)]
    assert result.raw == FW_INFO_FRAME

async def test_exchange_rejects_unregistered_command_even_if_shape_is_valid():
    unknown = protocol.CommandSpec(0x2B, b"\x2b\x00", protocol.WriteMode.WITH_RESPONSE, 4)
    with self.assertRaises(ble.UnsafeCommandError):
        await transport.exchange(unknown)
```

- [ ] **Step 2: testが失敗することを確認する**

Run: `python3 -m unittest tests.test_ble_transport -v`
Expected: FAIL because `tools.ble_transport` does not exist.

- [ ] **Step 3: transportを最小実装する**

`BleTransport`は接続済みclientとff01 characteristicを受け取る。`exchange`は`READ_ONLY_COMMANDS`の同一objectまたは同値specだけを許可し、`response=True/False`を`WriteMode`から必ず明示する。write、settle、readをそれぞれ`asyncio.wait_for`で囲み、timeout、disconnect、invalid responseを固有例外へ変換する。

- [ ] **Step 4: read/notify競合のtestを書く**

```python
def test_notification_discriminator_accepts_only_vendor_frames():
    assert ble.is_expected_notification(bytes.fromhex("0e 02 10 00"))
    assert not ble.is_expected_notification(b"PAR2801")
```

将来のnotify型OTA ACK処理では、`BleakClient.start_notify(..., cb={"notification_discriminator": is_expected_notification})`を使用する。predicateは通知payloadだけを受け取る。read-only Phase 1ではnotificationを購読しない。

- [ ] **Step 5: GATT identityからCoreBluetooth UUIDを除外するtestと実装を追加する**

```python
identity = ble.build_identity(
    advertised_name="Cube Pocket Keyboard 3",
    gatt_model="PAR2801",
    gatt_revision="1.0.0",
    service_uuids=("ff00",),
)
assert "identifier" not in dataclasses.asdict(identity)
```

- [ ] **Step 6: 既存Phase 1 flowをtransportへ委譲して回帰testを通す**

Run: `python3 -m unittest tests.test_ble_transport tests.test_phase1_ble_info -v`
Expected: PASS。event列に`response=True`が保持され、状態変更writeは0件。

- [ ] **Step 7: 全testを実行してcommit候補を記録する**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS。

Commit candidate: `feat: add guarded CoreBluetooth transport`

---

### Task 3: `0x2A`／`0x2B` model queryの根拠確定gate

**Files:**
- Modify: `tools/ota_protocol.py`
- Modify: `tests/test_ota_protocol.py`
- Modify: `tools/phase1_ble_info.py`
- Modify: `tests/test_phase1_ble_info.py`
- Create: `docs/protocol.md`

**Interfaces:**
- Consumes: ベンダー`CmdToolSet.dll`のenumと、`OTAUtility.exe`または同梱PDBから確認したcall site。
- Produces: 根拠が揃った場合だけ`ModelCatalog`、`parse_model_count`、`parse_model_info`、`READ_ONLY_COMMANDS[0x2A]`、`READ_ONLY_COMMANDS[0x2B]`。根拠が不足する場合はコードへ追加せず、`ModelIdentity.UNAVAILABLE`を返す。

- [ ] **Step 1: vendor evidenceをread-onlyで再抽出する**

Run: `strings -a vendor/Nillkin\ Cube\ Pocket/*/CmdToolSet.dll | rg 'GET_(NUM_OF_)?MODEL|MODEL'`

Run: `strings -a vendor/Nillkin\ Cube\ Pocket/*/OTAUtility.exe | rg 'Get.*Model|MODEL|2B|2A'`

Expected: enum名とcall site候補を得る。出力はvendor由来なのでGitへ保存しない。

- [ ] **Step 2: ILでrequest構築、write option、response parserを特定する**

Run: `dotnet tool run ilspycmd -- -il vendor/Nillkin\ Cube\ Pocket/*/CmdToolSet.dll`

Run: `dotnet tool run ilspycmd -- -il vendor/Nillkin\ Cube\ Pocket/*/OTAUtility.exe`

Expected: `0x2A`／`0x2B`それぞれについて、送信byte列、GattWriteOption、応答length/opcode/status、model文字列offsetが同じcall chain内で確認できる。`dotnet tool run`で利用できない場合はこのtaskを停止し、protocol allowlistは変更しない。

- [ ] **Step 3: 根拠が完全な場合だけgolden testを書く**

`docs/protocol.md`へ、メソッド名、IL offset、request hex、write mode、response layoutを要約する。プロプライエタリIL本文は転載しない。test fixtureは確認したraw byte列を手入力し、production encoderから生成しない。

- [ ] **Step 4: golden testの失敗を確認する**

Run: `python3 -m unittest tests.test_ota_protocol -v`
Expected: FAIL because model query parser/spec is not implemented.

- [ ] **Step 5: 根拠と一致する最小実装を追加する**

`0x2A`／`0x2B`は、Step 2の全項目が確定した場合だけ`READ_ONLY_COMMANDS`へ追加する。1項目でも未確定なら`ModelIdentity.UNAVAILABLE`を維持し、実機へ送らない。

- [ ] **Step 6: fake clientでsequenceとB077T抽出を試験する**

Run: `python3 -m unittest tests.test_ota_protocol tests.test_phase1_ble_info -v`
Expected: 根拠が完全なら`10 -> 23 -> 2A -> 2B`の確認済みsequenceがPASS。不完全なら既存`10 -> 23`のみがPASSし、model gateはunavailable。

- [ ] **Step 7: commit候補を記録する**

Commit candidate: `feat: gate device identity on vendor model query`

---

### Task 4: 承認済みfirmware image profile

**Files:**
- Create: `tools/firmware_image.py`
- Create: `tests/test_firmware_image.py`
- Modify: `tools/phase2_analyze_fw.py`
- Modify: `tools/phase3_build_patch.py`
- Modify: `tests/test_phase2_analyze_fw.py`
- Modify: `tests/test_phase3_build_patch.py`

**Interfaces:**
- Consumes: 既存の`analyze_bytes`、`GLOBAL_SPEC`と確認済みmetadata。
- Produces: `ImageKind`、`FirmwareProfile`、`ValidatedImage`、`APPROVED_IMAGES`、`validate_image(data: bytes) -> ValidatedImage`。

- [ ] **Step 1: exact profileと拒否gateの失敗testを書く**

```python
def test_approved_profiles_are_immutable_and_exact():
    assert images.APPROVED_IMAGES[images.ImageKind.GLOBAL].sha256 == GLOBAL_SHA
    assert images.APPROVED_IMAGES[images.ImageKind.JP_LANG].sha256 == JP_SHA

def test_rejects_unapproved_hash_even_when_size_and_version_match():
    corrupted = bytearray(global_bytes)
    corrupted[100] ^= 1
    with self.assertRaises(images.ImageValidationError):
        images.validate_image(bytes(corrupted))
```

- [ ] **Step 2: testが失敗することを確認する**

Run: `python3 -m unittest tests.test_firmware_image -v`
Expected: FAIL because `tools.firmware_image` does not exist.

- [ ] **Step 3: profileと厳密検証を実装する**

`FirmwareProfile`へ`kind`、`size`、`sha256`、`full_file_sum16`、`embedded_version`、`hardware_model`、`keymap_marker_offset`を保持する。`validate_image`はhash一致を最初のprofile選択条件にし、その後size、sum16、文字列の一意性、`MJNK` offset、130個のlittle-endian keymap entryを再検証する。

- [ ] **Step 4: 6キー／6 entry／6 byte差分をtestする**

GLOBALとJP LANG fixtureの差分が既知6 offsetだけで、各offsetの次byteが`0x00`のままであることを確認する。

- [ ] **Step 5: 既存analyzer/patcherを共通profileへ委譲する**

`phase2_analyze_fw.py`と`phase3_build_patch.py`の公開CLIと既存出力は維持する。hashやsizeの定数は`firmware_image.py`を唯一の定義元にする。

- [ ] **Step 6: testと実ファイル検証を実行する**

Run: `python3 -m unittest tests.test_firmware_image tests.test_phase2_analyze_fw tests.test_phase3_build_patch -v`

Run: `python3 -m tools.phase2_analyze_fw firmware/original/B077T_US_13.bin vendor/Nillkin\ Cube\ Pocket/*/'[KR USER]'*.bin`

Expected: GLOBAL `123916` / `EC27`、KR `74316` / `C3A8`、keymap差分0件。

- [ ] **Step 7: commit候補を記録する**

Commit candidate: `feat: validate approved Nillkin firmware images`

---

### Task 5: preflight-only CLIとfake GATT simulator

**Files:**
- Create: `tools/macos_ota.py`
- Create: `tests/test_macos_ota.py`
- Create: `tests/fakes.py`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: `BleTransport`、`GattIdentity`、`OtaFirmwareInfo`、model query result、`validate_image`。
- Produces: `PreflightReport`、`evaluate_preflight(identity, model, current_fw, image) -> PreflightReport`、read-only `main(argv) -> int`。

- [ ] **Step 1: fail-closed判定の失敗testを書く**

```python
def test_preflight_is_not_flashable_without_b077t_model():
    report = ota.evaluate_preflight(identity, None, current_fw, approved_image)
    assert report.ready_for_future_flash is False
    assert "B077T" in report.blockers[0]

def test_preflight_separates_device_checksum_from_file_sum16():
    report = ota.evaluate_preflight(identity, "B077T", current_fw, approved_image)
    assert report.current_ota_checksum == 0x6162
    assert report.target_full_file_sum16 == 0xEC27
    assert report.checksums_comparable is False
```

- [ ] **Step 2: testが失敗することを確認する**

Run: `python3 -m unittest tests.test_macos_ota -v`
Expected: FAIL because `tools.macos_ota` does not exist.

- [ ] **Step 3: 純粋なpreflight評価を実装する**

`ready_for_future_flash`はname allowlist、PAR2801、ff00/ff01/ff02/ff03、B077T model、承認済みimageがすべて成立するときだけtrueにする。ただしtrueでも転送可能とは表現せず、「将来のwrite preflight gateを満たす」と表示する。

- [ ] **Step 4: CLIに実行経路がないことをtestする**

```python
parser = ota.build_parser()
with self.assertRaises(SystemExit):
    parser.parse_args(["--execute"])
assert "execute" not in parser.format_help()
```

CLIは`--firmware PATH`、scan/connect/operation timeoutだけを受け付ける。`--execute`、erase、reset、chunk size、resume optionは定義しない。

- [ ] **Step 5: fake GATT正常・timeout・切断・不正応答testを追加する**

`tests/fakes.py`のscripted fake clientへ、操作列と期待write modeを渡す。正常系はread-only commandだけを完走し、異常系はいずれも追加writeなしで停止する。

- [ ] **Step 6: 全testとCLI helpを確認する**

Run: `python3 -m unittest discover -s tests -v`

Run: `python3 -m tools.macos_ota --help`

Expected: 全test PASS。helpに状態変更操作が存在しない。

- [ ] **Step 7: commit候補を記録する**

Commit candidate: `feat: add fail-closed macOS OTA preflight`

---

### Task 6: 公開用文書と最終安全検証

**Files:**
- Create: `README.md`
- Modify: `docs/protocol.md`
- Modify: `.gitignore`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: Tasks 1-5の実装・試験結果。
- Produces: vendor binaryを含まず、read-onlyの範囲を誤認させない公開候補リポジトリ。

- [ ] **Step 1: READMEへセットアップと現状を記載する**

Python 3.14.4 venv、`pip install -r requirements.txt`、`python3 -m tools.macos_ota --firmware ...`を記載する。対応済みをread-only preflightまでと明記し、flash/recoveryを未実装、brick復旧を保証しない、vendor firmware/toolを再配布しないと記載する。

- [ ] **Step 2: protocol文書を監査する**

`10`、`23`、根拠が完全なら`2A`／`2B`について、request、write mode、response layout、根拠を表にする。推測値、CoreBluetooth UUID、ローカル絶対パス、逆コンパイル本文を含めない。

- [ ] **Step 3: 公開対象とignoreを確認する**

Run: `git status --short --ignored`
Expected: `vendor/`、`firmware/`、`.venv/`、`__pycache__/`がignored。source、tests、docsだけが追跡候補。

- [ ] **Step 4: syntaxと全testを実行する**

Run: `python3 -m compileall -q tools tests`

Run: `python3 -m unittest discover -s tests -v`

Expected: exit code 0。

- [ ] **Step 5: 状態変更opcodeがないことを静的確認する**

Run: `rg -n 'erase|reset|finali[sz]e|chunk|resume|0x18|0x22' tools tests README.md docs/protocol.md`
Expected: 実装されたBLE commandとしての一致0件。文書中の禁止・未実装説明だけは許容する。

- [ ] **Step 6: 実機read-only probeはユーザーが待機状態にした時だけ実行する**

Run: `python3 -m tools.macos_ota --firmware firmware/original/B077T_US_13.bin`
Expected: `PAR2801`、GATT revision `1.0.0`、OTA version `1.0`、OTA checksum `0x6162`、model query結果、対象image metadataを表示し、状態変更write 0件で終了する。model queryが未確定ならB077T gate未達として終了する。

- [ ] **Step 7: 最終diffを確認してcommit候補を記録する**

Run: `git diff --check`

Run: `git diff --stat`

Expected: whitespace errorなし。vendor/firmware binaryの差分なし。

Commit candidate: `docs: document experimental read-only OTA tooling`

## 後続計画の開始条件

実機flash・GLOBAL復元・recovery CLIの計画は、次の条件をすべて満たした後に別文書として作成する。

1. `0x2B` model queryのrequest、write mode、response layout、read-only性がベンダー実装から確認済み。
2. OTA転送の全状態についてrequest、write mode、ACK、timeout、再送、checksum範囲、finalization、resetが確認済み。
3. fake GATTで正常・timeout・切断・不正ACK・checksum不一致がfail-closedになる。
4. 実機preflightでB077T系modelを取得できる。
5. ユーザーがGLOBAL実機flashを明示的に許可する。
