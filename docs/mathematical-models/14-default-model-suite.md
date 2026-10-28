# 14. 新規Scenarioの標準モデルセット

## 標準IDと状態

新規の未選択Scenarioに `policy_registry.schema_version=3` と `suite_id=core-model-suite-v2` を記録する。Policy Registryのselectorとsuiteの両方をRun署名へ含める。suite v1は旧utility selectorを使う固定モデルとして保持し、v2では7日Runで判明した睡眠負債の過小評価に対して新しいsleep-aware behavior selectorを導入した。v2の7日同seed Runはv1よりstress平均を0.9347から0.9019へ、sleep debtを1,438.6分から981.3分へ改善した一方、終了時の高stress率94%と最長episode 9,872分が残った。suite v2は内部候補であり、全合格ゲートを通過したモデルや実地検証済みモデルではない。新規Scenarioへの候補適用はこのfeature branch上の検証対象として扱い、Gate Dの条件行列、複数seedと残余stress原因の検証が完了するまで統合対象にしない。[13-comprehensive-validation.md](13-comprehensive-validation.md) に判定を記録する。

この段落の7日の数値は、#169 の不具合があったときの動作で計算した。suiteが個別の指定を省いた queue・relationship・誤情報の圧力に、宣言とは違うものが使われていた。#170 以降は宣言どおりのモデルを使い、署名は `model_suite: core-model-suite-v2@2` になる。同じ条件で実行し直したv2の7日の値は、終了stress平均0.9020、終了時の高stress率89%、sleep debt 942.3分、最長episode 9,894分である。前後の比較と、書き換えずに残した判断は [13-comprehensive-validation.md](13-comprehensive-validation.md) の冒頭の注記を参照（#171）。

| domain | selector | 主要な設定/有効化 |
|---|---|---|
| 行動 | `sleep-aware-utility-softmax-v2` | 15分間隔。睡眠負債を1日必要睡眠時間で割り、1日分の負債相当につき効用+1。必要時間未指定時は480分を仮定 |
| 心理 | `mean-reverting-life-state-v1` | 既存の自然回復とbody/life-state結合。急性/日単位状態を分離 |
| 社会 | `expressiveness-channel-openness-v1` | social propagation configが有効。家族/zone接触がなければ影響0 |
| 情報 | `evidence-aware-rumor-v1` | information spread configが有効。claim/contactがなければ影響0 |
| affect | `linear-positive-negative-v1` | body stateのaffect構成 |
| body dynamics | `body-feedback-complete-v2` | 完全feedback式と `ASM-SLEEP-DEBT-BALANCE-V1` の明示仮定 |
| queue | `need-weighted-physical-queue-v1` | urgency/継続/離脱を使う。混雑がなくとも強制waitを作らない |
| relationship | `distance-duration-repeat-trust-v1` | 宣言接触の距離/時間/反復を状態化 |
| social proximity | `disabled-proximity-v1` | 実座標のないScenarioへ位置を捏造しない。明示geometryで使う別Policyは引き続き選択可能 |

Model CatalogとScenario provenanceは、オペレータID、係数、source/assumption ID、モデル署名を記録する。`enabled`は機構の有効状態であり、選ばれたPolicy IDとは別である。physical proximity、message/contact、social claimが実際に無ければ、その伝播寄与はゼロである。

## 適用入口

- 初期Scenario生成（CLI `prepare` とProject/API経由）は新規defaultsとしてsuiteを作る。
- CLI `prepare` は未選択のScenario入力にsuiteを追加する。既存Policy Registry付き入力はそのまま。
- Web Demoのscenario editorは通常のutility-softmax presetを新規Scenarioとしてsuite化する。priority-ruleを明示選択した場合はsuiteで上書きしない。
- 直接読み込んで実行する既存Scenario、Run、Checkpointは自動で書き換えない。
- 比較・研究Scenarioはschema 1/2で旧Policyを指定できる。suiteとcore domain個別上書きの混用は拒否する。

## 固定・互換性

Run開始時のScenario canonical hash、model configs、model signaturesを固定する。suite/model IDの変更は別モデル版として扱い、suite v1の挙動をv2へ読み替えない。Checkpointは同じsuiteと係数でのみ復元する。既存Checkpointは保存されたselector/configに基づき復元し、suite IDを推測して追加しない。Checkpoint schema/body-state schemaは今回変更していない。

schema 3を受け付けるCLI/HTTP契約を更新した。Run間でsuiteを切り替える場合は新しいScenario/Runを作り、既存Branch stateへ無断適用しない。

## 完了条件

v2 behaviorの睡眠負債効用項は未校正の仮定である。現状のsuite v2は「最新の検証済み」と呼べない。少なくとも7日・複数seed・Checkpoint復元、Gate Dの同条件介入行列、残余stressの寄与分解を追加してGate A–Fの未完了項目を解消する必要がある。実測校正・現地妥当性は別プロジェクトで必要になる。
