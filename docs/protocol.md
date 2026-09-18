# OTA protocolの根拠

実装は、根拠を確認済みのread-onlyコマンドだけをallowlistに含めます。`ff01`へ各requestをwrite-with-responseで送信し、応答をreadします。UpdateFwInfoは応答のbytes 4..8をNUL以外のASCIIとしてversionに、`response[9] | response[10] << 8`をchecksumに読みます。unit testは、この復元済みの挙動を固定するものであり、根拠そのものではありません。

| Opcode | Request | Write mode | Response layout | 根拠 |
| --- | --- | --- | --- | --- |
| `0x10` | `10 00` | with response | 4 bytes: `0e`, length, echoed opcode, status `00` | 解析済みOTAUtility挙動。unit testはその挙動を固定 |
| `0x23` | `23 00` | with response | 11 bytes: `0e`, length, echoed opcode, status `00`, version、checksum | 解析済みOTAUtility挙動。unit testはその挙動を固定 |
| `0x2A` | `2a 00` | with response | 5 bytes: `0e 03 2a 00 <count>` | GLOBAL FW handler `0x1000df48..0x1000e00e`とresponse builder `0x1000a7ec` |
| `0x2B` | `2b 00 00 00 00`（index 0） | with response | 25 bytes: `0e 17 2b 00 ...`、model名はoffset 6から最大12 bytes | GLOBAL FW handler `0x1000df48..0x1000e00e`とresponse builder `0x1000a7c8` |

応答は、先頭byte、payload長、echoされたopcode、statusを検証します。
確認済みraw応答は、`0x10`: `0e 02 10 00`、`0x23`: `0e 09 23 00 31 2e 30 00 00 62 61`です。後者はversion `1.0`、checksum `0x6162`として解析されます。このchecksumはimageのsum16と同一とは扱いません。
`0x2A`のcountが1以上の場合だけ`0x2B`を送り、model名をNUL終端ASCIIとして読みます。配布GLOBAL FW内のidentity文字列は12-byte NUL paddedの`B077T_US_13`です。ASCII不正、空、`B077T`で始まらない値はfail-closedにします。PixArt fwupd一次資料もmodel名をresponse offset 6から12 bytes読みますが、fwupdは別transportであり、このBLE手順そのものの根拠ではありません。

## macOS CLI

通常はread-onlyです。`tools.macos_ota`は`--execute`なしではPhase 1情報を取得するだけで、
FWを送信しません。実行時は固定GLOBAL/JP_LANG imageのSHA-256を
`--confirm-sha256`へ完全一致で渡す必要があります。設定生成imageは、
`--base-firmware`と`--remap-config`を同時に渡し、GLOBAL原本と同じTOMLからメモリ上で
再生成したbytesが対象と完全一致する場合だけ送信候補にします。これは固定allowlistを
緩めるものではありません。

`--execute`ではscan後の**同一BLE接続**でGATT情報、`0x23`、`0x2B`を再取得します。
advertised name、`PAR2801`、`B077T` model、必須GATT構成、image hashがすべて通過した時だけ
`GattOtaEngine`へ送信を委譲します。CoreBluetooth UUIDは`--device-uuid`でscan対象を絞る用途だけであり、
本人性の判定には使いません。

`tools.macos_recover`は固定GLOBAL imageだけを許す薄い復旧CLIです。同じ確認と
`--execute --confirm-sha256 <GLOBALのhash>`を必要とします。BLE広告が失われた完全brick状態は
このCLIでは復旧できません。

## 静的転送計画

`python -m tools.macos_ota --firmware <approved.bin> --show-transfer-plan` は、
固定allowlistまたは原本＋TOMLから完全再生成できるimageを検証し、Bleakをimport・scan・connect・writeせずに既知の
wire operationだけを表示します。これは実行器ではありません。

Windows OTAUtilityの解析で確認したnew flowのoperationは、`0x27` init-new、
`0x25` object-create、raw payload、`0x17` PRN ACK、`0x18` upgrade、`0x22` resetです。
`0x27`、`0x25`、`0x18`はhostからwith-responseで送信し、`0x25` object ACKと
`0x18` upgrade ACKはdeviceからのnotifyを待ちます。`0x17` PRN ACKもdeviceからの
notifyです。raw payloadと`0x22` resetだけはhostからwithout-responseで送信します。
ただしobject size、payload chunk size、PRN間隔、resume位置は実機の`0x27`応答で
決まります。`GattOtaEngine`はその応答を検証してから送信します。`0x18` versionは
OTAUtility設定から確認した`1.0.1`、retransmitは`ff02`の`0x28`を使います。
`--show-transfer-plan`は引き続き表示専用で、実機へは何も送信しません。
