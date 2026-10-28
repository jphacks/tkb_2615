# 13. 包括的検証方法と結果

## 結論の範囲

検証はコードと合成Scenarioの内部整合性を対象とする。避難所での実地予測・心理尺度・臨床アウトカムの妥当性は検証していない。合格値を高ストレス率に置かず、入力と状態遷移の因果経路、再現性、bounded state、Traceの説明力で判定する。

機械可読比較: [core-model-suite-validation.json](core-model-suite-validation.json)。再実行: `SHELTERSIM_LLM_MODE=offline .venv/bin/python scripts/run_core_model_suite_validation.py --duration 1440`。

## 注記：#169 の前後の結果（2026-10-28、#171）

本書のsuite v1・v2の結果JSON 6つ（`core-model-suite-validation.json`、`core-model-suite-validation-7d.json`、`core-model-suite-mechanism-probe.json` と、それぞれのv2版）と、下の表・本文の数値は、#169 の不具合があったときの動作で計算した。検証スクリプトが入れる `policy_registry` はsuiteだけで、領域ごとの指定がない。当時の実行時コードは relationship・queue・誤情報の圧力について個別の指定だけを読んだため、suiteの宣言とは違うものが使われた。検証Scenarioには図面がない。

| 領域 | suiteの宣言（JSONの `model_signatures`） | #169 より前に使われたもの |
|---|---|---|
| queue | `need-weighted-physical-queue-v1` | `StrictFIFOQueuePolicy` |
| relationship | `distance-duration-repeat-trust-v1` | `DurationOnlyTrustPolicy` |
| psychology（誤情報の圧力） | `mean-reverting-life-state-v1` | 圧力を計算しない（0） |

#170 で、実行時はsuiteの宣言どおりのモデルを使うようになった。この場合の署名は `model_suite: core-model-suite-v2@2` で、上の結果JSONの `@1` と区別できる。v2の3つを、#170 のマージ後の `develop`（`7cc948e`）で同じ条件のまま実行し直し、別のファイルに保存した。

- [1日比較](core-model-suite-v2-validation-after-169.json)：`scripts/run_core_model_suite_validation.py --output …`（seed 2026、100 agents、1,440分）
- [7日 candidate-only](core-model-suite-v2-validation-7d-after-169.json)：`--duration 10080 --candidate-only`
- [mechanism probe](core-model-suite-v2-mechanism-probe-after-169.json)：`scripts/run_core_model_suite_mechanism_probe.py`（12人・180分）

#170 の直前のコミット（`249cee6`）で同じスクリプトを実行すると、3つとも元のv2のJSONと同じ値になる。1日比較と7日では、最終状態のハッシュ（`candidate_same_seed_final_state_sha256`、`same_seed_final_state_sha256`）も一致する。また、baselineの値は前後で変わらない。そのため、下の差はすべて #169 の修正によるものである。

| 指標（suite v2） | #169 より前 | #170 以降 |
|---|---:|---:|
| 1日：終了stress平均 | 0.7926 | 0.7952 |
| 1日：`stress ≥ 0.7` | 65% | 63% |
| 1日：mean sleep debt | 724.9分 | 726.2分 |
| 1日：sleep actions | 713 | 716 |
| 1日：toilet services complete | 138 | 135 |
| 1日：toilet queue person-minutes | 913 | 970 |
| 1日：最終平均待ち | 6.62分 | 7.14分 |
| 1日：最長待ち | 27分 | 78分 |
| 7日：終了stress平均 | 0.9019 | 0.9020 |
| 7日：`stress ≥ 0.7` | 94% | 89% |
| 7日：mean sleep debt | 981.3分 | 942.3分 |
| 7日：最大継続episode | 9,872分 | 9,894分 |
| probe：社会影響Trace positive／negative magnitude | 0.0411／0.0961 | 0.0411／0.0961 |

probeの false claim exposure 12、correction exposure 4、受容4、終了時誤信念7は変わらない。v1の結果は、今のスクリプトがv2しか入れないので実行し直していない。下の表・本文・ゲートの判定は #169 より前の値のまま残している。#170 以降の値でもこの解釈とゲートの判定が成り立つかは、#171 でモデル担当が確認する。

## Level 1–3: 数式・機構・接続経路

