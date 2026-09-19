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
WNR上限はそれぞれ20/47 bytesです。deviceのflash書込み境界に合わせ、物理断片は
各WNR上限と論理ブロック長の小さい方以下で最大の4-byte倍数（20/44 bytes）にします。
上限が4 bytes未満なら送信せず停止します。244-byte論理ブロックをこのサイズへ分割し、
各断片でCoreBluetoothの送信可能状態と10ms pacingを確認します。PRN threshold 16は
物理write 16回ではなく論理ブロック16個を表します。ただし物理分割時は、OSの送信queueと
deviceのACK timingがhostの3904 bytes dispatch境界に一致しない可能性があるため、中間PRNでは
待機せず4096-byte object末尾のchecksum ACKを採用します。一致しない早期ACKは記録して読み飛ばし、
末尾checksumと一致するACKがtimeoutまでに来なければupgrade/resetせず停止します。
全payload転送後の`0x18` upgrade ACKは専用の30秒deadlineで待ちます。timeout時は
finalization結果が不明なため`0x22` resetも再送も行いません。FWを再送せず、手動で電源を
入れ直した後、通常のread-only preflightで現在のOTA version/checksumを確認してください。

工場出荷FWから固定GLOBAL原本を書き込む場合は、`--accept-factory-signature`で限定fallbackを許可できます。advertised name `Cube Pocket Keyboard 3`、GATT model `PAR2801`、revision `1.0.0`、必須`ff00`/`ff01`/`ff02`/`ff03`、`0x23` raw応答 `0e 09 23 00 31 2e 30 00 00 62 61`がすべて完全一致した場合だけです。checksum `0x6162`だけでは識別しません。JP_LANG、設定生成FWには使えません。初回GLOBAL転送が中断した場合の`tools.macos_recover`でも、同じ完全一致条件と固定GLOBAL原本に限って使用します。

手順1: 工場出荷FWから固定GLOBALへ更新します。コマンドを示すだけで、自動実行はしません。

```sh
python3 -m tools.macos_ota \
  --firmware firmware/original/B077T_US_13.bin \
  --execute \
  --accept-factory-signature \
  --confirm-sha256 00c87d252b639165963cc4452600672305043696d5fec7837b34b3dbed66957f
```

GLOBAL導入後は`0x2A`がタイムアウトするため、`--accept-installed-global-signature`で`0x2A`/`0x2B`を送らずにJP_LANGへ進めます。同一接続でadvertised name `Cube Pocket Keyboard 1`/`2`/`3`のいずれか、GATT model `PAR2801`、revision `1.0.0`、必須`ff00`/`ff01`/`ff02`/`ff03`、`0x23` raw応答 `0e 09 23 00 31 2e 30 2e 31 27 ec`がすべて完全一致した場合だけです。対象は固定JP_LANG、またはGLOBAL原本とTOMLから完全再生成できる設定FWに限ります。GLOBALには使えず、`--probe-vendor-model`、`--accept-factory-signature`とも併用できません。

手順2としてJP_LANGを書き込みます。

固定JP LANG版の例です。

```sh
python3 -m tools.macos_ota \
  --firmware firmware/patched/B077T_US_13_JP_LANG.bin \
  --execute \
  --accept-installed-global-signature \
  --confirm-sha256 3c096e6498332d677cb0e4a0c541e6630f8955b0a21e97388b776674883f5246
```

自分のTOML設定から生成したFWは、原本と同じ設定を必ず一緒に渡します。CLIがメモリ上で再生成した結果と対象ファイルが完全一致するときだけ送信候補になります。

```sh
python3 -m tools.macos_ota \
  --firmware firmware/patched/B077T_US_13_MY_LAYOUT.bin \
  --base-firmware firmware/original/B077T_US_13.bin \
  --remap-config configs/my-layout.toml \
  --execute \
  --accept-installed-global-signature \
  --confirm-sha256 <生成時に表示されたSHA-256>
```

GLOBAL復旧は、固定GLOBAL原本だけを許す別CLIです。

中断後の保持状態は、FWを書き込まずに`0x27`状態照会だけで確認できます。

```sh
python3 -m tools.macos_recover \
  --firmware firmware/original/B077T_US_13.bin \
  --inspect-state
```

表示される`GLOBAL prefix match`は、実機のoffset/checksumが指定GLOBALの
同じprefixと一致するかを示します。このモードは`0x28`、FWデータ、`0x18`、
`0x22`を送信しません。

復旧を実行する場合はvendor new-flowどおり、`ff02`へ`0x28`を送信してから
`ff01`へ`0x27`を送信します。`0x28`は状態消去ではなく、deviceを永続化済みcheckpointへ
巻き戻す操作です。返されたoffsetまでのGLOBAL prefix checksumが一致する場合だけ、
そのobjectから残りを転送します。offset 0も同じ判定に含まれます。不一致ならpayload、
`0x18`、`0x22`を送信せず停止します。

```sh
python3 -m tools.macos_recover \
  --firmware firmware/original/B077T_US_13.bin \
  --execute \
  --accept-factory-signature \
  --confirm-sha256 00c87d252b639165963cc4452600672305043696d5fec7837b34b3dbed66957f
```

## 安全性と制限

- GLOBAL書込み・復旧は実機で完了確認済みです（再起動後`1.0.1` / `0xEC27`）。JP_LANG書込みも実機で確認済みです（再起動後`1.0.1` / `0xEC29`）。JP_LANG確認時は`0x18` ACKをhostが受信できず待機が継続しましたが、手動電源再投入後のread-only照会で`0xEC29`を確認しました。
- OTA中断や完全brick後にBLE広告が出ない場合、このツールだけでは復旧できません。
- このツールは完全brickからの復旧を保証しません。
- vendor提供のfirmwareおよびtoolは再配布しません。
- 実機へ書込む前に、まずGLOBALへの書込みと復旧を検証してください。必要なら`--device-uuid`はscan対象の絞込みにだけ使えます。

このツールは安全策を持ちますが、firmware更新のリスクをなくすものではありません。
