# 運用・保守ノート

この文書は、Nillkin Cube Pocketの状態確認、キーマップ変更、OTA再実行、
中断時の判断に必要な情報をまとめたものです。プロトコルの詳細は
[`protocol.md`](protocol.md)を参照してください。

## 確認済みの対象

- Product: Nillkin Cube Pocket Foldable Bluetooth Keyboard with Touchpad
- Model: NKF01
- GATT model: `PAR2801`
- GATT revision: `1.0.0`
- BLE advertised name: `Cube Pocket Keyboard 1` / `2` / `3`
- 確認環境: macOS / CoreBluetooth / Python 3.14.4

CoreBluetooth UUIDはMacごとに異なるため、機種判定には使いません。

## 実機で確認済みの状態

| 状態 | OTA version | OTA checksum | 備考 |
| --- | --- | --- | --- |
| 工場出荷FW | `1.0` | `0x6162` | 配布GLOBALとはchecksumの意味・対象範囲が同じと断定しない |
| 配布GLOBAL | `1.0.1` | `0xEC27` | `B077T_US_13`、書込み・起動確認済み |
| JP_LANG | `1.0.1` | `0xEC29` | 書込み・起動・キー入力確認済み |

確認済みファイル:

| ファイル | SHA-256 |
| --- | --- |
| `B077T_US_13.bin` | `00c87d252b639165963cc4452600672305043696d5fec7837b34b3dbed66957f` |
| `B077T_US_13_JP_LANG.bin` | `3c096e6498332d677cb0e4a0c541e6630f8955b0a21e97388b776674883f5246` |

JP_LANGで確認した配列:

```text
Caps      -> Ctrl
左Ctrl    -> Option
左Option  -> Cmd
左Cmd     -> 英数 (LANG2)
右Cmd     -> かな (LANG1)
右Option  -> Cmd
```

## 作業前の準備

1. キーボードを十分に充電する。
2. FW原本を変更せず保持する。
3. venvを有効化し、リポジトリのルートで実行する。
4. 最初は必ず`--execute`なしのread-only preflightを行う。
5. 書込み中はキーボードを操作せず、自動OFFやMacのスリープを避ける。

scan時はキーボードをペアリング待機状態にします。すでに接続済みでも見つからない場合は、
いったん電源を入れ直してから対象Bluetoothスロットをペアリング待機にします。

## 状態確認

GLOBAL原本を基準にread-only preflightを行います。このコマンドはFW本体を書き込みません。

```sh
python3 -m tools.macos_ota \
  --firmware firmware/original/B077T_US_13.bin
```

表示された`Current OTA version`と`Current OTA checksum`を上の表と照合します。
Vendor OTA modelが`unavailable`でも、既知signatureを使う経路があります。
`--probe-vendor-model`は`0x2A`がタイムアウトする実機があるため、通常は不要です。

## キーマップの生成

`configs/jp-lang.toml`をコピーして、物理キーと出力するHID Usage名を指定します。
指定しないキーは変更されません。

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

生成例:

```sh
python3 -m tools.phase3_build_patch downloaded/B077T_US_13.bin \
  --config configs/my-layout.toml \
  --patched-name B077T_US_13_MY_LAYOUT.bin \
  --output-root .
```

生成時に表示されるサイズ、差分、sum16、SHA-256を保存します。同名ファイルは上書きされません。
別のGLOBAL版を流用せず、確認済みの原本と設定から毎回再生成してください。

## OTA書込み

工場出荷FWから確認済みGLOBALへ更新する場合:

```sh
python3 -m tools.macos_ota \
  --firmware firmware/original/B077T_US_13.bin \
  --execute \
  --accept-factory-signature \
  --confirm-sha256 00c87d252b639165963cc4452600672305043696d5fec7837b34b3dbed66957f
```

確認済みGLOBALからJP_LANGへ更新する場合:

```sh
python3 -m tools.macos_ota \
  --firmware firmware/patched/B077T_US_13_JP_LANG.bin \
  --execute \
  --accept-installed-global-signature \
  --confirm-sha256 3c096e6498332d677cb0e4a0c541e6630f8955b0a21e97388b776674883f5246
```

