# OTA protocolの根拠

実装は、根拠を確認済みのread-onlyコマンドだけをallowlistに含めます。ローカルで解析したOTAUtilityの挙動では、`10 00`を送信してから`ff01`をreadし、続けて`23 00`を送信してから`ff01`をreadします。UpdateFwInfoは応答のbytes 4..8をNUL以外のASCIIとしてversionに、`response[9] | response[10] << 8`をchecksumに読みます。unit testは、この復元済みの挙動を固定するものであり、根拠そのものではありません。

| Opcode | Request | Write mode | Response layout | 根拠 |
| --- | --- | --- | --- | --- |
| `0x10` | `10 00` | with response | 4 bytes: `0e`, length, echoed opcode, status `00` | 解析済みOTAUtility挙動。unit testはその挙動を固定 |
| `0x23` | `23 00` | with response | 11 bytes: `0e`, length, echoed opcode, status `00`, version、checksum | 解析済みOTAUtility挙動。unit testはその挙動を固定 |
| `0x2A` | 未確認 | 未確認 | 未確認 | request、write mode、response layout、read-only性の根拠が不足 |
| `0x2B` | 未確認 | 未確認 | 未確認 | request、write mode、response layout、read-only性の根拠が不足 |

応答は、先頭byte、payload長、echoされたopcode、statusを検証します。
確認済みraw応答は、`0x10`: `0e 02 10 00`、`0x23`: `0e 09 23 00 31 2e 30 00 00 62 61`です。後者はversion `1.0`、checksum `0x6162`として解析されます。このchecksumはimageのsum16と同一とは扱いません。
`0x2A`と`0x2B`は実装・送信しません。model queryの根拠が揃うまで、B077T向けのwrite gateは未達です。

firmware転送、finalization、reset、recoveryに関するcommandは、いずれも未実装です。
