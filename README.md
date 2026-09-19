# Nillkin Cube Pocket OTA

macOSでNillkin Cube Pocketのfirmwareを検証・生成・OTA送信する実験的ツールです。

## セットアップ

Python 3.14.4でvenvを作成します。
BLE scanは、service UUIDを推測して絞り込まないためmacOS 12.3以降を対象とします。

```sh
python3.14 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## 実行

キーボードをペアリング待機状態にしてから、承認済みfirmware imageを指定します。

```sh
python3 -m tools.macos_ota --firmware firmware/original/B077T_US_13.bin
```

通常実行はread-only preflightです。BLE scan、GATT情報、`0x10` OTA init、`0x23` firmware infoを取得します。FWは送信しません。
工場出荷FWでは`0x2A` model countが応答せずタイムアウトするため、通常経路では送信しません。このためB077T modelのwrite gateは未達のままです。`--probe-vendor-model`を明示した場合だけ、同一接続上で`0x10`→`0x23`の後に`0x2A`→`0x2B`を試します。工場出荷FWではタイムアウトする可能性があります。
実機probeは、接続先と応答を確認できる状態でのみ実行してください。

## キーマップ設定とpatch生成

`configs/jp-lang.toml` は、確認済みGLOBAL原本から以下のJP配列を作る設定例です。

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

左辺は物理キー、右辺は出力するHID Usage名です。指定しない物理キーは変更しません。使用できる物理キーは `caps_lock`、`left_control`、`left_alt`、`left_gui`、`right_gui`、`right_alt` です。右辺には `caps_lock`、`space`、`left_control`、`left_shift`、`left_alt`、`left_gui`、`right_control`、`right_shift`、`right_alt`、`right_gui`、`lang1`、`lang2` を指定できます。

原本を直接指定して、コピーとpatchを新規作成します。出力済みの同名ファイルは上書きしません。

```sh
python3 -m tools.phase3_build_patch downloaded/B077T_US_13.bin \
  --config configs/jp-lang.toml \
  --output-root .
```

この例の出力先は `firmware/original/B077T_US_13.bin` と `firmware/patched/B077T_US_13_JP_LANG.bin` です。設定ファイル名からの出力名が不都合な場合は、ファイル名だけを `--patched-name MY_LAYOUT.bin` で指定できます。

## 書込み前のdry-run

BLEへ接続せず、既知のOTA送信手順だけを表示します。

```sh
python3 -m tools.macos_ota \
  --firmware firmware/patched/B077T_US_13_JP_LANG.bin \
  --show-transfer-plan
```

## OTA書込み

`--execute`は、同一接続上で再取得したadvertised name、`PAR2801`、`B077T` model、GATT構成を検証してから送信します。さらに、対象ファイルのSHA-256を明示確認しなければ動きません。

`0x27`応答の`mtu_size=244`はOTA上の論理ブロック長です。BLEのATT write上限とは
別で、raw payloadはvendor実装どおりwrite-without-response（WNR）のみを使います。
実測したhost MTUはSteam Deckで23、macOSで50であり、ATT header 3 bytesを除く
物理断片はそれぞれ最大20/47 bytesです。244-byte論理ブロックをこの上限以下へ分割し、
各断片でCoreBluetoothの送信可能状態と2ms pacingを確認します。PRN threshold 16は
物理write 16回ではなく論理ブロック16個を表すため、4096-byte objectのACK境界は
3904 bytes（244×16）とobject末尾4096 bytesです。

工場出荷FWからの初回更新に限り、`--accept-factory-signature`で固定GLOBAL原本だけを許可できます。advertised name `Cube Pocket Keyboard 3`、GATT model `PAR2801`、revision `1.0.0`、必須`ff00`/`ff01`/`ff02`/`ff03`、`0x23` raw応答 `0e 09 23 00 31 2e 30 00 00 62 61`がすべて完全一致した場合だけです。checksum `0x6162`だけでは識別しません。JP_LANG、設定生成FW、復旧には使えず、`--probe-vendor-model`とも併用できません。

手順1: 工場出荷FWから固定GLOBALへ更新します。コマンドを示すだけで、自動実行はしません。

```sh
python3 -m tools.macos_ota \
  --firmware firmware/original/B077T_US_13.bin \
  --execute \
  --accept-factory-signature \
  --confirm-sha256 00c87d252b639165963cc4452600672305043696d5fec7837b34b3dbed66957f
```

再起動後に再接続し、手順2としてGLOBAL FWから`B077T` modelを同一接続で取得できることをread-onlyで確認します。

```sh
python3 -m tools.macos_ota \
  --firmware firmware/original/B077T_US_13.bin \
  --probe-vendor-model
```

`Vendor OTA model: B077T_US_13`が確認できた後だけ、手順3としてJP_LANGを書き込みます。

固定JP LANG版の例です。

```sh
python3 -m tools.macos_ota \
  --firmware firmware/patched/B077T_US_13_JP_LANG.bin \
  --execute \
  --probe-vendor-model \
  --confirm-sha256 3c096e6498332d677cb0e4a0c541e6630f8955b0a21e97388b776674883f5246
```

自分のTOML設定から生成したFWは、原本と同じ設定を必ず一緒に渡します。CLIがメモリ上で再生成した結果と対象ファイルが完全一致するときだけ送信候補になります。

```sh
python3 -m tools.macos_ota \
  --firmware firmware/patched/B077T_US_13_MY_LAYOUT.bin \
  --base-firmware firmware/original/B077T_US_13.bin \
  --remap-config configs/my-layout.toml \
  --execute \
  --probe-vendor-model \
  --confirm-sha256 <生成時に表示されたSHA-256>
```

GLOBAL復旧は、固定GLOBAL原本だけを許す別CLIです。

```sh
python3 -m tools.macos_recover \
  --firmware firmware/original/B077T_US_13.bin \
  --execute \
  --confirm-sha256 00c87d252b639165963cc4452600672305043696d5fec7837b34b3dbed66957f
```

## 安全性と制限

- macOS OTA書込みとGLOBAL復旧CLIは実装済みですが、実機での正常書込み・復旧は未検証です。
- OTA中断や完全brick後にBLE広告が出ない場合、このツールだけでは復旧できません。
- vendor提供のfirmwareおよびtoolは再配布しません。
- 実機へ書込む前に、まずGLOBALへの書込みと復旧を検証してください。必要なら`--device-uuid`はscan対象の絞込みにだけ使えます。

このツールは安全策を持ちますが、firmware更新のリスクをなくすものではありません。
