# テストと検証

## 通常のoffline確認

開発toolを導入し、次を実行します。

~~~shell
python -m pip install -e ".[dev]"
SHELTERSIM_LLM_MODE=offline python -m pytest
ruff check .
ruff format --check .
~~~

標準pytestには長時間simulation testも含まれます。live API testはSHELTERSIM_ALLOW_LIVE_API_TESTSとANTHROPIC_API_KEYの両方を明示した場合だけ対象です。通常のCIではlive gateを設定しないでください。

### 並列実行

開発依存にはpytest-xdistが含まれます。複数CPUで並列に実行するには`-n auto`を付けます。

~~~shell
SHELTERSIM_LLM_MODE=offline python -m pytest -n auto
~~~

Ryzen 5 5625U（WSL2、12スレッド）での全件（640 passed、3 skipped）は、直列で約12〜15分、`-n auto`で約7〜9分でした（同じPCでの複数回の計測。負荷により変動します）。最も長い`test_100_agent_seven_day_combined_scenario_preserves_operational_invariants`だけで6分前後かかるため、並列にしてもそれより短くはなりません。開発中の確認には`-m "not long_simulation"`で長時間simulationを除けます。

2026-10-27に`develop`（`64f7059`）を別のPC（Ryzen 7 9800X3D、WSL2、16スレッド）で実行すると、全件（896 passed、4 skipped）は`-n auto`で約2分でした。直列は計測していません。このPCで最も長いテストは約100秒（`test_one_meal_station_keeps_up_with_one_hundred_people`と`test_100_agents_seven_day_failure_scenario_runs_offline`）でした。

tests/conftest.pyは各testのProject保存先（SHELTERSIM_PROJECTS_DIR）、状態保存先（XDG_STATE_HOME）、LLM cache（SHELTERSIM_LLM_CACHE_PATH）を一時directoryに向けます。並列workerがこれらを共有せず、開発者のrunsやPC全体の利用記録にも書き込みません。test内でこれらを設定した場合はその値が優先されます。live_api testは、実費をPC全体の利用記録に残すため対象外です。

## 移管時の検証記録

採用したsource snapshot a162aed8284641c2a21e21bbcd04bad6e595ffb1で、CPython 3.12.14と固定dependencyを使ってoffline pytest全件を実行し、**579 passed、3 skipped**でした。所要時間は545.17秒です。ANTHROPIC_API_KEYとlive test gateは未設定で、有料Provider呼び出しはありません。

同じsource snapshotでRuffも確認しました。

- ruff check . は既存指摘1,906件: E501 1,758、E701 71、E702 40、I001 28、F401 6、F841 3。
- ruff format --check . は130個のPython fileを整形対象、87個は整形済みと報告しました。

これらのrepository全体のstyle指摘は文書整理以前から存在します。文書整理ではsource/testの動作を変更していません。GitHub Actionsはdispatchしていません。二つのworkflowは手動実行のままです。

## 主なテスト範囲

