# 12. モデル改善設計

## 変更の範囲

変更は新規Scenarioの初期モデル選択、睡眠負債の次ステップ関数、および7日検証で観測した睡眠不足への行動選択接続に限定した。急性stressの心理係数、need relief量、在庫量を結果目標へ合わせて変更していない。旧Scenarioに明示されたPolicy Registryは読み替えない。

## 睡眠負債の収支

睡眠負債 (D_t) は分単位のbounded state ([0,1440])。1分tickで、活動 (a_t)、必要睡眠時間 (S_i)、睡眠効率 (eta_t) を用いる。


\[
D_{t+1}=\operatorname{clip}_{[0,1440]}\begin{cases}
D_t-r_s\eta_t,&a_t=\text{sleeping}\\
D_t-r_r\eta_t,&a_t=\text{resting}\\
D_t+r_a(S_i),&\text{otherwise}
\end{cases}
\]


個別必要睡眠時間がある場合は (0<S_i<1440) について (r_a(S_i)=S_i/(1440-S_i)) 分/覚醒分とする。必要時間どおり眠る理想日では、覚醒時間 ((1440-S_i)) による増加が (S_i) 分、睡眠中の回復が (S_i) 分となり、負債の一日差分は0になる。(S_i=0) は新たな覚醒負債を増やさない。未指定なら (r_a=0.5) を使う。これは480分睡眠・960分覚醒で均衡するシナリオ仮定である。睡眠回復 (r_s=1) と休息回復 (r_r=0) は別設定で、quiet restをsleepとして二重計上しない。zoneの睡眠効率は0..1に制限する。

この収支は必要睡眠の医学的推定でも、睡眠圧の臨床モデルでもない。Daanらの二過程枠組みは覚醒と睡眠で状態変化が異なる構造を支持するが、ShelterSimの係数は校正しない。長時間の睡眠時刻・途中覚醒・睡眠環境の個別推定は次の研究課題である。

## 新規Scenarioの行動設定

新規Scenarioのsoftmaxモデルを維持しつつ判断間隔を60分から15分へ変更する。15分は応答遅延を抑えるための内部ソフトウェア設定で、観察頻度の推定ではない。極端needで特定actionを決定論的に強制せず、適格性・効用・温度の現行Policyを使う。

normal scenarioのbehavior scaleと需要rateは未校正のまま保持する。水分needの増加とmlの関係、meal serviceの回数と空腹の関係を直接同一視しない。resource ledger上の物理量とAgentのnormalized needの間には、実測に基づく新しい換算がまだない。

## `core-model-suite-v1`（保存された初回候補）

Policy Registry schema 3の `suite_id` を追加し、以下のモデルIDを1つの再現可能な組み合わせとして署名する。

| domain | model ID | purpose |
|---|---|---|
| behavior | `utility-softmax-v1` | 適格行動間の確率選択 |
| psychology | `mean-reverting-life-state-v1` | 急性回復とlife-stateの統合 |
| social | `expressiveness-channel-openness-v1` | household/zone接触上のsigned affect influence |
| information | `evidence-aware-rumor-v1` | belief、sharing、訂正の分離 |
| affect | `linear-positive-negative-v1` | bounded affect transfer |
| body_dynamics | `body-feedback-complete-v2` | body feedback + sleep balance assumption |
| queue | `need-weighted-physical-queue-v1` | urgency、継続、離脱を持つ物理queue |
| relationship | `distance-duration-repeat-trust-v1` | 接触距離/持続/反復に基づく関係状態 |
| social_proximity | `disabled-proximity-v1` | geometryを推測しない。宣言されたhousehold/zone接触は別途有効 |

social propagation と information spread は新規デフォルトで有効にするが、接触、message、belief等の入力がない場合、その寄与は0である。接触機構のenabledフラグはmodel selectorから分離し、既存設定値を尊重する。distance samplingを使うには明示したfacility geometryと対応selectorが必要で、合成距離を勝手に作らない。

`InitialScenarioBuilder`、CLI `prepare`、Project/APIの生成経路、Demo scenario editorは新しい未選択Scenarioへsuiteを記録する。既存Policy Registry付きScenarioは変更しない。suiteは`model_signatures`・Checkpoint・run manifestに残る。schema 1/2の明示selectorと個別Policy選択は継続してサポートする。モデル版やsuite IDが変わると別署名として扱い、走行中Runのモデルは固定する。

## 根拠と互換性

Model Catalog、`fixtures/source-manifest.json`、Scenario provenanceにsuiteとsleep assumptionsを登録する。構造文献はそれぞれsoftmax choice、homeostatic sleep、allostatic burden、emotion contagion、misinformation correctionを説明する。文献は数値係数や避難所結果の校正根拠ではない。

Checkpoint schemaやbody state schemaは変更していない。Policy factory版はmodel signatureに含まれ、sleep係数は選択されたbody formula設定に含まれる。Suite署名は`model_suite`として追加し、Checkpoint互換性照合にも含める。Historical Scenarioにsuiteを後付けする移行は行わない。

## 変更しなかったもの

- 急性stress式・自然回復率、need-growth係数、water/food/toilet relief係数は維持。
- `stress >= 0.7`割合を合格閾値として使わない。
- social/infoの係数を特定の高ストレス結果に合わせて調整しない。
- physical proximityのための位置/建物形状を生成しない。
- 既存Run/Checkpointの係数やPolicyを読み替えない。

## 7日検証後の行動Policy v2と `core-model-suite-v2`

suite v1の100人・7日Runで睡眠負債平均が1,438.6分（上限1,440分）へ達した。1日Runの改善は長期の負債を解消できず、suite v1は行動モデルと身体状態の接続が弱いと判断した。1日の固定seed結果を改善成功と扱わず、suite IDとbehavior model IDを別版にした。

`sleep-aware-utility-softmax-v2` はsleep候補utilityを次のように変更する。

\[
U_{sleep}=0.25+0.90F+w\frac{D}{S_i}+0.20R-0.20\max(0,L-0.3),\quad w=1.0
\]

ここで (D) は睡眠負債分、(S_i) は個別に入力された必要睡眠分（未指定時は480分）、(F) はfatigue、(R) はsleep rhythm due、(L) は照明状態である。既存のutility-softmax-v1は元の `0.45 D/1440` 項のまま保持する。weight 1.0は1日必要睡眠相当の負債をutility 1単位に写像する**未校正仮定**であり、行動を強制せず、softmax候補選択の相対効用を変える。

新しいモデルIDにweight・式・source IDを登録し、既存のsuite v1とCheckpoint署名を維持する。suite v2の長期再検証は別JSONへ保存し、suite v1の記録と混ぜない。
