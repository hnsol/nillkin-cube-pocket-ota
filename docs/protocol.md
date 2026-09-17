# OTA protocolの根拠

## 実装済みのread-only範囲

protocol allowlistには、frame全体を確認済みの2コマンドだけを含める。

| Opcode | Request | Write mode | Response |
| --- | --- | --- | --- |
| `0x10` | `10 00` | with response | 4-byte vendor envelope |
| `0x23` | `23 00` | with response | 11-byte firmware-info envelope |

応答は`0e`、payload長、echoされたopcode、status `00`からなるenvelopeを検証する。
firmware転送コマンドはallowlistに含めない。

## Model queryの根拠gate

同梱`CmdToolSet.dll`のstatic stringsから、enum名
`CMD_FW_OTA_GET_NUM_OF_MODEL`と`CMD_FW_OTA_GET_MODEL`を確認した。task入力では、
それぞれ`0x2A`と`0x2B`に対応する。`OTAUtility.exe`のstatic stringsからは、
model queryのcall siteを確認できなかった。

この環境には.NET runtimeだけがあり.NET SDKがないため、必須の
`dotnet tool run ilspycmd`を実行できなかった。したがって、次の項目は同一の
vendor call chainから確認できていない。

| 必須根拠 | `0x2A` | `0x2B` |
| --- | --- | --- |
| methodとIL offset | 未確認 | 未確認 |
| 正確なrequest byte列 | 未確認 | 未確認 |
| `GattWriteOption` | 未確認 | 未確認 |
| response length/opcode/status | 未確認 | 未確認 |
| model文字列offset | 該当有無を含め未確認 | 未確認 |
| read-only性 | 未確認 | 未確認 |

このため、`0x2A`と`0x2B`は`READ_ONLY_COMMANDS`へ追加しない。model catalogと
response parserも実装せず、Phase 1はvendor OTA model identityを`unavailable`と
報告する。標準GATT Device Information Model Number（`2A24`）は別情報として表示し、
vendor model gateの代替にはしない。

互換.NET SDKとlocal `ilspycmd` tool manifestを用意した後、次のread-only抽出を
再実行する。

```sh
strings -a vendor/Nillkin\ Cube\ Pocket/*/CmdToolSet.dll \
  | rg 'GET_(NUM_OF_)?MODEL|MODEL'
strings -a vendor/Nillkin\ Cube\ Pocket/*/OTAUtility.exe \
  | rg 'Get.*Model|MODEL|2B|2A'
dotnet tool run ilspycmd -- -il \
  vendor/Nillkin\ Cube\ Pocket/*/CmdToolSet.dll
dotnet tool run ilspycmd -- -il \
  vendor/Nillkin\ Cube\ Pocket/*/OTAUtility.exe
```

表の全項目を確認し、proprietary IL本文をrepositoryへ転載せずに記録するまで、
いずれのコマンドも実機へ送信しない。