testは決定的state遷移、Checkpoint serializationと復元、独立Branch、event順、施設・資源・職員操作、履歴・timeline検索、model provenance、LLM fallbackとresponse tape、Project永続化、Web route、Demo成果物を確認します。テストはpackageの隣ではなく`tests/`直下にあり、ライブAPIテストだけが`tests/integration/`にあります。どのファイルが何を確かめるかは、下の[テストの地図](#テストの地図)を参照してください。

Project画面の作用説明（`project-round3.js`の`renderEffectExplanation`）は、`tests/test_effect_explanation_ui.py`がその関数だけをnodeで実行して確認します。fallback時の理由表示などが対象です。nodeがない環境ではskipします。

demo-v2の地図描画は、`tests/test_installed_facility_rendering.py`が確認します。timeline routeが返すframe（保存済み境界のframe cacheと、境界以外の再構成）をnodeで`demo-v2-scene.js`・`demo-v2-sprites.js`・`demo-v2-map.js`に渡し、描画命令を受け流すcanvasの代わりを使って、`HinanjoMap.draw`が返す施設アイコンの記録（hit）を検査します。介入で追加したトイレが実座標の地図と模式図の両方で別々の位置に描かれること、座標のない施設は実座標の地図に描かれないことが対象です。模式図は施設を実座標と無関係に並べるため、模式図に描かれても図面上の位置があるとは限りません。画素の見た目（色・重なり・文字）は確認しません。nodeがない環境では描画のtestだけをskipします。

## テストの地図

2026-10-27時点の`develop`（`64f7059`）で、`tests/`の全テストファイル104本（`tests/`直下103本と`tests/integration/`の1本）を分類しました。#53の段階1（#57）の成果です。テストを足したり移したりしたら、この表も直します。

### 分類の軸

- **担当**：主に確かめているモジュール。AGENTS.mdの区分に加え、`cli`（`sheltersim.cli`と`__main__`）と、複数モジュールにまたがって主担当を決められない`横断`を使います。
- **層**：主な層を1つと、ほかに含む層を挙げます。
  - 単体：1つのモジュールの関数・クラスを直接呼ぶ。runnerで時間を進めないか、進めても数tickに限る。
  - 組合せ：runner・engine・Branchで時間を進め、複数モジュールにまたがる状態やイベントの変化を確かめる。
  - 結果：KPI、比較レポート、画面データ（frame・timeline）、PDF、説明文など、利用者が目にする出力の値や内容を確かめる。
  - 契約：同じseedで同じ結果になること、Checkpointからの復元、スキーマ版と互換、HTTP API・CLIの入出力、出典台帳とprovenance、秘匿情報の扱いを確かめる。
  - UI：nodeでブラウザー用のJavaScriptを動かす、または画面用の静的ファイルやHTMLの配信を確かめる。
- **Scenario**：テストが使うScenarioの出どころ。
  - fixture：`fixtures/`のScenario（`src/sheltersim/web/examples/failure_day3.json`の同梱版を含む）。読んだ後に一部を書き換えるものも含みます。どれも`model_configs`に`facility_geometry`も`agent_dynamics`も持ちません。
  - 生成：`InitialScenarioBuilder`で作ったScenarioを実行する。UIで作るScenarioと同じく、図面（`facility_geometry`）と認知（`agent_dynamics`）を持ちます。`test_facility_orientation.py`だけは、比べるために認知を外した生成Scenarioも実行します。
  - 生成（実行なし）：生成したdraftを検査するだけで、実行しない。
  - fixture＋図面：fixtureに図面だけを手で足して実行する。認知は持たず、UIでは作られない組み合わせです。
  - 手書きRun成果物：テスト内で書いたRun成果物のファイル（`manifest.json`など）を読む。
  - コード内で組立：テスト内でScenarioを一から組み立てる。
  - なし：Scenarioを使わない。
- **印**：pytestの印（`live_api`、`long_simulation`）。

Scenario欄は、全件のoffline実行中に`ScenarioSpec.load`、`InitialScenarioBuilder.build`、`WorldState.from_scenario`の呼び出しをテストファイルごとに記録して決めました。実行されない`live_api`と、別プロセスで動くCLIの中身はコードを読んで決めました。層と担当は、テスト名と本体を読んで決めた判断で、境界の例ではほかの層と迷うものがあります。

### 一覧

| ファイル | 担当 | 主な層 | ほかの層 | Scenario | 印 |
|---|---|---|---|---|---|
| `test_checkpoint.py` | core | 契約 |  | fixture |  |
| `test_engine.py` | core | 単体 | 契約 | fixture | long_simulation |
| `test_per_tick_history_validation.py` | core | 単体 |  | 生成 |  |
| `test_state.py` | core | 単体 | 契約 | fixture |  |
| `test_action_lifecycle.py` | domain | 単体 |  | fixture |  |
| `test_ambient_demand_effects.py` | domain | 組合せ | 契約 | fixture |  |
| `test_care_lifecycle.py` | domain | 組合せ | 契約 | 生成・fixture |  |
| `test_cascading_infrastructure.py` | domain | 組合せ | 契約 | fixture |  |
| `test_distress_monitor.py` | domain | 単体 |  | なし |  |
| `test_facilities.py` | domain | 単体 |  | fixture |  |
| `test_facility_awareness.py` | domain | 組合せ | 契約 | fixture |  |
| `test_facility_orientation.py` | domain | 組合せ | 契約 | 生成 |  |
| `test_facility_queues.py` | domain | 組合せ | 契約 | fixture＋図面 |  |
| `test_geometry_config_cache.py` | domain | 単体 | 契約 | 生成 |  |
| `test_installed_facility_announcement.py` | domain | 組合せ | 結果 | 生成 |  |
| `test_operational_events.py` | domain | 組合せ | 契約 | fixture |  |
| `test_physical_queue_overflow.py` | domain | 組合せ | 結果 | 生成 |  |
| `test_population_demand_integration.py` | domain | 組合せ | 契約 | fixture＋図面 |  |
| `test_queue_service_while_repositioning.py` | domain | 組合せ | 結果 | 生成 |  |
| `test_resource_ledger_state_check.py` | domain | 単体 | 契約 | fixture |  |
| `test_resource_loss_without_operational_state.py` | domain | 組合せ | 契約 | 生成 |  |
| `test_resource_queue_regressions.py` | domain | 組合せ |  | 生成 |  |
| `test_resource_service.py` | domain | 組合せ | 契約 | fixture |  |
| `test_resources.py` | domain | 組合せ | 契約・結果 | fixture |  |
| `test_staff.py` | domain | 組合せ | 結果 | fixture |  |
| `test_staff_operation_relief.py` | domain | 組合せ | 契約 | fixture |  |
| `test_staff_support_travel.py` | domain | 組合せ | 契約 | fixture＋図面 |  |
| `test_staff_task_order_effect.py` | domain | 組合せ | 契約 | fixture |  |
| `test_structured_social_history.py` | domain | 組合せ | 契約 | fixture |  |
| `test_effect_program.py` | events | 組合せ | 契約 | fixture＋図面 |  |
| `test_events.py` | events | 単体 |  | fixture |  |
| `test_agents.py` | policies | 単体 | 契約 | fixture |  |
| `test_causal_stress.py` | policies | 組合せ | 契約 | コード内で組立 |  |
| `test_information_spread.py` | policies | 組合せ | 契約 | fixture |  |
| `test_policy_parameter_overrides.py` | policies | 契約 | 組合せ | 生成 |  |
| `test_policy_registry_full.py` | policies | 契約 | 単体 | fixture |  |
| `test_research_models.py` | policies | 単体 | 組合せ | fixture |  |
| `test_research_psychology_default.py` | policies | 契約 | 組合せ | 生成・fixture |  |
| `test_route_bottleneck.py` | policies | 組合せ | 結果 | fixture |  |
| `test_social.py` | policies | 組合せ | 契約 | fixture |  |
| `test_space.py` | policies | 単体 |  | fixture |  |
| `test_branch.py` | experiments | 契約 | 組合せ | fixture |  |
| `test_comparison.py` | experiments | 結果 | 契約 | fixture |  |
| `test_e2e.py` | experiments | 組合せ | 契約 | fixture | long_simulation |
| `test_exact_timeline.py` | experiments | 契約 |  | fixture |  |
| `test_household_correction.py` | experiments | 契約 |  | 生成（実行なし） |  |
| `test_metrics.py` | experiments | 結果 | 契約 | fixture |  |
| `test_model_evaluation.py` | experiments | 単体 | 契約 | fixture・生成（実行なし） |  |
| `test_operational_scenarios.py` | experiments | 組合せ | 結果 | fixture | long_simulation |
| `test_performance_smoke.py` | experiments | 契約 |  | fixture |  |
| `test_profiles.py` | experiments | 単体 | 契約 | fixture |  |
| `test_provenance.py` | experiments | 契約 |  | fixture |  |
| `test_sanitation_diagnostics.py` | experiments | 単体 |  | fixture |  |
| `test_scenario_editor.py` | experiments | 契約 | UI | fixture |  |
| `test_sensitivity.py` | experiments | 結果 | 契約 | fixture |  |
| `test_service_capacity.py` | experiments | 単体 | 契約 | fixture・生成（実行なし） |  |
| `test_startable_generated_scenarios.py` | experiments | 単体 |  | 生成（実行なし） |  |
| `test_timed_branch_plan.py` | experiments | 契約 |  | fixture | long_simulation |
| `test_imported_plan_route_revision.py` | region | 組合せ | 契約 | なし |  |
| `test_region_providers.py` | region | 単体 | 契約 | なし |  |
| `test_region_water_outage_conditions.py` | region | 組合せ |  | 生成 |  |
| `test_standard_provisioning.py` | region | 単体 | 組合せ・結果 | 生成 |  |
| `integration/test_live_api.py` | llm | 組合せ | 結果・契約 | fixture | live_api |
| `test_adaptive_event_eval.py` | llm | 契約 |  | fixture |  |
| `test_complex_interactions.py` | llm | 組合せ | 契約 | fixture |  |
| `test_llm_outcomes.py` | llm | 契約 | 結果 | fixture・生成（実行なし） |  |
| `test_llm_prepare.py` | llm | 契約 |  | fixture |  |
| `test_llm_provider.py` | llm | 単体 | 契約 | fixture |  |
| `test_llm_review.py` | llm | 契約 | 結果 | fixture |  |
| `test_llm_run_integration.py` | llm | 組合せ | 契約 | fixture |  |
| `test_llm_runtime.py` | llm | 組合せ | 契約 | fixture |  |
| `test_nl_opus.py` | llm | 契約 | 組合せ | 生成・fixture |  |
| `test_nl_parser.py` | llm | 単体 | 契約 | fixture |  |
| `test_story_report.py` | llm | 結果 | 契約 | なし |  |
| `test_support_priority_runtime.py` | llm | 組合せ | 契約 | fixture |  |
| `test_checkpoint_save_regressions.py` | storage | 契約 |  | fixture |  |
| `test_project_store.py` | storage | 契約 |  | fixture |  |
| `test_round3_common_services.py` | storage | 契約 |  | fixture |  |
| `test_scaling_storage.py` | storage | 契約 |  | fixture |  |
| `test_timeline_frame_cache.py` | storage | 契約 |  | fixture |  |
| `test_windows_storage_compat.py` | storage | 契約 |  | fixture |  |
| `test_admin_audit.py` | web | 契約 | UI | fixture |  |
| `test_demo_jobs.py` | web | 契約 | 組合せ | fixture |  |
| `test_demo_performance.py` | web | 契約 |  | fixture |  |
| `test_demo_timeline.py` | web | 結果 |  | 手書きRun成果物 |  |
| `test_demo_v2.py` | web | UI | 単体 | なし |  |
| `test_demo_v2_polling.py` | web | UI |  | なし |  |
| `test_effect_explanation_ui.py` | web | UI |  | なし |  |
| `test_installed_facility_rendering.py` | web | UI | 結果 | 生成 |  |
| `test_judge_demo_e2e.py` | web | 結果 | 契約・UI | fixture |  |
| `test_map_layout.py` | web | 結果 |  | 手書きRun成果物 |  |
| `test_multi_plan.py` | web | 契約 | 組合せ | fixture |  |
| `test_nl_demo_ui.py` | web | 契約 |  | fixture |  |
| `test_pdf_report.py` | web | 結果 | 契約 | fixture |  |
| `test_pointer_only_reads.py` | web | 契約 |  | fixture |  |
| `test_replay_frames.py` | web | 契約 |  | fixture |  |
| `test_results_chat.py` | web | 契約 |  | fixture |  |
| `test_resume_checkpoint_loads.py` | web | 契約 |  | fixture |  |
| `test_web_demo.py` | web | 契約 | 組合せ | fixture |  |
| `test_web_inspector.py` | web | 結果 | 契約 | fixture |  |
| `test_web_routes.py` | web | UI | 契約 | なし |  |
| `test_inspect_cli.py` | cli | 契約 |  | fixture |  |
| `test_smoke.py` | cli | 契約 |  | なし |  |
| `test_round2_common_services.py` | 横断 | 契約 |  | 生成・fixture |  |

### 集計

担当ごとの集計です。「結果の層を含む」は、主な層かほかの層に「結果」があるファイルの数です。

| 担当 | ファイル | 生成Scenarioを実行 | 生成（実行なし） | fixtureのみ | 結果の層を含む |
|---|---|---|---|---|---|
| core | 4 | 1 | 0 | 3 | 0 |
| domain | 25 | 8 | 0 | 13 | 5 |
| events | 2 | 0 | 0 | 1 | 0 |
| policies | 10 | 2 | 0 | 7 | 1 |
| experiments | 17 | 0 | 4 | 13 | 4 |
| region | 4 | 2 | 0 | 0 | 1 |
| llm | 13 | 1 | 1 | 10 | 4 |
| storage | 6 | 0 | 0 | 6 | 0 |
| web | 20 | 1 | 0 | 13 | 6 |
| cli | 2 | 0 | 0 | 1 | 0 |
| 横断 | 1 | 1 | 0 | 0 | 0 |
| 計 | 104 | 16 | 5 | 67 | 21 |

主な層ごとに見ると、生成Scenarioを実行するのは単体21本中3本、組合せ34本中8本、結果9本中0本、契約35本中4本、UI 5本中1本です。

fixtureのScenarioは次のとおり使われています（全件のoffline実行中に読まれた回数。読んだ後に書き換える分を含みません）。

| Scenario | 読まれた回数 | 内容 |
|---|---|---|
| `scenario_100_agents.json` | 168 | 100人・7日。`scenarios/baseline_100_7d.json`と同じIDだが、`model_configs`は`behavior`だけで、社会・職員・トイレの設定と仮定IDの一覧を持たない |
| `scenarios/h00_small_replay.json` | 36 | 小規模の再生用 |
| `scenarios/baseline_100_7d.json` | 32 | 100人・7日の基準 |
| `web/examples/failure_day3.json`（同梱版） | 31 | デモの既定。`scenarios/failure_day3.json`との一致は`test_provenance.py`が確かめる |
| `scenarios/`の運営イベント系6本 | 37 | `test_operational_scenarios.py`と`test_scenario_editor.py`が使う |
| `scenarios/failure_day3.json` | 10 | 3日目の設備故障 |
| `scenarios/research_operational_100_7d.json` | 1 | `test_service_capacity.py`だけが使う |
| `scenarios/research_shortage_100_7d.json` | 0 | テストからは読まれない。数理モデル文書の検証で使う |

### 見えた穴

1. **利用者が見る結果を、利用者の経路で確かめるテストがない。** 主な層が「結果」の9本は、すべてfixtureか手書きRun成果物を使います。生成Scenarioで結果を確かめるのは、ほかの層として結果を含む5本（`test_installed_facility_announcement.py`、`test_installed_facility_rendering.py`、`test_physical_queue_overflow.py`、`test_queue_service_while_repositioning.py`、`test_standard_provisioning.py`）だけです。そのうち介入して本線と比べるのはトイレ追加だけです。→ #60
2. **experiments、storage、eventsを主担当とするファイルは、生成Scenarioを1本も実行しない。** Branch、比較、時系列、Project保存、EffectProgramは、図面と認知を持つ状態で確かめられていません（`横断`の`test_round2_common_services.py`は生成Scenarioの保存を一部含みます）。webも、生成Scenarioを実行するのは描画の1本だけで、デモ、HTTP、PDF、再生frameはfixtureだけで確かめています。→ #58、#60
3. **UIでは作られない組み合わせで確かめているファイルがある。** `test_effect_program.py`、`test_facility_queues.py`、`test_population_demand_integration.py`、`test_staff_support_travel.py`は、fixtureに図面だけを足し、認知を持たない状態で実行します。→ #58
4. **再現性の固定が生成Scenarioにない。** `fixtures/regression-contract.json`が結果を固定するのは`baseline_100_7d`と`small_replay`（どちらもfixture）だけです。→ #58
5. **生成Scenarioのテストが遅く、印もない。** 実行時間の上位5本のうち3本が生成Scenarioのテストで（`test_one_meal_station_keeps_up_with_one_hundred_people`約104秒、`test_staffed_water_station_serves_a_queue_longer_than_its_room`約58秒、`test_generated_scenario_with_entered_stock_completes_a_day`約52秒）、`long_simulation`の印がありません。生成Scenarioを新しいテストで気軽に使うには、小規模な生成Scenarioが要ります。→ #58
6. **層が混ざったファイルと、名前から中身が分からないファイルがある。** 主な層が契約のファイルが35本と最も多く、その多くは組合せの検査も含みます。`test_round2_common_services.py`（24件）と`test_round3_common_services.py`は、LLMの利用記録、保存、web projectionをまとめて扱い、名前から担当が分かりません。→ #59
7. **生成したdraftを検査するだけのファイルがある。** 5本（`test_household_correction.py`、`test_llm_outcomes.py`、`test_model_evaluation.py`、`test_service_capacity.py`、`test_startable_generated_scenarios.py`）は生成Scenarioを作りますが実行しません。生成の検査としては十分ですが、実行時の振る舞いは別のテストに頼っています。

## 確認できていないこと

- 今回、live有料Provider testは実行していません。
- Windows/macOSの実機上で起動確認していません。
- 30日間の性能結果は確立していません。過去の30人・30日試行は10分以内に完了しませんでした。
- 現在のbrowser証跡だけでは、全map layer、実施設図面取込、介護・会話・職員の組合せ、大規模人口を含む一連の操作を確認したとは言えません。
- 地域人口、避難所、天候、医療、結果の照合データを用いた外部検証はできていません。
- 心理、健康、社会、容量、incidentの係数は実測校正されていません。合成Branchの差は、条件付き出力であり、予測や現実の因果効果ではありません。

再現可能な1日性能値と環境はbenchmarks.md、モデル仮定とsource recordはmodel-validation.mdとfixtures/source-manifest.jsonを参照してください。
