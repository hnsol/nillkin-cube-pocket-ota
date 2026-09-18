# Nillkin Cube Pocket OTA preflight

macOSでBLE接続情報とOTA firmware-infoを確認する、実験的なread-onlyツールです。

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

現時点の対応範囲は、BLE scan、GATT情報のread、およびread-only preflightのみです。
実機probeは、接続先と応答を確認できる状態でのみ実行してください。
この統合CLIは物理キーボードに対してまだ再実行していません。過去のPhase 1 raw観測はありますが、live preflightとB077T gateは未検証です。

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

## 安全性と制限

- firmware flash、復旧、初期化、再開処理は未実装です。
- 端末のbrickからの復旧は保証しません。
- vendor提供のfirmwareおよびtoolは再配布しません。
- vendor OTA model queryは未検証のため送信せず、B077T向けの将来のwrite gateは満たしません。
- Phase 1はnotificationを購読しません。CoreBluetoothのnotification discriminator helperは、将来のnotify型OTA ACK処理専用です。

このリポジトリは、状態変更を伴わない調査用の基盤です。flashやrecovery用途には使用しないでください。