独自設定版では、生成に使ったGLOBAL原本とTOMLも指定します。

```sh
python3 -m tools.macos_ota \
  --firmware firmware/patched/B077T_US_13_MY_LAYOUT.bin \
  --base-firmware firmware/original/B077T_US_13.bin \
  --remap-config configs/my-layout.toml \
  --execute \
  --accept-installed-global-signature \
  --confirm-sha256 <生成時に表示されたSHA-256>
```

`--confirm-sha256`には、対象ファイルの64桁の値を省略せず指定します。

## 中断・エラー時の判断

まず再送せず、保持状態をread-onlyで確認します。

```sh
python3 -m tools.macos_recover \
  --firmware firmware/original/B077T_US_13.bin \
  --inspect-state
```

主な見方:

- `Offset (objects): 0` / `Checksum: 0x0000`: 永続化済みcheckpointは先頭。
- `GLOBAL prefix match: yes`: 表示offsetまでの状態が指定GLOBALと一致。
- `GLOBAL prefix match: no`: GLOBALを前提に自動再開してはいけない。
- 実機ではGLOBAL転送中に`offset=30 / checksum=0x24EC`、JP_LANG転送中に
  `offset=30 / checksum=0x24EE`を観測した。これは観測例であり、常にこの値になる保証はない。

全payload後の`ACK 0x18 timed out`は、失敗確定ではなく最終結果が不明な状態です。
この場合は次の順で確認します。

1. OTAを再送しない。
2. 30秒以上待ってCLIが停止したことを確認する。
3. キーボードの電源を一度切り、入れ直す。
4. read-only preflightでversion/checksumを確認する。
5. `0xEC27`ならGLOBAL、`0xEC29`ならJP_LANGが起動済みと判断する。

実機ではJP_LANG転送後にhostが成功ACKを受信できませんでしたが、電源再投入後に
`1.0.1 / 0xEC29`を確認し、全6キーが正常に動作しました。

## 初回GLOBAL転送の復旧

これは、工場出荷FWからGLOBALへの初回転送が中断した場合の再開手順です。
BLE広告とGATTへ接続でき、GLOBAL prefixが一致する場合に限り実行します。

```sh
python3 -m tools.macos_recover \
  --firmware firmware/original/B077T_US_13.bin \
  --execute \
  --accept-factory-signature \
  --confirm-sha256 00c87d252b639165963cc4452600672305043696d5fec7837b34b3dbed66957f
```

復旧CLIはcheckpointまでのprefix checksumが一致しない場合、payload、upgrade、resetを送信せず停止します。
正常起動中のJP_LANGからGLOBALへ戻す一般的なdowngradeは、現在の既知signature gateでは
許可されません。`--accept-factory-signature`を回避策として流用してはいけません。

## やってはいけないこと

- 原本FWを直接編集または上書きする。
- 別FWのSHA-256を`--confirm-sha256`へ流用する。
- final ACK timeout直後に同じFWを再送する。
- `GLOBAL prefix match: no`の状態でGLOBAL復旧を強行する。
- 根拠を確認していないopcodeやFWを実機へ送る。
- OTA中にキーボードを電源OFF、Macをスリープ、またはPythonを強制終了する。

## 復旧できない可能性がある状態

BLE広告が出ず、scanでも検出できない完全brick状態は、このツールだけでは照会・復旧できません。
OTA中断からの復旧実績は、完全brickからの復旧可能性を意味しません。

## 確認済みの制約と未確認事項

- macOSでは物理WNR fragmentを44 bytes、CoreBluetooth pacingを10msとして実機確認した。
- Steam DeckではATT MTU 23、WNR payload limit 20 bytesを確認したが、最終OTAはmacOSで実施した。
- Vendor model query `0x2A` / `0x2B`は、この実機では安定して使用できなかった。
- CoreBluetoothが最終成功ACKを取り逃す場合がある。
- 正常起動中のJP_LANGからGLOBALへ戻す経路は未実装。
- 別個体、別hardware revision、別の配布FWでは未検証。
- vendor提供のfirmwareとWindows toolは、このリポジトリでは再配布しない。
