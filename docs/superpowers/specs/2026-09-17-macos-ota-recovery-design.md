# macOS OTA・復旧ツール設計

## 対象範囲

BLE GATT OTAサービス経由でNillkin Cube Pocket NKF01を更新・復旧できる、
macOSネイティブのPythonツールを構築する。最初に対応するハードウェアおよび
ファームウェア系統はPAR2801 / B077Tとする。制御された実機での更新・復旧試験が
完了するまではproduction-readyを名乗らず、実験的なGitHubプロジェクトとして
公開可能な品質を目標とする。

実装はWindows、Wine、仮想マシン、Linux fwupdのHID転送方式に依存してはならない。
ベンダー提供ファームウェアおよびWindows実行ファイルは解析入力に限り、再配布しない。

## 確認済みのデバイスインターフェース

- デバイス名：`Cube Pocket Keyboard 1`、`2`、または`3`
- GATTモデル番号：`PAR2801`
- GATTファームウェアリビジョン：`1.0.0`
- OTAサービス：`ff00`
- OTAコマンド／データCharacteristic：`ff01`
- 追加Characteristic：`ff02`、`ff03`
- `ff01`で確認済みの情報取得コマンド：`10 00`および`23 00`
- `23 00`の実機raw frame：`0e 09 23 00 31 2e 30 00 00 62 61`
- OTAUtilityと同じ解析結果：OTA version `1.0`、OTA checksum `0x6162`

OTAUtilityの`UpdateFwInfo`は、応答byte `4..8`の非NUL文字をversion、
`response[9] | response[10] << 8`をchecksumとして扱う。GATTファームウェア
リビジョン`1.0.0`とは別フィールドとして保持する。

実機が報告するOTA checksum `0x6162`は、配布GLOBAL全ファイルのsum16
`0xEC27`と一致しない。工場出荷FWが別ビルドである可能性は高いが、両checksumの
対象範囲が完全に同一であることを確認するまでは理由を断定しない。

Linux fwupdのPixArt実装は、コマンドの意味やchecksum動作を理解するための概念的な
参考にしてよいが、HID Feature Report転送は再利用してはならない。このキーボードに
配布されるWindows OTAUtilityおよびCmdToolSetバイナリを、BLEフレーミング、状態遷移、
応答確認、再送、reset動作の正しい根拠とする。

## 構成要素

### `ota_protocol.py`

転送方式から独立した純粋なプロトコルモジュール。既知opcode、パケットencoder／decoder、
応答検証、checksum、転送状態、chunk順序、再送動作、終端の成功／失敗状態を定義する。
未知のopcodeおよび不正な応答は拒否する。

### `ble_transport.py`

探索、接続、GATT列挙、read、write、notify、timeout、切断検出だけを担当する小さな
Bleakアダプター。プロトコルパケットは受け入れるが、更新状態は判断しない。

### `firmware_image.py`

OTAで状態を変更する前にファームウェアを検証する。サイズ、SHA-256、sum16、埋め込みの
model/version文字列、キーマップマーカー、承認済みイメージprofileを確認する。初期profileは
検証済みGLOBAL原本とJP LANG版とする。JP LANG版は6キーに対応する6個の
16-bit keymap entryを変更する。各entryの下位byteのみが変化するため、実際の
binary diffは6 bytesとなる。任意のファームウェアはデフォルトで拒否する。

### `macos_ota.py`

更新用CLI。デフォルトはpreflight/dry-runモードとする。更新には明示的な実行flagと、対象
identityおよび厳密なファームウェアSHA-256を表示する対話確認を要求する。構造化された進捗を
表示し、サニタイズ済みのローカルlogを書き出す。

### `macos_recover.py`（最終段階）

正常なGLOBAL更新、JP LANG更新、GLOBAL復元を実機で確認した後に作成する、
検証済みGLOBALイメージ限定の復旧用CLI。通常のデバイス名と、プロトコル解析で確認された
OTAモードidentityを探索し、OTA GATTサービスに到達できるか検出する。OTAUtilityで実証された
動作に従う場合に限り、再開または再起動する。BLE OTAを公開しなくなったデバイスの復旧は保証しない。

## 実装段階

1. read-only probeを整備する。
2. `0x2A`（model数取得）と`0x2B`（model取得）のrequest/response形式を
   ベンダー実装から確定し、read-onlyであることを確認してから実機modelを取得する。
3. firmware parser/patcherを公開可能な構成へ整理する。
4. protocol simulatorとfake GATT転送を完成させる。
5. 別途明示許可を得て、検証済みGLOBALを実機へ正常flashする。
6. 別途明示許可を得て、JP LANG版を実機へflashする。
7. GLOBALへの復元を実機で確認する。
8. 実証済みの動作だけを使用してrecovery CLIを作成する。

## 更新フロー

