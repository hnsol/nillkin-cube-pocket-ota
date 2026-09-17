# Nillkin Cube Pocket OTA preflight

macOSでBLE接続情報とOTA firmware-infoを確認する、実験的なread-onlyツールです。

## セットアップ

Python 3.14.4でvenvを作成します。

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

## 安全性と制限

- firmware flash、復旧、初期化、再開処理は未実装です。
- 端末のbrickからの復旧は保証しません。
- vendor提供のfirmwareおよびtoolは再配布しません。
- vendor OTA model queryは未検証のため送信せず、B077T向けの将来のwrite gateは満たしません。

このリポジトリは、状態変更を伴わない調査用の基盤です。flashやrecovery用途には使用しないでください。