| 機構/接続 | 確認方法 | 結果・限界 |
|---|---|---|
| 心理平均回帰・自然回復 | `unit/policies/test_psychology.py` の解析解・刻み一致、`integration/test_research_psychology.py` のhigh-state recovery、`unit/domain/test_distress_monitor.py` のonline monitor | 一定外力の理論解と実装一致を確認。全体の固定点・結合Jacobianは未解析 |
| 睡眠負債 | `sleep_debt_step` を420/480/600分の反復日でテスト | 休息/睡眠を分離し、必要時間に等しい睡眠で一日差分0。fallbackと範囲検証も確認。睡眠時刻・臨床係数は未検証 |
| need → 行動 → service → need | `unit/domain/test_action_lifecycle.py`, `unit/domain/test_resource_service.py`, `integration/test_resource_service.py`, `integration/test_facility_queues.py`, `test_research_models.py` | event完了でneedが設定量だけ減る。normalized needと物理単位の変換は未校正 |
| 行動選択 | `tests/unit/policies/test_behavior.py`, behavior policy tests, 15分intervalのcandidate Run | 適格候補・確率・乱数再現はテスト済み。長期の行動頻度と必須行動の無期限延期は全組合せ未検証 |
| queue/service/staff | lifecycle、timeout、resource、facility、staff tests | capacityとservice eventの分離は確認。稼働能力が十分な条件と不足条件を揃えた感度は未完了 |
| 感情伝播 | `unit/policies/test_social.py` | 正方向/鎮静方向/接触なしゼロ/Traceを確認。接触ネットワーク・率は仮定 |
| 噂・訂正 → 心理 | `integration/test_information_spread.py`, `unit/policies/test_information.py`, `unit/policies/test_psychology.py`, `unit/domain/test_information.py` | belief/訂正/uncertainty pressureが分離され、訂正受容なしの安心は加えない。拡散全体の複数seed統合結果は未完了 |
| staff増員 → queue → psychology | staff/facility/psychology個別テスト | 各機構はあるが、同一Scenarioの因果追跡付き増員AB比較は本実施では未完了 |

## Level 4: 統合Scenario

既存の `research_operational_100_7d.json` と `research_shortage_100_7d.json` は通常/不足を比較する再利用可能な合成条件。両者は個別に合成され、現実の需給推定ではない。短時間ScenarioやA–Hの各要因をそろえた新しい統合行列は未完了。

候補suiteとの1日同seed比較は、同じ研究Scenario、seed 2026、100 agents、1,440分で実行した。測定値、Scenario hash、Policy signature、Trace SUMMARY/OFFの最終状態一致はJSONに記録する。候補とbaselineはPolicy/係数が異なるため、差は因果効果推定ではなく固定seedのsoftware comparisonである。

| 指標 | 保存baseline | suite v1 | suite v2 | 解釈 |
|---|---:|---:|---:|---|
| 終了stress平均 | 0.8272 | 0.8070 | 0.7926 | v2が最小だが各比較は同seedの記述値 |
| p95 stress | 1.0000 | 1.0000 | 1.0000 | 全条件に上限clipへ達するAgentがいる |
| `stress ≥ 0.7` | 73% | 68% | 65% | 任意の合格閾値ではなく記述指標 |
| mean sleep debt | 1,421.6分 | 845.2分 | 724.9分 | 負債比例効用で睡眠actionが増加 |
| sleep actions | 97 | 190 | 713 | 1日では睡眠候補の効用変更に反応。長期評価は下記 |
| toilet services complete | 146 | 162 | 138 | v2は睡眠増加に伴い件数が減った |
| toilet queue person-minutes | 540 | 851 | 913 | 待機負荷はsuiteとともに増加した |
| 最終平均待ち | 3.70分 | 5.25分 | 6.62分 | utilityを変えても待ち時間トレードオフは残る |

suite v1とv2それぞれ、同じcandidate ScenarioのTrace SUMMARYとTrace OFFではfinal WorldStateが一致した。各機械結果の `same_seed_trace_on_off_final_state_equal` は `true`。比較Scenario hash、実行時間、全policy signaturesはsuite別JSONを参照。

suite v2の12人・180分・seed 2026 mechanism probeでは、false claim exposure 12、correction exposure 4、受容4、終了時誤信念7であった。社会影響Traceはpositive 0.0411、negative magnitude 0.0961。baselineはそれぞれ0.0414/0.0939、終了時誤信念6であった。これは社会・情報経路が動く合成入力であり、効果量や訂正成功率の一般化を示さない。[suite v1 probe](core-model-suite-mechanism-probe.json) と [suite v2 probe](core-model-suite-v2-mechanism-probe.json) にseed、Scenario hash、model signaturesを保存した。

## Level 5: 長時間・複数seed

suite v1の100人・7日・seed 2026 candidate-only Runは終了stress平均0.9347、`stress ≥ 0.7` 99%、sleep debt 1,438.6分で、長期負債・distressの飽和が継続した。これを受け、sleep-aware行動utilityを追加してsuite v2へ版上げした。suite v2の同規模・同seed candidate-only再Runでは終了stress平均0.9019、`stress ≥ 0.7` 94%、sleep debt 981.3分となった。v1より各指標は改善した一方、終了時にも94%が閾値以上で、最大継続episodeは9,872分だった。睡眠負債の改善だけでは慢性stressを解消しない。これは1 seedの記述比較であり、v1とv2以外の基準suiteを含めた因果比較ではない。Trace OFFのため、7日Runの行動別件数・社会伝播寄与は記録していない。

