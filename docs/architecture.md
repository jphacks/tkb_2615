# アーキテクチャ

## コンポーネントの流れ

~~~text
CLI / ローカルHTTPサーバー
        |
Project、Scenario、Inspector、Demoサービス
        |
Scenario + Policy設定 -> WorldState -> 決定的tick engine
        |                            |                |
地域・Provider adapter             |             EventQueue
任意LLM adapter                    |             EventLog
        |                            |
ProjectStore / TimelineStore <- Checkpoint / BranchRuntime
        |
Run成果物、履歴index、Inspector projection、比較レポート
~~~

Python標準ライブラリのHTTPサーバーがローカルブラウザーアプリを配信します。ブラウザーは型付きrequestを送信し、simulation stateを直接書き換えません。保存済みRunのInspectorはread-onlyです。Project操作は別の検証済みcommandを使います。

## モジュールの責務

| 領域 | 主な責務 |
|---|---|
| core | WorldState、simulation clock、keyed RNG、tick engine、Checkpoint |
| domain | 人物、施設、Zone、route、資源、職員、運営規則 |
| events | 予定・実行済みevent、型付きhandler、EffectProgram |
| policies | 行動、ニーズ、空間、心理、社会、情報モデル |
| experiments | Scenario、Run、Branch、timeline、metrics、evaluation |
| inspection | 証拠検索、Run projection、trace要約 |
| region | 地域データ、geometry検証、施設図面取込 |
| llm | Provider adapter、型付き出力、cache、応答再生、利用額記録 |
| storage | Project、asset、Run、Checkpoint、履歴indexの永続化 |
| web | ローカルHTTP route、Project API、Inspector、Demo、ブラウザーasset |

実装の正本はパッケージ構成です。公開入力・状態契約はsrc/sheltersim/contracts、event/world model、およびserializationと互換性を確認するテストにあります。

## シミュレーションと状態境界

- 時刻は整数分です。event順と同順位の決着は、固定IDと明示的なsort keyで決めます。
- WorldStateが変更可能なsimulation stateの正本です。Policyとadapterは型付き提案を作り、controllerが検証して適用します。
- 乱数はScenario seedと安定したdomain IDから決定します。simulation結果の判断にwall-clockやmodule-global乱数を使いません。
- EventQueueは今後処理する予定を、EventLogは結果を記録します。sourceやcausationを含む、相互に区別された記録です。
- TickEngineは毎分の終わりに、現在の状態と、その分に追記された履歴記録（直前の1件を含む）を検証します。追記専用の履歴（共有運用、生活状態の社会履歴、給水・配食の依頼と停止の履歴）とEventLogの全体の再生、共有運用の支援ライフサイクルの再構成は、Runの開始・終了とCheckpointの作成・復元で行う完全検証で確認します。毎分の検証が履歴全体を再走査しないため、1分あたりの検証時間は履歴の長さに比例して増えません。
- Checkpointは対応する復元に必要なstateとruntime情報を含みます。BranchRuntimeは分離したstateを復元し、兄弟Branchで可変queue、RNG状態、actor objectを共有しません。
- inspection、描画、metrics、trace levelはWorldStateを変えず、simulation乱数も消費しません。

ScenarioSpec、WorldState、Checkpoint、EffectProgram、Project recordはそれぞれ個別に版管理します。state変更には互換性テストが必要です。古い永続データを読み続ける場合は明示的な移行を用意します。

## 永続化とインターフェース

ProjectStoreとTimelineStoreは、ローカルProject、承認済みScenario、Run、Branch、Checkpoint chunk、asset、index付き証拠を保持します。CLI Run成果物にはmanifest、凍結Scenario、event記録、metrics、任意のモデル利用記録が含まれます。formatは有効化した機能によって異なります。APIリファレンスを参照してください。

HTTP APIは次の3領域を分けています。

- /api/v1/projects: 永続Projectと状態変更操作。
- /api/v1/runs: 保存Run成果物の上限付きread-only参照。
- /api/v1/demo: process内のデモBranch。

HTTPサーバーは既定で127.0.0.1にbindします。認証機能がないため、信頼できないネットワークへ公開してはいけません。

## 外部接続との境界

地域データProviderと任意Anthropic adapterはsimulation coreの外側にあります。Providerから得たデータは信頼せず、sourceと適用範囲を保持します。offline replayは保存済み応答か決定的fallbackを使い、過去時点の参照でlive Providerを呼びません。

モデル仮定と検証範囲はsimulation-models.mdとmodel-validation.mdを参照してください。