1. 許可されたデバイス名を探索して接続する。
2. `PAR2801`、`ff00`、`ff01`、必要なCharacteristic propertyを検証する。
3. 端末固有のCoreBluetooth UUIDをdevice identityに使用せず、GATT情報とmodel queryで
   対象を識別する。
4. 確定した`0x2A`／`0x2B` queryで実機modelがB077T系であることを検証する。
5. ファームウェア情報と、利用可能な場合はバッテリー情報を読む。
6. 選択されたイメージを、承認済みで不変のprofileに対して検証する。
7. デバイスidentity、現在のversion/checksum、対象イメージhash、対象checksum、予定する
   プロトコル操作を表示する。
8. dry-runモードではここで停止する。
9. 明示的な実行モードでは、OTAUtilityから復元した正確な状態機械を実行する：初期化、転送準備、
   erase/object作成、chunk write、応答確認／再送処理、checksum検証、finalization、reset。
10. 再接続し、導入後のversion/checksumを検証する。

フレームレイアウト、期待応答、timeout、状態遷移がベンダー実装から確認されるまで、状態変更opcodeを
実装または有効化してはならない。

## 復旧動作

復旧には保持したGLOBALイメージのみを使用する。通常ファームウェアまたはOTA loaderが引き続き
advertiseし、確認済みのGATTサービスを公開している場合、ツールはベンダー定義の初期化および完全な
再flash手順を実行してよい。offsetからの再開は、OTAUtilityが明確に対応し、かつデバイスが信頼できる
offset/checksumを報告する場合に限り対応する。

BLE advertiseまたはOTAサービスが存在しない場合、ツールは停止し、ハードウェアプログラミングまたは
メーカーサービスが必要であることを説明する。根拠なくdual-bank、rollback、bootloaderの耐障害性を
仮定しない。

## 安全制御

- dry-runをデフォルトとし、状態変更writeを一切実行しない。
- read-onlyコマンドと、状態変更OTAコマンドを別々にallowlist化する。
- 実行には`0x2B`で確認したB077T系model、`PAR2801`、確認済みGATT構成、承認済み
  ファームウェアhashを要求する。model queryを確定または取得できない場合は書き込み前で停止する。
- CoreBluetooth UUIDはMac固有の一時的識別子として扱い、allowlist identityには使用しない。
- 既存のファームウェアファイルおよびlogを黙って上書きしない。
- 切断、timeout、不正ACK、checksum不一致、予期しないoffsetでは、推測せず転送を停止する。
- logからCoreBluetooth identifierおよびその他の端末固有情報を除外する。
- あいまいな結果の後、ツールはerase、reset、finalizationを自動再試行しない。
- 実機試験には別途明示的な許可が必要である。

## テスト

- unit testでは手作業で組み立てたパケットと、独立に導出した期待byteを使用する。
- golden testでOTAUtilityから復元した全frameを対象とする。
- golden dataにはpayloadだけでなく、各送信のwrite-with-response／
  write-without-response、期待ACK、timeout、次状態を含める。
- fake GATT転送で、通常転送、timeout、切断、不正応答、checksum失敗、再送、復旧中断経路を試験する。
- CoreBluetoothではread応答とnotifyが同じcallback経路を通るため、Bleak 3.xの
  `notification_discriminator`を使用する分離処理を試験する。
- ファームウェア検証testで、承認済みhashすべてと各拒否gateを対象とする。
- read-onlyの実機preflightで、macOS上の探索とGATT identityを確認する。
- 実際の更新および復旧testは別途文書化し、自動CIには含めない。

## 公開基準

リポジトリにはsource、test、プロトコル文書、サニタイズ済みのexample log、README、license、
型チェック、lint設定、CIを含めてよい。ベンダー提供ファームウェア、OTAUtility、CmdToolSet、
抽出したプロプライエタリコード、Bluetooth identifier、brickからの復旧を保証する主張を含めてはならない。

初回リリースはexperimental/alphaと表記する。production-readyの表記には、GLOBAL更新、JP LANG更新、
GLOBAL復元、定義された転送段階での中断処理、更新後checksum検証について、制御された試験の成功が必要である。

## 受け入れ基準

- プロトコル動作が推測ではなく、ベンダーツールの根拠まで追跡できる。
- dry-runでは状態変更BLE writeを実行しない。
- 実機modelをB077T系と確認できない場合、状態変更BLE writeへ進まない。
- updaterはデフォルトで、検証済みの2つのファームウェアイメージのみを受け入れる。
- 更新と復旧で、テスト済みの同一プロトコルengineを共有する。
- シミュレーションの成功・失敗testがすべて通る。
- 実機preflightが、ファームウェアを変更せずに実際のNKF01を検証する。
- 実際のOTA転送は、後日の明示的なユーザー許可なしには実行しない。