両suiteの長期結果はそれぞれ [v1 7日結果](core-model-suite-validation-7d.json) と [v2 7日結果](core-model-suite-v2-validation-7d.json) に保存した。通常/不足条件の複数seed、12/30人規模、介入A/B、Checkpoint途中復元、Trace ON/OFFの7日比較は未完了である。既存baselineの2回実行は [09-internal-validation.md](09-internal-validation.md) に記録されている。

## 合格ゲート

| Gate | 判定 | 根拠 |
|---|---|---|
| A 数理的整合性 | 部分合格 | sleep収支と心理平均回帰の単体性質、Trace clip差を確認。全feedbackの固定点/Jacobian、需要単位、モデル間clip監査は残る |
| B 行動・身体 | 部分合格 | service completion、need反映、睡眠収支、choice境界を確認。v2は睡眠負債を改善したが、睡眠負債はなお981分。必須行動の長期延期や複数need/個人差を横断する長時間検証は残る |
| C 心理・社会・情報 | 部分合格 | 回復、正負伝播、情報訂正経路のテストあり。v2の7日Runで94%がstress 0.7以上、最長episode 9,872分。良好条件での全状態非飽和と複数seedの長期統合は未確認 |
| D 比較実験 | 未合格 | baseline/suite固定seed比較は行うが、A–Hの条件行列、単調制御、全介入の因果Trace検証は未完了 |
| E 既存機能・互換性 | 部分合格 | 旧selector/Checkpoint保持、suite signature、生成入口、Trace再現性の関連テストと全pytestを実行。全体はsandbox制約と未導入optional SDKに起因する10件の失敗が残るため未合格 |
| F 透明性 | 部分合格 | 出典・仮定・実行JSONを保存。未実施条件と外部妥当性限界を明記。重大な未説明結果のないことは全scenarioで確定していない |

未合格ゲートは誤魔化さず保留する。短い固定seed差を効果量や実地妥当性に読み替えない。

## 実行ログ

- `SHELTERSIM_LLM_MODE=offline .venv/bin/pytest -q tests/test_provenance.py tests/test_policy_registry_full.py tests/test_research_models.py` — 36 passed.
- `SHELTERSIM_LLM_MODE=offline .venv/bin/pytest -q tests/test_policy_registry_full.py tests/test_startable_generated_scenarios.py tests/test_scenario_editor.py` — 33 passed.
- `.venv/bin/ruff check --output-format concise scripts/run_core_model_suite_validation.py scripts/run_core_model_suite_mechanism_probe.py src/sheltersim/experiments/model_suite.py tests/test_policy_registry_full.py --select I,F,E501` — passed. 既存の大きな差分対象ファイル全体に対する広域lintは既存行の違反を多数検出するため、今回追加した主要コードを絞って確認した。
- `python -m json.tool` — source manifest、model catalog、1日比較JSON、機構probe JSONを構文検査。
- `git diff --check` — passed.
- `SHELTERSIM_LLM_MODE=offline XDG_STATE_HOME=/private/tmp/sheltersim-test-state .venv/bin/python scripts/run_core_model_suite_validation.py --duration 10080 --candidate-only --output docs/mathematical-models/core-model-suite-v2-validation-7d.json` — completed, seed 2026; 100 agents; 7 days; 1,186.9 sec. End distress mean 0.9019, high-distress share 0.94, mean sleep debt 981.3 min. Candidate-only, trace off; not causal or external validation.
- Validation JSON records `git_head_at_run` as the parent revision and `implementation_commit_sha=e4405419e7de96a71df85e6312f32ce785cff430` as the exact source snapshot; `working_tree_dirty_at_run=true` because the implementation was committed after the runs.
- `.venv/bin/ruff check --output-format concise scripts/run_core_model_suite_validation.py scripts/run_core_model_suite_mechanism_probe.py src/sheltersim/experiments/model_suite.py tests/test_policy_registry_full.py --select I,F,E501` — passed.
- `git diff --check` — passed.
- `SHELTERSIM_LLM_MODE=offline XDG_STATE_HOME=/private/tmp/sheltersim-test-state .venv/bin/pytest -q` — 668 passed, 10 failed, 3 skipped in 616.82 sec. Two mocked Anthropic-provider tests fail because the optional `anthropic` SDK is not installed. Eight storage tests attempt `/Users/mizuki/Library/Application Support/ShelterSim/state/.lock`; the sandbox denies writes there despite `XDG_STATE_HOME`. These are environment limitations, so Gate E remains partial. No code-level failure appeared in the focused model suite.
- `SHELTERSIM_LLM_MODE=offline XDG_STATE_HOME=/private/tmp/sheltersim-test-state .venv/bin/pytest -q tests/test_agents.py tests/test_policy_registry_full.py tests/test_model_evaluation.py tests/test_policy_parameter_overrides.py tests/test_scenario_editor.py tests/test_provenance.py tests/test_startable_generated_scenarios.py` — 92 passed in 25.12 sec.
- 実施していない複数seed、条件行列、介入A/B、suiteのCheckpoint途中復元は未実施として残す。
