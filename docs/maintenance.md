# 運用・保守ノート

半年後に作業を再開するとき、忘れると判断を誤りやすい情報だけをまとめています。
通常の生成・書込みコマンドは[`README.md`](../README.md)、通信仕様は
[`protocol.md`](protocol.md)を参照してください。

## 確認済みの実機と完成状態

- Nillkin Cube Pocket Foldable Bluetooth Keyboard with Touchpad
- Model: NKF01
- GATT model / revision: `PAR2801` / `1.0.0`
- BLE名はスロットにより`Cube Pocket Keyboard 1` / `2` / `3`
- 確認環境: macOS / CoreBluetooth / Python 3.14.4
- JP_LANGを書込み、再起動後`1.0.1 / 0xEC29`と全6キーの動作を確認済み

確認済みの配列:

```text
Caps      -> Ctrl
左Ctrl    -> Option
左Option  -> Cmd
左Cmd     -> 英数 (LANG2)
右Cmd     -> かな (LANG1)
右Option  -> Cmd
```

## 基準値

| 状態 | OTA version | OTA checksum |
| --- | --- | --- |
| 工場出荷FW | `1.0` | `0x6162` |
| 配布GLOBAL | `1.0.1` | `0xEC27` |
| JP_LANG | `1.0.1` | `0xEC29` |

| ファイル | SHA-256 |
| --- | --- |
| `B077T_US_13.bin` | `00c87d252b639165963cc4452600672305043696d5fec7837b34b3dbed66957f` |
| `B077T_US_13_JP_LANG.bin` | `3c096e6498332d677cb0e4a0c541e6630f8955b0a21e97388b776674883f5246` |

工場出荷FWの`0x6162`と、配布GLOBAL全体のsum16 `0xEC27`は一致しません。
checksumの対象範囲が同一と確認できていないため、工場出荷FWが別ビルドであることを
理由として断定しないでください。

## 再開時の基本原則

1. 原本FWを変更せず保持し、キーボードを十分に充電する。
2. 最初に`tools.macos_ota`を`--execute`なしで実行し、version/checksumを確認する。
3. 書込み時は、生成時に表示された64桁のSHA-256を省略せず指定する。
4. エラー後はすぐ再送せず、まず`tools.macos_recover --inspect-state`で状態を見る。

CoreBluetooth UUIDはMac固有なので本人性の判定には使いません。Vendor model query
`0x2A` / `0x2B`もこの実機では安定しません。代わりに、実機情報と`0x23`のraw応答を
完全一致させるfactory / installed-GLOBAL signature gateを使用しています。

## 最終ACK timeoutは失敗確定ではない

全payload送信後に`ACK 0x18 timed out`となっても、device側では更新が完了している場合があります。
実際のJP_LANG更新ではhostが成功ACKを受信できませんでしたが、電源再投入後に
`1.0.1 / 0xEC29`で起動しました。

この場合:

1. FWを再送しない。
2. CLIがtimeoutで停止したことを確認する。
3. キーボードの電源を入れ直す。
4. `--execute`なしのread-only preflightでversion/checksumを確認する。

`0xEC27`ならGLOBAL、`0xEC29`ならJP_LANGが起動しています。

## リマップ済みFWからの再書込み

JP_LANGや、TOMLから生成したFWが一度でも稼働すると、`0x23`はそのイメージのchecksum（例:
JP_LANGなら`0xEC29`）を報告するようになり、`--accept-installed-global-signature`は
一致しなくなります（このgateは`0xEC27`固定のGLOBAL fingerprintだけを見ています）。
この状態から別のイメージへ書き込むには`--accept-installed-remap-signature`を使います。

- 既定では、導入済みFWを固定JP_LANG（`1.0.1 / 0xEC29`）と仮定します。
- 独自TOMLで生成したFWが稼働している場合は、同じTOMLを`--installed-remap-config`で
  渡します。期待checksumはGLOBALのsum16 `0xEC27`に、そのTOMLによる`(new-old)`差分の
  合計をmod `0x10000`で加えた値です。
- 許可される書込み先は、固定GLOBAL、固定JP_LANG、TOMLから再生成した設定FWのいずれかです。
- 渡したTOMLがGLOBALと同一構成（sum == `0xEC27`）の場合は拒否されます。その場合は
  `--accept-installed-global-signature`を使ってください。
- **書き込んだTOMLは必ず保管してください。** 保管していないと、稼働中のFWをこのgateで
  認識できません。
- 固定JP_LANGでのJP_LANG → GLOBAL → JP_LANG往復を実機検証済み。独自TOMLの再書込みは未検証。

## checkpointの読み方

`tools.macos_recover --inspect-state`はFW本体を書き込まず、保持中のcheckpointを表示します。

- `offset=0 / checksum=0x0000`: 永続化済みcheckpointは先頭
- `GLOBAL prefix match: yes`: 表示offsetまで指定GLOBALと一致
- `GLOBAL prefix match: no`: GLOBALを前提に復旧を実行しない

実機では`offset=30 / checksum=0x24EC`をGLOBAL、`offset=30 / checksum=0x24EE`を
JP_LANGの転送中に観測しました。これは観測例であり、固定の成功判定値ではありません。

## 復旧機能の範囲

`tools.macos_recover`の実行機能は、工場出荷FWからGLOBALへの初回転送が中断した場合の
再開用です。正常起動中のJP_LANGからGLOBALへ戻す一般的なdowngrade機能ではありません。
`--accept-factory-signature`を回避策として流用しないでください。

BLE広告が出ない完全brick状態は、このツールだけでは照会・復旧できません。

## 追加リマップで忘れないこと

- `configs/jp-lang.toml`をコピーして変更する。
- 確認済みGLOBAL原本と同じ設定から、対象FWを毎回再生成する。
- 指定していない物理キーは変更されない。
- 生成後の差分、sum16、SHA-256を保存する。
- 書き込んだTOMLを保管する。`--accept-installed-remap-signature`で稼働中FWを認識するのに必要。
- vendor firmwareとWindows toolはリポジトリに含まれない。

## 通信上の重要な前提

- deviceが報告する`mtu_size=244`はOTAの論理ブロック長で、BLEの物理MTUではない。
- macOSでは物理WNR fragment 44 bytes、CoreBluetooth pacing 10msで実機確認した。
- Steam DeckではATT MTU 23、WNR payload limit 20 bytesを確認したが、最終OTAはmacOSで実施した。
- 別個体、別hardware revision、別の配布FWでは未検証。
