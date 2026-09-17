# OTA protocolの根拠

実装は、根拠を確認済みのread-onlyコマンドだけをallowlistに含めます。

| Opcode | Request | Write mode | Response layout | 根拠 |
| --- | --- | --- | --- | --- |
| `0x10` | `10 00` | with response | 4 bytes: `0e`, length, echoed opcode, status `00` | 実装したframe定義とunit testで検証済み |
| `0x23` | `23 00` | with response | 11 bytes: `0e`, length, echoed opcode, status `00`, version、checksum | 実装したframe定義とunit testで検証済み |
| `0x2A` | 未確認 | 未確認 | 未確認 | request、write mode、response layout、read-only性の根拠が不足 |
| `0x2B` | 未確認 | 未確認 | 未確認 | request、write mode、response layout、read-only性の根拠が不足 |

応答は、先頭byte、payload長、echoされたopcode、statusを検証します。
`0x2A`と`0x2B`は実装・送信しません。model queryの根拠が揃うまで、B077T向けのwrite gateは未達です。

firmware転送、finalization、reset、recoveryに関するcommandは、いずれも未実装です。
