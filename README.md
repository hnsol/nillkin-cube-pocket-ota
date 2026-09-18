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

通常実行はread-only preflightです。BLE scan、GATT情報、`0x23` firmware info、`0x2B` modelを取得します。FWは送信しません。
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

固定JP LANG版の例です。

```sh
python3 -m tools.macos_ota \
  --firmware firmware/patched/B077T_US_13_JP_LANG.bin \
  --execute \
  --confirm-sha256 3c096e6498332d677cb0e4a0c541e6630f8955b0a21e97388b776674883f5246
```

自分のTOML設定から生成したFWは、原本と同じ設定を必ず一緒に渡します。CLIがメモリ上で再生成した結果と対象ファイルが完全一致するときだけ送信候補になります。

```sh
python3 -m tools.macos_ota \
  --firmware firmware/patched/B077T_US_13_MY_LAYOUT.bin \
  --base-firmware firmware/original/B077T_US_13.bin \
  --remap-config configs/my-layout.toml \
  --execute \
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
