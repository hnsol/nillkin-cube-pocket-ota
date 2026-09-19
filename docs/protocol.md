# OTA protocolの根拠

実装は、根拠を確認済みのread-onlyコマンドだけをallowlistに含めます。`ff01`へ各requestをwrite-with-responseで送信し、応答をreadします。UpdateFwInfoは応答のbytes 4..8をNUL以外のASCIIとしてversionに、`response[9] | response[10] << 8`をchecksumに読みます。unit testは、この復元済みの挙動を固定するものであり、根拠そのものではありません。

| Opcode | Request | Write mode | Response layout | 根拠 |
| --- | --- | --- | --- | --- |
| `0x10` | `10 00` | with response | 4 bytes: `0e`, length, echoed opcode, status `00` | 解析済みOTAUtility挙動。unit testはその挙動を固定 |
| `0x23` | `23 00` | with response | 11 bytes: `0e`, length, echoed opcode, status `00`, version、checksum | 解析済みOTAUtility挙動。unit testはその挙動を固定 |

応答は、先頭byte、payload長、echoされたopcode、statusを検証します。
確認済みraw応答は、`0x10`: `0e 02 10 00`、`0x23`: `0e 09 23 00 31 2e 30 00 00 62 61`です。後者はversion `1.0`、checksum `0x6162`として解析されます。このchecksumはimageのsum16と同一とは扱いません。
`0x2A`/`0x2B`は配布GLOBAL FWの解析ではmodel照会として確認できるものの、工場出荷FWでは`0x2A`がタイムアウトしました。通常のmacOS preflightはこの2 opcodeを送らず、`0x10`と`0x23`だけに限定します。従ってvendor OTA modelはunavailableとして扱い、`B077T` modelを必須とするwrite gateはfail-closedのままです。`--probe-vendor-model`を明示した場合だけ、同一接続上の通常sequence完了後に`0x2A`→`0x2B`を追加します。工場出荷FWではタイムアウトし得ます。

## macOS CLI

通常はread-onlyです。`tools.macos_ota`は`--execute`なしではPhase 1情報を取得するだけで、
FWを送信しません。実行時は固定GLOBAL/JP_LANG imageのSHA-256を
`--confirm-sha256`へ完全一致で渡す必要があります。設定生成imageは、
`--base-firmware`と`--remap-config`を同時に渡し、GLOBAL原本と同じTOMLからメモリ上で
再生成したbytesが対象と完全一致する場合だけ送信候補にします。これは固定allowlistを
緩めるものではありません。

`--execute`ではscan後の**同一BLE接続**でGATT情報、`0x10`、`0x23`を再取得します。
通常のwrite gateはadvertised name、`PAR2801`、`B077T` model、必須GATT構成、image hashが
すべて通過した時だけ`GattOtaEngine`へ送信を委譲します。JP_LANGと設定生成imageは、
`--probe-vendor-model`でその同一接続上から`B077T` modelを取得できた場合だけ許可します。

工場出荷FWから固定GLOBALへの初回更新だけは、`--accept-factory-signature`で限定fallbackを
明示できます。advertised name `Cube Pocket Keyboard 3`、GATT model `PAR2801`、revision
`1.0.0`、必須`ff00`/`ff01`/`ff02`/`ff03`、`0x23` raw応答
`0e 09 23 00 31 2e 30 00 00 62 61`、固定GLOBALの既知hash・埋込`B077T_US_13`・
`PAR2801`がすべて一致した場合だけです。`0x6162`単体はmodel識別に使いません。
authorizationにはGLOBALに埋め込まれた`B077T_US_13`を渡しますが、report上のdevice-reported
Vendor OTA modelは`unavailable`のままとし、factory signature一致を別fieldで表示します。
このfallbackはJP_LANG、設定生成image、復旧には使えず、`--probe-vendor-model`とも併用できません。
明示SHA確認も通常どおり必須です。

実施順は、factory signatureでGLOBALのみを書込み、再起動・再接続後に
`--probe-vendor-model`で`B077T`を確認し、その後にJP_LANGを書き込む順です。
CLIはこの連続手順を自動実行しません。CoreBluetooth UUIDは`--device-uuid`でscan対象を
絞る用途だけであり、本人性の判定には使いません。

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
BleakのCoreBluetooth without-response writeはnative送信queueへの投入後すぐreturnし、
queueの空きを保証しません。そのため各raw payloadとresetのwrite直前に
`canSendWriteWithoutResponse`を約1ms間隔、operation timeout内でpollし、trueの時だけ送信します。
APIの例外、不正値、待機中の切断、timeoutはfail-closedです。OTAUtilityに合わせ、raw payloadは
各物理write後に2ms待機します。plannerは`0x17` ACKをPRN threshold数の
論理ブロック送信後、またはobject末尾（firmware末尾を含む）に配置します。3 bytes ACKの
checksumはbytes 1..2、4 bytes ACKではbytes 2..3のlittle-endianです。
FWは`0x25` ACKを4 bytesで通知しますが、OTAUtilityは先頭の`0x25`だけを検査します。
そのため実装も残りのopaque bytesを解釈しません。各object-createは、最終objectでも
`0x27`で得たmax object size（配布FWは4096を広告）を宣言し、実際に送るpayloadだけを
残りのbytesにします。
ただしobject size、deviceの論理ブロック長、PRN間隔、resume位置は実機の`0x27`応答で
決まります。`GattOtaEngine`はその応答を検証してから送信します。`0x18` versionは
OTAUtility設定から確認した`1.0.1`、retransmitは`ff02`の`0x28`を使います。
`0x27`の`mtu_size=244`はBLEの物理MTUではなく、OTA上の論理payload block上限です。
plannerは最大244 bytesの論理ブロックを生成し、PRN thresholdをその個数で数えます。
PRN 16、object 4096 bytesでは論理上のACK境界は3904 bytes（244×16）後とobject末尾の
4096 bytes後です。running sum16もこの2境界に一致させます。

GATT engineは各論理ブロックをCoreBluetoothのWNR上限（host MTU - 3）以下の物理断片へ
分割します。実測はSteam DeckでMTU 23／最大20 bytes、macOSでMTU 50／最大47 bytesです。
raw payloadはWNRのみで送信し、各物理断片ごとにreadiness確認、dispatch counter更新、
2ms pacingを行います。物理断片サイズが論理ブロック長より小さい場合、OSの送信queueとdeviceの
ACK timingがhostの中間PRN dispatch境界に一致しない可能性があるため、中間PRNでは待機せず、
object末尾でrunning sum16に一致する`0x17`を待ちます。有効な形式でもchecksumが一致しない早期ACKは
記録して読み飛ばし、timeout時の診断に最後の不一致を含めます。別opcodeまたは不正形式は即時停止し、
一致ACKが得られなければupgrade/resetしません。物理分割しない場合は従来どおり各PRN境界で待機します。
payload dispatch順も通知に記録し、boundaryより前のwriteに対応する一致ACKは採用しません。
host上限を取得できない場合や不正な場合はobject-create前に停止します。
診断用に`0x27`のoffset/checksum/max object size/論理ブロック長/PRN threshold、host WNR上限、
物理断片サイズをengine上に保持し、payload送信または`0x17` ACK待機の失敗時はCLIエラーにも
object/論理payload位置・長さとWNR readinessのfalse観測回数・累積待機時間を含めます。
244-byte単位のWRは実機でACKを得られなかったため、WR経路と実験オプションはありません。
`--show-transfer-plan`は引き続き表示専用で、実機へは何も送信しません。
