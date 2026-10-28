# テストと検証

## 通常のoffline確認

開発toolを導入し、次を実行します。

~~~shell
python -m pip install -e ".[dev]"
SHELTERSIM_LLM_MODE=offline python -m pytest
ruff check .
ruff format --check .
~~~

`ruff check .`と`ruff format --check .`は指摘なしで通ります（既存の指摘は#139の一括整形で片付けました）。変更後もこの2つが通る状態を保ちます。1行100桁を超える文字列は、文言を変えずに、隣り合う文字列リテラルの連結に分けて書きます。分けると値が変わる複数行の文字列（SQLiteの表定義など）だけ、閉じる引用符の行に`# noqa: E501`を付けます。

一括整形のcommitは`.git-blame-ignore-revs`に登録してあります。手元の`git blame`で整形前の変更をたどるには、一度だけ次を設定します。GitHubのblame表示はこのファイルを自動で使います。

~~~shell
git config blame.ignoreRevsFile .git-blame-ignore-revs
~~~

標準pytestには長時間simulation testも含まれます。live API testはSHELTERSIM_ALLOW_LIVE_API_TESTSとANTHROPIC_API_KEYの両方を明示した場合だけ対象です。通常のCIではlive gateを設定しないでください。

### 並列実行

開発依存にはpytest-xdistが含まれます。複数CPUで並列に実行するには`-n auto`を付けます。`pyproject.toml`で`--dist worksteal`を指定しているので、手の空いたworkerが残りのテストを引き取り、長いテストが最後にまとまっても全体が延びにくくなっています（`-n`なしでは効果はありません）。worksteal は、集めたテストを順に連続した塊に分けて各workerへ配ります。`tests/conftest.py`の`pytest_collection_modifyitems`が、`long_simulation`の印のテストを各塊の先頭に1本ずつ置くので、長時間テストは最初から並んで走ります（印のテストがないとき、またはworkerが1つのときは並びを変えません）。2026-10-27に Ryzen 7 9800X3D で計ると、全件は約163秒から約144秒になりました。長時間テストを先頭にまとめると、1つのworkerに集まって約197秒、`--dist load`にするとモジュール単位の準備を作り直して約420秒と、どちらも遅くなりました。

~~~shell
SHELTERSIM_LLM_MODE=offline python -m pytest -n auto
~~~

Ryzen 5 5625U（WSL2、12スレッド）での全件（640 passed、3 skipped）は、直列で約12〜15分、`-n auto`で約7〜9分でした（同じPCでの複数回の計測。負荷により変動します）。最も長い`test_100_agent_seven_day_combined_scenario_preserves_operational_invariants`だけで6分前後かかるため、並列にしてもそれより短くはなりません。開発中の確認には`-m "not long_simulation"`で長時間simulationを除けます。

2026-10-27に`develop`（`64f7059`）を別のPC（Ryzen 7 9800X3D、WSL2、16スレッド）で実行すると、全件（896 passed、4 skipped）は`-n auto`で約2分でした。直列は計測していません。このPCで最も長いテストは約100秒（`test_one_meal_station_keeps_up_with_one_hundred_people`と`test_100_agents_seven_day_failure_scenario_runs_offline`）でした。

`long_simulation`の印は、7日間のsimulationを実行するテストと、1件で30秒以上（直列）かかるテストに付けます。今は、7日間のテストが4件と、生成Scenarioを1日分実行するテストが4件の計8件です。手動実行のCIでは、`オフラインCI`（`.github/workflows/ci.yml`）がこれらを除き、`長時間simulation回帰`（`.github/workflows/long-regression.yml`）の`long`がこれらだけを実行します。印のない`pytest`は全件を実行します。2026-10-27に同じPC（Ryzen 7 9800X3D）で計ると、`-n auto`で全件が約2分20秒、`-m "not long_simulation"`が約1分40秒でした。

### 結果の残り方と見方

pytestを実行するたびに、`tests/results_log.py`（`tests/conftest.py`が読み込むpytestのplugin）が、結果をリポジトリ直下の`.test-results/<UTCの日時>-<プロセスID>/`に書きます。`.test-results/`はgitに登録しません。全件でも一部でも、並列（`-n`）でも同じです。

- `summary.txt`：実行したコマンド、件数、失敗（FAILED）とエラー（ERROR）の一つ一つについてtracebackと捕まえた出力、時間のかかったテスト15件。実行の終わりに書きます。
- `results.jsonl`：テストの段階（setup・call・teardown）ごとに1行で、結果、時間、並列のworker、失敗ならtracebackの文字列。実行中に書き足すので、途中で止まった実行でもそこまでの結果が残ります。
- `.test-results/latest.txt`：いちばん新しい実行のディレクトリ名。ディレクトリは新しいものから20回分だけ残し、古いものは消します。

出力の最後の行は、pytestの集計行のあとに足す次の1行です。失敗があれば、失敗したテストの名前（5件まで）と`summary.txt`の場所が入ります。

~~~text
test results: 1 failed, 967 passed, 4 skipped; failed: tests/unit/x/test_y.py::test_z; tracebacks in .test-results/20261028T010203Z-1234/summary.txt
~~~

失敗の原因は、もう一度実行せずに`summary.txt`で確かめます。`.pytest_cache`の失敗記録（`--lf`）は、次に全件が通ると消えるので、記録としては使いません。コーディングエージェントも、`| tail -1`などで集計行だけを見て判断しないでください。失敗があったら、この最後の行で名前を、`summary.txt`で原因を確かめ、テストの名前と原因を報告します。結果を残したくない実行では`-p no:results_log`を付けます。

手動実行のCI（`.github/workflows/`）でも同じファイルが書かれますが、artifactとして保存していません。保存するにはworkflowの変更が要るので、チームで合意してから行います。

tests/conftest.pyは各testのProject保存先（SHELTERSIM_PROJECTS_DIR）、状態保存先（XDG_STATE_HOME）、LLM cache（SHELTERSIM_LLM_CACHE_PATH）を一時directoryに向けます。並列workerがこれらを共有せず、開発者のrunsやPC全体の利用記録にも書き込みません。test内でこれらを設定した場合はその値が優先されます。live_api testは、実費をPC全体の利用記録に残すため対象外です。

### 網羅率の計測

網羅率（行と分岐）は、必要なときだけ測ります。開発依存にpytest-covとcoverageを入れてあり、設定は`pyproject.toml`の`[tool.coverage.*]`です（対象は`sheltersim`、分岐も数える）。通常のpytestは計測しません。

~~~shell
SHELTERSIM_LLM_MODE=offline python -m pytest -n auto -m "not long_simulation" --cov --cov-report=html
python -m coverage report --sort=cover
~~~

計測の結果はリポジトリ直下の`.coverage`と`htmlcov/`に書かれます。どちらも`.gitignore`の対象です。`htmlcov/index.html`を開くと、ファイルごとに通っていない行と分岐が見えます。

`long_simulation`の印のテストは外します。計測すると、それらだけが極端に遅くなるためです。2026-10-28の計測では次のとおりでした。

- 全件：2時間6分。7日間のテスト1件が52倍遅くなり、約2時間かかった。
- 印のテストを外す：7分25秒。網羅率は73.2%から73.1%になり、外したことで通らなくなる行は6行だけだった（`events/effects.py`のguardの比較、`web/demo.py`の1行）。

#### 2026-10-28の計測（`develop`の`e12b7d4`）

印のないテスト（992 passed、4 skipped）で、行77.8%（27,984/35,965）、分岐62.4%（9,786/15,686）、合わせて73.1%でした。担当ごとの値は次のとおりです。

| 担当 | 行数 | 行 | 行と分岐 |
|---|---:|---:|---:|
| `region` | 4,704 | 53.8% | 46.9% |
| `web` | 4,698 | 70.3% | 66.5% |
| `storage` | 2,047 | 72.0% | 67.4% |
| `events` | 3,075 | 81.6% | 76.7% |
| `core` | 994 | 81.6% | 77.6% |
| 直下のファイル | 410 | 80.2% | 77.7% |
| `domain` | 8,097 | 83.5% | 78.8% |
| `llm` | 2,992 | 83.2% | 78.9% |
| `contracts` | 1,658 | 85.2% | 79.1% |
| `experiments` | 4,389 | 85.7% | 82.0% |
| `policies` | 2,132 | 89.4% | 85.8% |
| `inspection` | 769 | 91.2% | 88.0% |

利用者の経路で、通っていない処理が多いのは次の箇所です。テストを足す候補として記録します。

- Project画面のAPI：`web/project_services.py`の`get`（HTTPのGETの経路。205行中167行が通っていない）、`web/project_queries.py`の`first_divergence`・`compare`・`history`。→ project-round3.jsが送る証跡・比較・実験の要求を、生成ScenarioのRunで確かめるテストを足した（`integration/test_project_evidence_routes.py`、`integration/test_project_experiments.py`）。不具合は見つからなかった。`project_queries.py`は80%、`project_services.py`は46%（行と分岐）になり、全体は74.0%になった。残りは契約の一覧、図面の取り込みと承認、評価の読み取りの一部。
- 避難者の一時外出・帰還・移動・再会（`domain/population_lifecycle.py`の`apply_population_presence_command`、`_schedule_relocation`、`admit_pending_returns`など）。→ 生成Scenarioで移動・再会・一時外出・帰還を確かめるテストを足した（`integration/test_population_presence.py`）。不具合が3つ見つかり、それぞれ`xfail`で置いた：支援を要請中の人の再会でシミュレーションが止まる（#179。直して印を外した）、帰還先へ移動中の人を数えず定員を超えて止まる（#180。到着にも同じ不具合があり、あわせて直して印を外した）、一時外出・移送が計画では通るのに実行時に失敗する（#181。手が空くまで延期するように直して印を外した）。`population_lifecycle.py`は行74.1%（行と分岐68%）、全体は74.3%になった。帰還先が満員のときに待たせる処理（`admit_pending_returns`）は、#180を直したあとのテストで通る。
- イベントの処理：近接接触（`ProximityContactHandler`）、移動の完了（`AgentTravelCompletionHandler`）の一部、職員の不在（`StaffUnavailabilityHandler`）、施設の復旧（`FacilityRecoveryHandler`）、情報の噂・訂正・接触の一部。→ 通っていない行の多くは、壊れたpayloadを拒否する防御的な検査で、利用者の経路からは届かない。利用者の経路として、Project画面のフォームで選べる近接接触のモデルと、フォームで入れる運営の条件（施設の停止・職員の不在）を生成Scenarioで確かめた。不具合が2つ見つかり、`xfail`で置いた：近接接触のモデルを選ぶとCheckpointの保存が拒否されて止まる（#187。モデルの署名をメールアドレスと誤判定する。直して印を外した）、使用中の施設への停止の条件が失敗して最後まで効かない（#188。停止を予定の時刻に効かせ、使用中の人は使い終えるように直して印を外した）。職員の不在の条件（繰り返しを含む）は正しく動くので、テストを足した（`integration/test_staff_unavailable_conditions.py`）。職員の自律的な作業（清掃・巡回）は、生成Scenarioの6時間では一度も始まらず、不在による作業の中断の分岐には届いていない。
- EffectProgramの検証で、不正な入力を拒否する分岐（`events/effects.py`の`validate_effect_program`で115行）とguardの評価。LLMの出力を信頼しない入力として扱うための検査なので、拒否の分岐も確かめる価値がある。→ 生成Scenarioで、操作17種のそれぞれに7種類の誤り（対象の誤り・時刻の誤り・未知のパラメーターなど）を入れて分岐させ、すべて理由付き（`BranchError`か契約の検証エラー）で拒否され、ほかの例外で落ちないことを確かめた（`integration/test_effect_program_validation.py`）。guardの評価も確かめ（`integration/test_effect_program_guards.py`）、不具合が3つ見つかったので`xfail`で置いた：図面の変更の介入のguardが対象を見つけられず失敗する（#194。直して印を外した）、環境の変更でguardにより開始が抑制されると期限切れが失敗として記録される（#195。直して印を外した）、`eq`・`ne`が整数と小数を型で比べる（#196。直して印を外した）。`validate_effect_program`の通っていない行は107→93行、guardの評価は42→21行、全体は74.9%になった。
- 図面の取り込み（`region/plan_import.py`が31.6%、`region/plan_extract.py`はテストがなく0%）。→ 合成の避難所の図面（`tests/support/plans.py`。画像とGeoJSON）を、Project画面と同じ経路で取り込んだ。画像を手でトレースしたものとGeoJSONのどちらも、承認してScenarioに指定すると開始でき、Checkpointから同じ世界に再開できることを確かめた（`integration/test_imported_plan_scenarios.py`）。抽出の候補（壁・扉・PDFのページ）は`unit/region/test_plan_extract.py`、取り込みの入力の誤り（トレース20種類、要求16種類、GeoJSON30種類など）が理由付きで拒否されることは`unit/web/test_plan_services.py`で確かめた。不具合が3つ見つかったので`xfail`で置いた：外壁に接する部屋が部屋の候補にならない（#200。直して印を外した）、取り込んだ図面では職員が1つのZoneに集められ、給水と配食が一度も始まらない（#201。直して印を外した）、GeoJSONの型の誤りなど7種類が理由なしで落ちる（#202。直して印を外した）。あわせて、拒否の理由が画面に届かない（応答が一律に「invalid project request」になる）不具合も見つけ、#202と一緒に直した（#204）。`plan_import.py`は65.6%、`plan_extract.py`は86.0%、`web/plan_services.py`は48%→81.3%になり、全体は76.7%になった。
- 外部データの接続（`region/additional_providers.py`、`region/providers.py`）。外部サービスを呼ぶ部分は、偽の応答で確かめる必要がある。

## 移管時の検証記録

採用したsource snapshot a162aed8284641c2a21e21bbcd04bad6e595ffb1で、CPython 3.12.14と固定dependencyを使ってoffline pytest全件を実行し、**579 passed、3 skipped**でした。所要時間は545.17秒です。ANTHROPIC_API_KEYとlive test gateは未設定で、有料Provider呼び出しはありません。

同じsource snapshotでRuffも確認しました。

- ruff check . は既存指摘1,906件: E501 1,758、E701 71、E702 40、I001 28、F401 6、F841 3。
- ruff format --check . は130個のPython fileを整形対象、87個は整形済みと報告しました。

これらのrepository全体のstyle指摘は文書整理以前から存在し、#139の一括整形で解消しました。文書整理ではsource/testの動作を変更していません。GitHub Actionsはdispatchしていません。二つのworkflowは手動実行のままです。

## 主なテスト範囲

testは決定的state遷移、Checkpoint serializationと復元、独立Branch、event順、施設・資源・職員操作、履歴・timeline検索、model provenance、LLM fallbackとresponse tape、Project永続化、Web route、Demo成果物を確認します。テストは`tests/`の下に範囲ごとに置きます（[テストの置き場所と名前](#テストの置き場所と名前)）。どのファイルが何を確かめるかは、下の[テストの地図](#テストの地図)を参照してください。

Project画面の作用説明（`project-round3.js`の`renderEffectExplanation`）は、`tests/unit/web/test_static_project_round3.py`がその関数だけをnodeで実行して確認します。fallback時の理由表示などが対象です。nodeがない環境ではskipします。

保存したRunのinspectorの地図の注記（`app.js`の`mapNote`）は、`tests/unit/web/test_static_app.py`が同じ方法で確認します。地図に描けない区画にいる人の数と区画が出ることが対象です（#161）。nodeがない環境ではskipします。

demo-v2の地図描画は、`tests/integration/test_installed_facility_rendering.py`が確認します。timeline routeが返すframe（保存済み境界のframe cacheと、境界以外の再構成）をnodeで`demo-v2-scene.js`・`demo-v2-sprites.js`・`demo-v2-map.js`に渡し、描画命令を受け流すcanvasの代わりを使って、`HinanjoMap.draw`が返す施設アイコンの記録（hit）を検査します。介入で追加したトイレが実座標の地図と模式図の両方で別々の位置に描かれること、座標のない施設は実座標の地図に描かれないことが対象です。模式図は施設を実座標と無関係に並べるため、模式図に描かれても図面上の位置があるとは限りません。画素の見た目（色・重なり・文字）は確認しません。nodeがない環境では描画のtestだけをskipします。

## テストの置き場所と名前

テストは範囲（単体・統合・e2e）ごとにディレクトリを分けます。範囲の定義は[テストの地図](#テストの地図)と同じで、1つのテストが通る一番広い範囲で決めます。

| 置き場所 | 範囲 | 名前 |
|---|---|---|
| `tests/unit/<担当>/` | 単体：`TickEngine`で時間を進めず、HTTP・CLIも通さない | `test_<srcのモジュール名>.py`。例：`src/sheltersim/domain/facility_queues.py`なら`tests/unit/domain/test_facility_queues.py` |
| `tests/integration/` | 統合：`TickEngine`で時間を進めるか、HTTP・CLIを通す（どちらか一方） | `test_<機能>.py`。例：`test_toilet_service.py`、`test_branch_interventions.py` |
| `tests/e2e/` | e2e：HTTPかCLIから入り、時間も進める | `test_<利用者の流れ>.py`。例：`test_project_scenario_to_results.py` |
| `tests/live/` | 有料APIを呼ぶテスト（`live_api`の印） | `test_live_api.py`だけ。APIキーを使えるようにするゲート（`sheltersim.llm.config`）が、このパスのテストにだけ開くため |

- `<担当>`は、AGENTS.mdのモジュール（`core`、`domain`、`events`、`policies`、`experiments`、`region`、`llm`、`storage`、`web`）と`cli`、それにリポジトリ自体（テストの配置、文書、fixture、出典台帳）を確かめる`repo`です。
- 名前に、不具合名（例：`resource_loss_without_operational_state`）、開発の回の名前（例：`round2_common_services`）、`regressions`を使いません。不具合の再発防止テストはその機能のファイルに足し、守るべき振る舞いを関数名に書き、Issue番号をdocstringかコメントに書きます。
- ブラウザー用の静的ファイル（JavaScript、HTML）を確かめる単体は、Pythonのモジュール名がないので`tests/unit/web/test_static_<ファイル名>.py`にします。例：`demo-v2.js`なら`test_static_demo_v2.py`。
- 同じ名前のファイルを別のディレクトリに置けるよう、pytestは`--import-mode=importlib`で動かします（`pyproject.toml`）。テストファイル同士ではimportできないので、共有する準備は`tests/support/`の題材ごとのモジュールにfixtureとして置きます（下の「共有する準備の置き場所」）。テストファイルはfixtureとして受け取り、`tests/support/`のモジュールを直接importしません。例外は`tests/support/paths.py`の定数で、`fixtures/`のファイルのパス（`SCENARIO_100_AGENTS`、`SMALL_REPLAY`など）は`from support.paths import …`で使い、テストファイルの中で`Path(__file__).parents[N]`から組み立てません（例：`project_store_at`は空のproject`project`を持つ`ProjectStore`を開き、`counting_provider`は呼ばれた回数を数える偽のLLM Providerを返します）。
- 背景で動くjob（`DemoJobManager`、`ProjectAPI`）の終わりは、実時間の短い期限で状態を読み続けて待ちません。CPUが混むと期限を超えて失敗するためです（#143）。どちらもworkerが1つなので、`wait_for_job`のfixture（`DemoJobManager`）か、`api._pool.submit(lambda: None).result(timeout=120)`（`ProjectAPI`）で、それまでのjobが終わるのを待ってから状態を確かめます。`timeout`は止まったときにテストを終わらせるためだけのものです。
- 何を確かめるか（結果・契約・画面）は、ディレクトリにも印にもしません。実行の選び分けに使わない印は、付け忘れても気づけず、すぐ実態とずれるためです。確かめる振る舞いを関数名に書き、全体は地図の「目的」欄で把握します。

### 共有する準備の置き場所

共有するfixtureは、`tests/support/`に題材ごとのモジュールとして置き、`tests/conftest.py`の`pytest_plugins`で読み込みます（#138）。`pyproject.toml`の`pythonpath`に`tests`を入れているので、`support.<モジュール名>`として読み込めます。`tests/conftest.py`に残すのは、すべてのテストに効く設定（ローカルの状態の隔離と、並列実行のときの長いテストの並べ替え）と、結果を残すplugin `tests/results_log.py`の読み込みだけです。新しいfixtureは、題材が合うモジュールに足します。合うモジュールがなければ、新しいモジュールを作って`pytest_plugins`に加え、この表にも足します。

| モジュール | 題材 | 主なfixture |
|---|---|---|
| `support/generated.py` | UIで作る生成ScenarioとProject画面のAPI | `generated_scenario`、`generated_scenario_with`、`generated_previews`、`ui_project_api`、`place_three_at_facility`、`add_toilets_program` |
| `support/fixture_scenarios.py` | `fixtures/`のScenarioを基に、1つの仕組みを確かめるために組み立てたScenario | `small_replay_scenario`、`small_benchmark_scenario`、`resource_service_scenario`、`information_spread_scenario`、`research_psychology_scenario` |
| `support/demo.py` | 旧UIのdemo（`web/demo.py`）と、その計画・HTTP | `small_replay_demo`（`paused`、`offline_compiler`で開始・一時停止とofflineのEvent Compilerを切り替え）、`failure_day3_demo`（`start`、`events_due_minute`）、`day3_demo`、`day4_plan`、`multi_plan_draft`、`wait_for_job`、`demo_world_hash` |
| `support/storage.py` | Projectの保存、保存済みRunとCheckpoint、ほかのOSでの保存 | `project_store_at`、`saved_timeline`、`no_checkpoint_decode`、`windows_like` |
| `support/web.py` | 保存したRunのinspectorと、socketのないrequest handler | `demo_timeline_inspector`、`audit_inspector`、`full_trace_inspector`、`bare_request_handler` |
| `support/llm.py` | 偽のLLM Providerと、有料APIのライブの確認 | `counting_provider`、`recording_router`、`live_model_check`、`live_source_search_check` |
| `support/reports.py` | PDFレポートの保存済みRun、根拠つきの文章、偽のOpus | `report_saved_run`、`prose_document`、`report_router`、`install_report_router` |
| `support/plans.py` | 取り込む図面：合成の避難所の図面（画像・GeoJSON）と、手で描いたそのトレース | `shelter_trace`、`upload_plan`（ほかに`SHELTER_TRACE`、`shelter_plan_png`、`shelter_plan_geojson`、`rooms_plan_png`） |
| `support/world.py` | WorldState・イベント・検証の部品 | `make_event`、`record_handler`、`stress_delta`、`count_validations`、`live_checkpoint_world` |

`support/paths.py`（`fixtures/`のファイルのパス。テストファイルからもimportします）と`support/isolation.py`（隔離の処理）は、fixtureを持たない部品です。ほかのテストで使う`fixtures/`のファイルが増えたら、`support/paths.py`に定数を足します。

関数を返すfixtureは、呼ぶたびに新しいものを作るだけで状態を持ちません。そのうち`saved_timeline`、`add_toilets_program`、`demo_world_hash`、`recording_router`はsession単位にして、module単位のfixtureからも使えるようにしています。テストファイルの中のhelperがこれらを使うときは、テストが受け取ったfixtureを引数で渡します（例：`integration/test_intervention_outcomes.py`の`_main_and_child`）。

### 時刻を手で動かすテスト

単体テストでは、handlerの反応を確かめるために、テスト自身で時刻を動かして`EventDispatcher.dispatch_due`を直接呼んでかまいません。ただし次を守ります。

- tickの順序やモジュールの組み合わせで決まる振る舞い（「シミュレーション中にこうなる」）は、`TickEngine`で時間を進める統合テストでも確かめます。時刻を手で動かす単体テストを足すときは、対になる統合テストを足すか、既存の統合テストの名前をコメントに書きます。
- KPI、比較、レポートを確かめるために、イベント記録や履歴を手で書き込んで実行結果の代わりにしません。共通fixture（`generated_scenario`）を実行して作ります。

### 既存のテスト

決まりを作る前に`tests/`直下にあったファイルは、#126で担当と範囲ごとにすべて移しました。ファイルごとに`git mv`だけのcommitを先に作り、内容の変更を次のcommitにしているので、`git log --follow`で元のファイルの履歴をたどれます。行き先のファイルがすでにあった場合は、移して足す1つのcommitにしています。

`tests/unit/repo/test_test_layout.py`は、`tests/`直下のテストファイル、決まった場所以外のテストファイル、`tests/live/`以外での`live_api`の印を見つけると失敗します。`scripts/`のファイルに書かれたテストファイルのパスが存在しないときも失敗します（#146）。

## 利用者の経路を通る共通fixture

UIで作るScenarioは、必ず図面（`facility_geometry`）と認知（`agent_dynamics`）を持ちます。一方、`fixtures/`のScenarioはどちらも持ちません。#51の不具合は図面と認知がある場合にだけ起き、fixtureを使う多くのテストをすり抜けました。新しいテストは、特に理由がなければ`tests/support/generated.py`の共通fixtureを使ってください。

- `generated_scenario`：`ScenarioSpec`をテストごとに新しく作って返します。テスト内で変更しても、ほかのテストには影響しません。
- `generated_scenario_preview`：元になったpreviewの結果（`scenario`、`approved_hash`、`can_start`、`request`など）です。session単位で1回だけ作るので、変更してはいけません。module単位のfixtureから使うときは、`copy.deepcopy(generated_scenario_preview["scenario"])`から`ScenarioSpec`を作ります。
- `generated_scenario_with(**fields)`：画面の入力欄の一部を変えたときのScenarioを、テストごとに新しく作って返します。たとえば`generated_scenario_with(duration_days=4)`は、期間の欄に4と入力した場合です。変えた欄は利用者の入力として扱います。
- `generated_previews(**fields)`：上のpreviewの結果を返します。session単位のfixtureで、同じ入力のpreviewは1回だけ作って共有するので、変更してはいけません。
- `ui_project_api`と`generated_request(**fields)`：作ったpreviewにさらに画面の操作（世帯の修正、承認など）を続けるテスト用です。`ui_project_api`は、空の地域調査に差し替えた`ProjectAPI`（project名`project`）をテストの間だけ開いておきます。`generated_request`は、上と同じ入力欄の値を返すので、`ui_project_api.workspace.post(["project", "scenario-preview"], {"text": "", "request": generated_request(...)})`でpreviewを作ります。
- `generated_scenario_with`などには、画面にない`InitialScenarioRequest`の項目（`seed`、`operational_conditions.scheduled`、`population_conditions`など）も渡せます。previewが受け取る項目なら、そのまま通ります。

共通fixtureは、demo-v2のScenario画面が送る入力（`GENERATED_SCENARIO_REQUEST`：合成市・地震・20人・0.25日。各項目は利用者の入力として扱います）を、UIと同じ`ProjectAPI`のpreviewに渡して作ります。違いは地域調査だけで、本物は公開Webサービスに接続するため、空の調査結果に差し替えています。20人・6時間の全体を実行しても、約6秒で終わります。

この共通fixtureの実行結果は、`fixtures/regression-contract.json`の`generated_small`が固定していて、`tests/integration/test_generated_scenario_contract.py`が確かめます（fixtureそのものは`tests/unit/repo/test_generated_scenario_fixture.py`）。生成手順やモデルを意図して変えたためにhashが変わった場合は、変えた理由をPull Requestに書いたうえで値を更新します。理由が説明できない変化は不具合として調べます。

### 介入の効果を確かめるテスト

介入が利用者の見る結果を変えることは、`tests/integration/test_intervention_outcomes.py`で確かめます。手順はどの介入も同じです。

1. 共通fixtureの本線を分岐させ、子Branchに介入を1つ適用します。
2. 両方を`TickEngine`で同じ時刻まで進め、Project画面のKPI（`MetricSnapshot`と`operational_kpi_values`）を比べます。
3. 変化が、記録されたイベント（介入の適用、施設や職員の動作）を通して起きていることも確かめます。

比べるのは変化の向きだけで、大きさは確かめません。係数の多くは実測で校正されていないためです。期待どおりに効かない介入や、生成Scenarioに適用できない介入が見つかったら、テストで隠さずに不具合のIssueにします（例：#65）。

- 不具合で今は確かめられない振る舞いは、期待する形でテストを書き、`pytest.mark.xfail(strict=True, raises=…)`にIssue番号を付けて置きます（#72、#74、#76、#78〜#80、#82〜#84、#118で使い、どれも直して印を外しました）。`raises`で失敗の種類を限るので、不具合を直したのに期待と違う結果になった場合も気づけます。直したら印を外します。
- 職員が待機中のときにしか出せない指示は、本線を先に進め、`TimelineStore.iter_worlds`で条件を満たす最初の時刻を探してから分岐させます（読み取りだけで、状態も乱数も変えません）。
- 作業の引き継ぎのように、別の介入で作った状態が前提になるものは、介入したBranchからさらに分岐させます。
- 仕切りや設備の移設のように、KPIに現れにくい空間の変化は、人の歩く経路（`movement_v1`の`journeys`）や移動の完了イベントが通る区画で確かめます。移動時間は分単位なので、数メートルの違いは時間に出ません。
- 区画の環境（騒音、明るさ、換気、プライバシー、気温、行動の起こりやすさ）は、続いている間に人の状態（疲れ、衛生の必要度、不満、ストレス）へ積み上がります。期限が切れる時刻に、まだ効いていることを確かめてから比べます。疲れ・衛生・不満はKPIにないので、全員の平均を直接比べます。

## 出典台帳と根拠IDの整合

AGENTS.mdは、新しい係数の根拠を`fixtures/source-manifest.json`に記録するよう求めています。`tests/unit/repo/test_source_manifest.py`は、`src/`に固定の文字列で書かれた仮定ID（`ASM-…`）と出典ID（`SRC-…`）を集め、台帳にも既知の不足の一覧にもないIDがあると失敗します。

- 実行時に組み立てるID（`SRC-PLAN-<digest>`、`ASM-POLICY-OVERRIDE-<hash>`など）と、`str.startswith`に渡す接頭辞は、IDとして扱いません。
- 既知の不足は2026-10-27時点の4分類で、テストファイルに一覧があります（#61）。
  - 別のファイルに記載があるが台帳にない仮定ID：3件（`fixtures/regression-contract.json`、関連文書）
  - どこにも記載がない仮定ID：56件（例：`ASM-NEED-WEIGHTED-PHYSICAL-QUEUE-V1`、`ASM-BOUNDED-RUNTIME-GEOMETRY-V1`）
  - 実行時に取得する公開データの出典ID：5件（国土地理院、e-Stat、国土交通省）
  - 利用者が入力した値の出典ID：6件
- 新しい仮定IDを一覧に足してはいけません。コードと同じ変更で台帳に記録します。台帳に記録する中身（出典、単位、上限、仮定、適用範囲）はモデル担当が決めます。
- 台帳に記録されたID、コードが使わなくなったIDが一覧に残っていると、テストが失敗します。一覧は減る一方です。
- 実行時のデータ提供元と利用者入力の出典IDを台帳に載せるかは、まだ決まっていません。新しく足す場合は、その分類の一覧に加え、理由をPull Requestに書きます。

## テストの地図

2026-10-27時点の`develop`（`64f7059`）で、`tests/`の全テストファイル104本（`tests/`直下103本と`tests/integration/`の1本）と、そこに含まれるテスト896件（parametrizeを1件ずつ数えた数）を分類しました。#53の段階1（#57）の成果です。テストを足したり移したりしたら、この表も直します。その後に足したテストも表と集計に加えています（#58で1本・3件、#59で1本・4件、#60で1本・4件、#61で1本・5件、#66で8件、#67で5件、#68で6件、#69で6件、#60の残りで1件、#76・#78・#82で2本・5件、#91で1本・3件、#72で1件、#74で1本・1件、#79で1本・2件、#83で1本・3件、#118で3件、#87で1本・1件、#65で7件、#75で3件、#136で1本・6件、#146で1件、#148で1件、#152で1件、#137で10件、#157で1件、#169で3件、#159で2件、#160で2件、#161で1本・5件、Project画面のAPIで2本・22件、避難者の移動・再会で1本・5件、#180で1件、#181で1件、イベントの処理で3本・3件と単体1件、#187で1件、#188で1件、#190で2件、EffectProgramの検証で2本・16件、#194で2件、#196で5件、図面の取り込みで3本・80件、#201で2件、#202・#204で3件）。#59でライブテストを`tests/integration/`から`tests/live/`へ移しました。#88では、既存のファイルを共通fixtureに寄せながら新しい置き場所へ移しています（`test_facility_orientation.py`、`test_geometry_config_cache.py`、`test_installed_facility_rendering.py`、`test_nl_opus.py`、`test_research_psychology_default.py`、`test_resource_loss_without_operational_state.py`、`test_model_evaluation.py`、`test_physical_queue_overflow.py`、`test_queue_service_while_repositioning.py`、`test_resource_queue_regressions.py`、`test_household_correction.py`、`test_care_lifecycle.py`、`test_region_water_outage_conditions.py`、`test_service_capacity.py`、`test_standard_provisioning.py`。範囲が混ざっていたものは、単体と統合に分けました）。`test_round2_common_services.py`からは、生成器を使う1件だけを3件に分けて切り出しました。#75では、`test_staff_task_order_effect.py`を`unit/domain/test_staff_operations.py`へ移しました。残りは#115で、`test_round3_common_services.py`と合わせて担当と範囲ごとに分けました（新しいファイル11本と、既存の`unit/web/test_workspace.py`・`unit/domain/test_population_lifecycle.py`。確かめる内容は変えていません）。`test_staff_support_travel.py`は、生成された図面に迂回経路を1本だけ手で足して確かめています。`test_facility_queues.py`、`test_population_demand_integration.py`、`test_effect_program.py`も移しました。図面のない部品のテスト（介入の事前検証など）と、生成器が作らない形が要るテストは、fixtureのまま理由を書いて残しています。#126では、残りの`tests/`直下のファイルを担当ごとにまとめて移しています。core・events・regionの7本は、範囲と確かめているモジュールごとに17本へ分けました（新しいファイル14本と、既存の`unit/core/test_world.py`・`unit/core/test_checkpoint.py`・`unit/events/test_models.py`）。`test_state.py`のうち、ScenarioSpecの検査は`unit/experiments/test_scenario.py`へ、AgentStateの検査は`unit/domain/test_models.py`へ移したので、件数はexperimentsとdomainの行に数えています。domainの14本は、新しいファイル13本と、既存の`unit/domain/test_agent_dynamics.py`・`test_geometry_overlay.py`・`test_staff.py`・`test_staff_operations.py`へ移しました。`test_resource_ledger_state_check.py`は`unit/domain/test_resources.py`へ、`test_staff_operation_relief.py`は`unit/domain/test_staff_operations.py`へまとめています。policiesの9本は、新しいファイル12本と、既存の`unit/policies/test_registry.py`・`integration/test_research_psychology.py`などへ移しました。`test_policy_parameter_overrides.py`は、生成器の要求が実行時とCheckpointに届くことを確かめるので`unit/experiments/test_initial_scenario.py`へ、`test_route_bottleneck.py`の経路編集・施設移設のBranchは`unit/experiments/test_branch.py`へ移し、件数はexperimentsの行に数えています。experimentsの前半8本は、新しいファイル13本（うち`repo`の2本）と既存の`unit/experiments/test_branch.py`・`test_comparison.py`へ移しました。共通fixtureとScenarioのfixtureファイルそのものを確かめる単体3件は、担当を`repo`にしています。後半7本のうち、出典台帳と実装・fixtureの整合を確かめる`test_provenance.py`は`unit/repo/`へ移しました。`test_sensitivity.py`と`test_startable_generated_scenarios.py`は、既存の比較・trace level・生成器・施設のファイルへ分けて入れています。llmの11本は、新しいファイル9本と、既存の`unit/llm/test_natural_language.py`・`test_accounting.py`、`unit/web/test_workspace.py`、`unit/experiments/test_metrics.py`へ移しました。同じモジュールを確かめるファイルは1本にまとめています（`test_adaptive_event_eval.py`と`test_nl_parser.py`は`test_natural_language.py`へ、`test_complex_interactions.py`と`test_llm_runtime.py`は`test_runtime.py`へ、`test_llm_review.py`と`test_story_report.py`は`test_report.py`へ）。storage・cliの7本は、新しいファイル6本と、既存の`unit/storage/test_projects.py`・`test_checkpoints.py`・`test_query_cursors.py`、`e2e/test_cli_run.py`へ移しました。`e2e/test_cli_run.py`はCLIでの実行の流れなので、担当を`cli`にしています。`test_inspect_cli.py`の行にあった「手で時刻」の6件は統合のテストの数でした（定義は単体だけ）ので、移した先では数えていません。webの前半10本は、新しいファイル16本と、既存の`unit/llm/test_natural_language.py`・`unit/policies/test_psychology.py`・`test_behavior.py`へ移しました。ブラウザー用の静的ファイルを確かめる単体は、Pythonのモジュール名がないので`unit/web/test_static_<ファイル名>.py`にしています。webの後半9本は、新しいファイル17本と、既存の`unit/web/test_demo.py`へ移しました。これで`tests/`直下にテストファイルはなくなりました。空になった`UNSORTED`と、それが直下のファイルと一致することを確かめていたテスト1件は消したので、テストは972件です。#137では、統合・e2eでfixtureのScenarioを使うテストのうち、図面と認知の有無で通る処理が変わるものを、共通fixtureへ置き換えています（再開・分岐・seek・trace levelの4本、frameの2本、給水・配食の1本、比較の2本は済み）。Cは振り分けをIssueに記録し、Aになったものを置き換えています（情報拡散・ストレスの伝播・心理の回復、支援の優先度の再生、PDFレポートの介入、Project画面での文の介入（demoの`test_nl_demo.py`・`test_nl_demo_branching.py`の対）、行動Policyの選択は済み。回復と介入の数量の`xfail`は、#159・#160を直して外した）。行動Policyの比較は、生成Scenarioでは比べられない不具合（#157）を直したので、生成Scenarioのテストの`xfail`を外しました。モデル一式のうち個別に指定していない領域があるScenarioのテストも足し、#169で実行時のモデルを宣言に揃えてからは、比べられることと、#169より前に記録したRunを拒否することを確かめています。fixtureのテストも残しています。比較のKPIをイベント記録の手書きで確かめていた単体1件は、demoの実行で確かめる統合テスト（`integration/test_web_demo.py`）に移しました。demoは同梱の体育館の配置しか扱えないため、fixtureのままです。見えた穴の説明は#57時点の分析で、対応が済んだものには印を付けています。

### 分類の軸

- **範囲**：テストがどこからどこまでを動かすか。テスト1件ごとに決め、ファイルごとに件数を数えます。
  - 単体：シミュレーションの時間を`TickEngine`で進めず、HTTP・CLIも通さない。
  - 統合：`TickEngine`で時間を進める（runner、Branch、timelineを通す場合を含む）か、HTTP・CLIを通すか、どちらか一方。
  - e2e：HTTPかCLIから入り、`TickEngine`で時間も進める。
- **手で時刻**：単体のうち、`TickEngine`を使わずにテスト自身が時刻を動かし、`EventDispatcher.dispatch_due`を直接呼ぶテストの件数。tickの順序（AGENTS.mdの不変条件）を通らないので、部品ごとには正しくても組み合わせると壊れる不具合（#51）を見逃します。
- **担当**：主に確かめているモジュール。AGENTS.mdの区分に加え、`cli`（`sheltersim.cli`と`__main__`）、リポジトリ自体を確かめる`repo`、複数モジュールにまたがって主担当を決められない`横断`を使います（#115の後は該当するファイルがありません）。
- **目的**：範囲とは別に、何を確かめるファイルかを表す印です。1つのファイルに複数付くことがあります。
  - 結果：KPI、比較レポート、画面データ（frame・timeline）、PDF、説明文など、利用者が目にする出力の値や内容。
  - 契約：同じseedで同じ結果になること、Checkpointからの復元、スキーマ版と互換、HTTP API・CLIの入出力、出典台帳とprovenance、秘匿情報の扱い。
  - 画面：nodeでブラウザー用のJavaScriptを動かす、または画面用の静的ファイルやHTMLの配信。
- **Scenario**：テストが使うScenarioの出どころ。
  - fixture：`fixtures/`のScenario（`src/sheltersim/web/examples/failure_day3.json`の同梱版を含む）。読んだ後に一部を書き換えるものも含みます。どれも`model_configs`に`facility_geometry`も`agent_dynamics`も持ちません。
  - 生成：`InitialScenarioBuilder`で作ったScenarioを実行する。UIで作るScenarioと同じく、図面（`facility_geometry`）と認知（`agent_dynamics`）を持ちます。`integration/test_facility_orientation.py`と対になる`unit/domain/test_agent_dynamics.py`は、比べるために認知を外した生成Scenarioも作ります。
  - 生成（実行なし）：生成したdraftを検査するだけで、実行しない。
  - fixture＋図面：fixtureに図面だけを手で足して実行する。認知は持たず、UIでは作られない組み合わせです。
  - 手書きRun成果物：テスト内で書いたRun成果物のファイル（`manifest.json`など）を読む。
  - コード内で組立：テスト内でScenarioを一から組み立てる。
  - なし：Scenarioを使わない。
- **印**：pytestの印（`live_api`、`long_simulation`）。

範囲、手で時刻、Scenarioの欄は、全件のoffline実行中にテスト1件ごとの呼び出しを記録して決めました。記録したのは`TickEngine.step`、HTTPの入口（`_dispatch_api`とrequest handlerの`do_GET`・`do_POST`）、CLIの入口（`sheltersim.cli.main`と、`sheltersim`を起動する子プロセス）、`EventDispatcher.dispatch_due`、`ScenarioSpec.load`、`InitialScenarioBuilder.build`、`WorldState.from_scenario`です。fixtureの準備中に起きた呼び出しは、そのfixtureを使う各テストに数えています。`live_api`の4件は実行されないので範囲を数えていません（表では「—」）。担当と目的は、テスト名と本体を読んで決めた判断です。

### 一覧

| ファイル | 担当 | 単体 | 統合 | e2e | 手で時刻 | 目的 | Scenario | 印 |
|---|---|---|---|---|---|---|---|---|
| `unit/core/test_checkpoint.py` | core | 6 |  |  | 3 | 契約 | fixture |  |
| `integration/test_checkpoint_resume.py` | core |  | 1 |  |  | 契約 | 生成 |  |
| `unit/core/test_clock.py` | core | 1 |  |  |  | 契約 | なし |  |
| `unit/core/test_engine.py` | core | 1 |  |  |  | 契約 | なし |  |
| `unit/core/test_rng.py` | core | 2 |  |  |  | 契約 | なし |  |
| `integration/test_tick_engine.py` | core |  | 6 |  |  | 契約 | fixture | long_simulation |
| `unit/core/test_history.py` | core | 3 |  |  |  |  | なし |  |
| `integration/test_per_tick_history_validation.py` | core |  | 6 |  |  |  | 生成 |  |
| `unit/core/test_world.py` | core | 24 |  |  | 1 | 契約 | 生成（実行なし）・fixture |  |
| `unit/domain/test_care_receipts.py` | domain | 16 |  |  | 11 | 契約 | 生成（実行なし）・fixture |  |
| `integration/test_care_lifecycle.py` | domain |  | 1 |  |  | 契約 | 生成 |  |
| `unit/domain/test_agent_dynamics.py` | domain | 11 |  |  |  | 契約 | 生成（実行なし）・fixture |  |
| `integration/test_facility_awareness.py` | domain |  | 1 |  |  | 契約 | fixture |  |
| `integration/test_facility_orientation.py` | domain |  | 1 |  |  | 結果 | 生成 |  |
| `unit/domain/test_facility_queues.py` | domain | 1 |  |  |  | 契約 | 生成（実行なし） |  |
| `integration/test_facility_queues.py` | domain |  | 3 |  |  | 契約 | 生成 |  |
| `integration/test_geometry_config_cache.py` | domain |  | 1 |  |  | 契約 | 生成 |  |
| `unit/domain/test_models.py` | domain | 2 |  |  |  | 契約 | fixture |  |
| `unit/domain/test_information.py` | domain | 2 |  |  |  | 契約 | なし |  |
| `unit/domain/test_layouts.py` | domain | 3 |  |  |  | 契約 | fixture |  |
| `unit/domain/test_action_lifecycle.py` | domain | 3 |  |  |  |  | fixture |  |
| `unit/domain/test_distress_monitor.py` | domain | 5 |  |  |  |  | なし |  |
| `unit/domain/test_environment.py` | domain | 10 |  |  | 4 | 契約 | fixture |  |
| `unit/domain/test_facilities.py` | domain | 7 |  |  | 5 |  | fixture |  |
| `unit/domain/test_infrastructure.py` | domain | 10 |  |  | 9 | 契約 | fixture |  |
| `unit/domain/test_operational.py` | domain | 15 |  |  | 12 | 契約 | fixture |  |
| `unit/domain/test_resource_service.py` | domain | 23 |  |  | 5 | 契約 | fixture |  |
| `integration/test_resource_service.py` | domain |  | 4 |  |  | 契約 | 生成 |  |
| `unit/domain/test_resources.py` | domain | 14 |  |  | 3 | 契約 | fixture |  |
| `e2e/test_resource_ledger_inspection.py` | domain |  |  | 1 |  | 結果・契約 | fixture |  |
| `unit/domain/test_social_dialogue.py` | domain | 2 |  |  | 2 | 契約 | fixture |  |
| `unit/domain/test_movement.py` | domain | 6 |  |  |  |  | 生成（実行なし） |  |
| `integration/test_physical_queues.py` | domain |  | 1 |  |  | 結果 | 生成 | long_simulation |
| `unit/domain/test_population_lifecycle.py` | domain | 3 |  |  | 2 | 契約 | 生成（実行なし）・なし |  |
| `integration/test_queue_service.py` | domain |  | 1 |  |  | 結果 | 生成 | long_simulation |
| `integration/test_resource_loss.py` | domain |  | 1 |  |  | 契約 | 生成 |  |
| `integration/test_entered_stock.py` | domain |  | 1 |  |  |  | 生成 | long_simulation |
| `unit/domain/test_queueing.py` | domain | 4 |  |  |  |  | 生成（実行なし） |  |
| `unit/domain/test_common_operations.py` | domain | 2 |  |  | 1 | 契約 | なし・fixture |  |
| `integration/test_staff_support_travel.py` | domain |  | 1 |  |  | 契約 | 生成 |  |
| `unit/domain/test_staff_operations.py` | domain | 21 |  |  | 11 | 契約 | 生成（実行なし）・fixture |  |
| `unit/domain/test_geometry_overlay.py` | domain | 17 |  |  | 3 | 契約 | 生成（実行なし） |  |
| `integration/test_installed_facility_announcement.py` | domain |  | 3 |  |  | 結果 | 生成 |  |
| `integration/test_population_presence.py` | domain |  | 7 |  |  | 結果・契約 | 生成 |  |
| `integration/test_proximity_contacts.py` | domain |  | 2 |  |  | 結果・契約 | 生成 |  |
| `unit/domain/test_population.py` | domain | 10 |  |  | 6 | 契約 | 生成（実行なし）・fixture＋図面 |  |
| `unit/domain/test_staff.py` | domain | 16 |  |  | 3 | 結果・契約 | 生成（実行なし）・fixture |  |
| `integration/test_intervention_outcomes.py` | events |  | 31 |  |  | 結果 | 生成 |  |
| `integration/test_effect_program_geometry.py` | events |  | 1 |  |  | 契約 | 生成 |  |
| `integration/test_effect_program_guards.py` | events |  | 10 |  |  | 結果・契約 | 生成 |  |
| `integration/test_effect_program_validation.py` | events |  | 8 |  |  | 契約 | 生成 |  |
| `unit/events/test_handlers.py` | events | 8 |  |  | 8 |  | fixture |  |
| `unit/events/test_queue.py` | events | 2 |  |  | 1 |  | fixture・なし |  |
| `unit/events/test_effects.py` | events | 23 |  |  | 5 | 契約 | 生成（実行なし）・fixture |  |
| `unit/events/test_models.py` | events | 2 |  |  |  | 契約 | なし |  |
| `unit/policies/test_registry.py` | policies | 21 |  |  |  | 契約 | 生成（実行なし）・fixture |  |
| `integration/test_research_psychology.py` | policies |  | 4 |  |  | 契約 | 生成・fixture |  |
| `e2e/test_legacy_psychology_cli.py` | policies |  |  | 1 |  | 契約 | fixture |  |
| `unit/experiments/test_comparison.py` | experiments | 9 |  |  |  | 結果・契約 | なし・fixture |  |
| `unit/experiments/test_branch.py` | experiments | 10 |  |  |  | 契約 | fixture |  |
| `unit/policies/test_behavior.py` | policies | 9 |  |  |  | 契約 | fixture・コード内で組立 |  |
| `integration/test_behavior_policy.py` | policies |  | 2 |  |  | 契約 | fixture・生成 |  |
| `unit/policies/test_psychology.py` | policies | 17 |  |  | 1 | 契約 | fixture・コード内で組立 |  |
| `integration/test_causal_stress.py` | policies |  | 1 |  |  | 契約 | コード内で組立 |  |
| `unit/policies/test_information.py` | policies | 4 |  |  |  | 契約 | fixture |  |
| `integration/test_information_spread.py` | policies |  | 8 |  |  | 契約 | 生成・fixture |  |
| `unit/policies/test_social.py` | policies | 9 |  |  |  | 契約 | fixture |  |
| `integration/test_stress_propagation.py` | policies |  | 2 |  |  | 契約 | fixture・生成 |  |
| `unit/policies/test_space.py` | policies | 10 |  |  |  | 契約 | fixture |  |
| `unit/experiments/test_validation.py` | experiments | 12 |  |  |  | 契約 | fixture・生成（実行なし） |  |
| `integration/test_branch_forks.py` | experiments |  | 1 |  |  | 契約 | 生成 |  |
| `integration/test_comparison.py` | experiments |  | 2 |  |  | 結果・契約 | 生成 |  |
| `e2e/test_policy_comparison.py` | experiments |  |  | 3 |  | 結果・契約 | fixture・生成 |  |
| `integration/test_offline_run.py` | experiments |  | 4 |  |  | 契約 | fixture | long_simulation |
| `integration/test_exact_timeline.py` | experiments |  | 1 |  |  | 契約 | 生成 |  |
| `integration/test_generated_scenario_contract.py` | experiments |  | 1 |  |  | 契約 | 生成 |  |
| `unit/experiments/test_metrics.py` | experiments | 6 |  |  | 1 | 結果・契約 | fixture |  |
| `integration/test_trace_level.py` | experiments |  | 2 |  |  | 契約 | 生成 |  |
| `integration/test_operational_scenarios.py` | experiments |  | 3 |  |  | 結果 | fixture | long_simulation |
| `unit/experiments/test_benchmark.py` | experiments | 1 |  |  |  | 契約 | なし |  |
| `integration/test_benchmark.py` | experiments |  | 1 |  |  | 契約 | fixture |  |
| `unit/experiments/test_scenario.py` | experiments | 11 |  |  |  | 契約 | fixture |  |
| `unit/experiments/test_runner.py` | experiments | 4 |  |  | 4 | 契約 | 生成（実行なし） |  |
| `unit/experiments/test_initial_scenario.py` | experiments | 37 |  |  |  | 契約 | 生成（実行なし）・生成 |  |
| `unit/experiments/test_service_capacity.py` | experiments | 14 |  |  |  | 契約 | fixture・生成（実行なし） |  |
| `integration/test_service_capacity_cli.py` | experiments |  | 1 |  |  | 契約 | 生成（実行なし） |  |
| `unit/experiments/test_profiles.py` | experiments | 8 |  |  |  | 契約 | fixture |  |
| `unit/experiments/test_sanitation_diagnostics.py` | experiments | 1 |  |  |  |  | fixture |  |
| `unit/experiments/test_scenario_editor.py` | experiments | 7 |  |  |  | 契約 | fixture |  |
| `integration/test_scenario_editor.py` | experiments |  | 2 |  |  | 契約・画面 | fixture |  |
| `integration/test_timed_branch_plan.py` | experiments |  | 2 |  |  | 契約 | fixture | long_simulation |
| `unit/experiments/test_timeline.py` | experiments | 1 |  |  |  | 契約 | なし |  |
| `unit/region/test_plan_import.py` | region | 1 |  |  |  | 契約 | なし |  |
| `unit/region/test_plan_extract.py` | region | 5 |  |  |  | 契約 | なし |  |
| `integration/test_imported_plan_scenarios.py` | region |  | 3 |  |  | 結果・契約 | 生成 |  |
| `unit/region/test_providers.py` | region | 3 |  |  |  | 契約 | なし |  |
| `unit/region/test_research.py` | region | 4 |  |  |  | 契約 | なし |  |
| `unit/region/test_conditions.py` | region | 7 |  |  |  |  | 生成（実行なし） |  |
| `integration/test_water_outage_conditions.py` | region |  | 1 |  |  |  | 生成 |  |
| `integration/test_facility_outage_conditions.py` | region |  | 2 |  |  | 結果 | 生成 |  |
| `integration/test_staff_unavailable_conditions.py` | region |  | 1 |  |  | 結果 | 生成 |  |
| `integration/test_standard_provisioning.py` | region |  | 2 |  |  | 結果 | 生成 | long_simulation |
| `unit/region/test_facility.py` | region | 13 |  |  |  |  | 生成（実行なし） |  |
| `live/test_live_api.py` | llm | — | — | — | — | 結果・契約 | fixture | live_api |
| `unit/llm/test_natural_language.py` | llm | 73 |  |  | 12 | 契約 | fixture |  |
| `unit/llm/test_accounting.py` | llm | 10 |  |  |  | 契約 | なし |  |
| `integration/test_project_intervention_preview.py` | llm |  | 5 |  |  | 契約 | 生成 |  |
| `unit/llm/test_runtime.py` | llm | 16 |  |  | 6 | 契約 | fixture |  |
| `unit/llm/test_prepare.py` | llm | 4 |  |  |  | 契約 | fixture |  |
| `unit/llm/test_provider.py` | llm | 30 |  |  |  | 契約 | fixture |  |
| `unit/llm/test_report.py` | llm | 8 |  |  |  | 結果・契約 | fixture・なし |  |
| `unit/llm/test_outcomes.py` | llm | 3 |  |  |  | 結果・契約 | なし |  |
| `integration/test_llm_outcomes.py` | llm |  | 7 |  |  | 結果・契約 | fixture・生成（実行なし） |  |
| `integration/test_llm_review_run.py` | llm |  | 1 |  |  | 契約 | fixture |  |
| `integration/test_support_priority.py` | llm |  | 5 |  |  | 契約 | fixture・生成 |  |
| `integration/test_checkpoint_saves.py` | storage |  | 10 |  |  | 契約 | fixture |  |
| `unit/storage/test_frames.py` | storage | 1 |  |  |  | 契約 | fixture |  |
| `integration/test_timeline_frame_cache.py` | storage |  | 4 |  |  | 契約 | 生成 |  |
| `unit/storage/test_artifacts.py` | storage | 2 |  |  |  | 契約 | なし |  |
| `unit/storage/test_checkpoints.py` | storage | 2 |  |  |  | 契約 | なし・fixture |  |
| `unit/storage/test_experiments.py` | storage | 1 |  |  |  | 契約 | fixture |  |
| `unit/storage/test_projects.py` | storage | 25 |  |  |  | 契約 | fixture |  |
| `unit/storage/test_query_cursors.py` | storage | 2 |  |  |  | 契約 | なし |  |
| `unit/storage/test_usage.py` | storage | 5 |  |  |  | 契約 | なし |  |
| `integration/test_installed_facility_rendering.py` | web |  | 5 |  |  | 結果・画面 | 生成 |  |
| `unit/web/test_static_index.py` | web | 1 |  |  |  | 画面 | なし |  |
| `integration/test_admin_audit.py` | web |  | 3 |  |  | 契約 | fixture |  |
| `e2e/test_admin_audit_queries.py` | web |  |  | 2 |  | 契約 | fixture |  |
| `integration/test_demo_jobs.py` | web |  | 6 |  |  | 契約 | fixture |  |
| `e2e/test_demo_jobs_http.py` | web |  |  | 1 |  | 契約 | fixture |  |
| `integration/test_demo_live_recompute.py` | web |  | 1 |  |  | 契約 | fixture |  |
| `integration/test_demo_timeline.py` | web |  | 1 |  |  | 結果 | 手書きRun成果物 |  |
| `unit/web/test_inspector.py` | web | 5 |  |  |  | 結果 | 手書きRun成果物 |  |
| `integration/test_demo_v2_routes.py` | web |  | 8 |  |  | 画面 | なし |  |
| `unit/web/test_static_demo_v2.py` | web | 1 |  |  |  | 画面 | なし |  |
| `unit/web/test_static_project_round3.py` | web | 2 |  |  |  | 画面 | なし |  |
| `unit/web/test_static_app.py` | web | 2 |  |  |  | 画面 | なし |  |
| `integration/test_judge_demo.py` | web |  | 2 |  |  | 結果・契約・画面 | fixture |  |
| `unit/web/test_demo.py` | web | 6 |  |  |  | 契約 | fixture |  |
| `integration/test_multi_plan.py` | web |  | 1 |  |  | 契約 | fixture |  |
| `e2e/test_multi_plan.py` | web |  |  | 2 |  | 契約 | fixture |  |
| `integration/test_nl_demo.py` | web |  | 4 |  |  | 契約 | fixture |  |
| `e2e/test_nl_demo_branching.py` | web |  |  | 3 |  | 契約 | fixture |  |
| `integration/test_pdf_report.py` | web |  | 35 |  |  | 結果・契約 | fixture・生成 |  |
| `unit/web/test_pdf_report.py` | web | 3 |  |  |  | 結果 | なし |  |
| `e2e/test_pdf_download.py` | web |  |  | 1 |  | 契約 | fixture |  |
| `integration/test_pointer_only_reads.py` | web |  | 2 |  |  | 契約 | fixture |  |
| `unit/web/test_projects.py` | web | 3 |  |  |  | 契約 | fixture |  |
| `integration/test_replay_frames.py` | web |  | 13 |  |  | 契約 | 生成 |  |
| `unit/web/test_results_chat.py` | web | 3 |  |  |  | 契約 | なし |  |
| `integration/test_results_chat.py` | web |  | 1 |  |  | 契約 | fixture |  |
| `integration/test_resume_checkpoint_loads.py` | web |  | 1 |  |  | 契約 | fixture |  |
| `integration/test_web_demo.py` | web |  | 3 |  |  | 結果・契約 | fixture |  |
| `e2e/test_web_demo_story_review.py` | web |  |  | 1 |  | 契約 | fixture |  |
| `integration/test_web_inspector.py` | web |  | 3 |  |  | 結果・契約 | fixture・生成 |  |
| `integration/test_project_evidence_routes.py` | web |  | 19 |  |  | 結果・契約 | 生成 |  |
| `integration/test_project_experiments.py` | web |  | 3 |  |  | 結果・契約 | 生成 |  |
| `e2e/test_web_inspector_http.py` | web |  |  | 3 |  | 結果・契約 | fixture |  |
| `integration/test_web_routes.py` | web |  | 2 |  |  | 契約・画面 | なし |  |
| `unit/web/test_server.py` | web | 2 |  |  |  | 画面 | なし |  |
| `unit/web/test_frames.py` | web | 1 |  |  |  | 契約 | 生成（実行なし） |  |
| `unit/web/test_workspace.py` | web | 10 |  |  |  | 契約 | 生成（実行なし）・fixture・なし |  |
| `unit/web/test_project_queries.py` | web | 2 |  |  |  | 契約 | なし |  |
| `unit/web/test_plan_services.py` | web | 74 |  |  |  | 契約 | なし |  |
| `e2e/test_project_scenario_to_results.py` | web |  |  | 1 |  | 結果・契約 | 生成 |  |
| `integration/test_inspect_cli.py` | cli |  | 12 |  |  | 契約 | fixture |  |
| `e2e/test_cli_run.py` | cli |  |  | 4 |  | 結果・契約 | fixture |  |
| `integration/test_cli_entrypoint.py` | cli |  | 3 |  |  | 契約 | なし |  |
| `unit/cli/test_main.py` | cli | 1 |  |  |  | 契約 | なし |  |
| `integration/test_usage_cli.py` | cli |  | 2 |  |  | 契約 | なし |  |
| `unit/repo/test_source_manifest.py` | repo | 5 |  |  |  | 契約 | なし |  |
| `unit/repo/test_test_layout.py` | repo | 4 |  |  |  | 契約 | なし |  |
| `unit/repo/test_generated_scenario_fixture.py` | repo | 2 |  |  |  | 契約 | 生成（実行なし） |  |
| `unit/repo/test_scenario_fixtures.py` | repo | 1 |  |  |  | 契約 | fixture |  |
| `unit/repo/test_provenance.py` | repo | 7 |  |  |  | 契約 | fixture |  |
| `unit/repo/test_demo_replay_fixture.py` | repo | 1 |  |  |  | 結果・契約 | fixture |  |
| `unit/repo/test_results_log.py` | repo | 6 |  |  |  | 契約 | なし |  |

### 集計

担当ごとの件数です。「範囲が混ざるファイル」は、単体・統合・e2eのうち2つ以上を含むファイルの数です。

| 担当 | ファイル | 単体 | 統合 | e2e | 範囲が混ざるファイル | 生成Scenarioを実行するファイル |
|---|---|---|---|---|---|---|
| core | 9 | 37 | 13 | 0 | 0 | 2 |
| domain | 38 | 203 | 28 | 1 | 0 | 13 |
| events | 8 | 35 | 50 | 0 | 0 | 4 |
| policies | 12 | 70 | 17 | 1 | 0 | 4 |
| experiments | 25 | 121 | 20 | 3 | 0 | 7 |
| region | 11 | 33 | 9 | 0 | 0 | 5 |
| llm | 12 | 144 | 18 | 0 | 0 | 2 |
| storage | 9 | 38 | 14 | 0 | 0 | 1 |
| web | 41 | 115 | 113 | 14 | 0 | 7 |
| cli | 5 | 1 | 17 | 4 | 0 | 0 |
| repo | 7 | 26 | 0 | 0 | 0 | 0 |
| 計 | 177 | 823 | 299 | 23 | 0 | 45 |

1060件のうち単体が741件（70%）、統合が296件（28%）、e2eが23件（2%）です。どのファイルも範囲は1つだけで、単体だけが92本、統合だけまたはe2eだけが81本です（ほかに、実行しないライブテストが1本）。

fixtureのScenarioは次のとおり使われています（全件のoffline実行中に読まれた回数。読んだ後に書き換える分を含みません）。

| Scenario | 読まれた回数 | 内容 |
|---|---|---|
| `scenario_100_agents.json` | 168 | 100人・7日。`scenarios/baseline_100_7d.json`と同じIDだが、`model_configs`は`behavior`だけで、社会・職員・トイレの設定と仮定IDの一覧を持たない |
| `scenarios/h00_small_replay.json` | 36 | 小規模の再生用 |
| `scenarios/baseline_100_7d.json` | 32 | 100人・7日の基準 |
| `web/examples/failure_day3.json`（同梱版） | 31 | デモの既定。`scenarios/failure_day3.json`との一致は`unit/repo/test_provenance.py`が確かめる |
| `scenarios/`の運営イベント系6本 | 37 | `integration/test_operational_scenarios.py`、`unit/repo/test_scenario_fixtures.py`、`unit/experiments/test_scenario_editor.py`が使う |
| `scenarios/failure_day3.json` | 10 | 3日目の設備故障 |
| `scenarios/research_operational_100_7d.json` | 1 | `unit/experiments/test_service_capacity.py`だけが使う |
| `scenarios/research_shortage_100_7d.json` | 0 | テストからは読まれない。数理モデル文書の検証で使う |

### 見えた穴

1. **粒度がファイルごとにばらばら。** 104本中57本が、単体・統合・e2eのうち2つ以上を含みます。たとえば`test_pdf_report.py`は統合32件・単体1件・e2e 1件、`test_checkpoint_save_regressions.py`は単体17件・統合10件です。ファイル名からも置き場所からも範囲が分からず、新しいテストをどの粒度で書くかの手本がありません。→ #59で置き場所の決まりを作り、#126ですべてのファイルを範囲ごとに分けました。
2. **時刻を手で動かす単体テストが多い。** 22本のファイルにある126件が、`TickEngine`を使わずにテスト自身で時刻を動かし、イベントを直接処理させています（`test_operational_events.py`、`test_effect_program.py`、`test_adaptive_event_eval.py`で各12件など）。中には、比較のKPIをイベント記録を手で書き込んで確かめるテスト（`test_route_bottleneck.py`）もあります（#137で、demoの両Branchを実行して確かめる統合テストに直しました）。部品の検査としては有効ですが、同じ振る舞いをtickの順序どおりに通す統合テストが対になっていないものがあります。→ #59、#60
3. **e2eが少なく、利用者の主な流れを通していない。** e2eは20件で、使うScenarioはすべてfixtureです。Project画面の主な流れ（生成Scenarioのpreview → 承認 → 実行 → 介入 → 結果を見る）をHTTPから通すテストはありません。ProjectのHTTP routeを通るのは、PDFのダウンロード（`test_pdf_http_attachment_and_existing_origin_guard`）だけです。→ #58と#60ではHTTPを通していないので、#87で`e2e/test_project_scenario_to_results.py`を足しました。demo-v2の画面と同じrequestを本物のHTTPサーバーへ送り、共通fixtureと同じ入力欄の値でpreview → 承認 → 実行 → 文で入力した介入（避難者の到着）のpreviewと承認 → 両Branchの実行 → 比較までを通します。本線の結果が`fixtures/regression-contract.json`の`generated_small`と一致することも確かめます。
4. **利用者が見る結果を、利用者の経路で確かめるテストが少ない。** 目的に「結果」が付くファイル21本のうち、生成Scenarioを実行するのは5本（`test_installed_facility_announcement.py`（今の`integration/test_installed_facility_announcement.py`）、`test_installed_facility_rendering.py`、`test_physical_queue_overflow.py`、`test_queue_service_while_repositioning.py`、`test_standard_provisioning.py`）です。そのうち、介入して本線と比べるのはトイレ追加だけです。→ #60で、スタッフ追加・設備の故障・断水・物資搬入を`tests/integration/test_intervention_outcomes.py`に追加。#66で施設・職員・物資の残り（稼働の上限、修理、職員の不在・作業指示・引き継ぎ・担当変更、物資の遅延）を追加。#67で人口（到着、退所、一時外出、仮設スペース）を追加。#68で空間・経路（経路の閉鎖、施設の移動、設備の移設、仕切り、仮設の部屋）を追加。#69で環境（騒音、行動の起こりやすさ、換気、明るさ、プライバシー、気温）を追加。トイレ追加は、既存のテストが追加したトイレが使われることまでしか確かめていなかったので、本線との比較を足しました。
5. **experiments、storage、eventsを主担当とするファイルは、生成Scenarioを1本も実行しない**（#58で足した共通fixtureの検査`test_generated_scenario_fixture.py`（今の`integration/test_generated_scenario_contract.py`）と、#60で足した介入の結果テストを除く）。 Branch、比較、時系列、Project保存、EffectProgramを、図面と認知を持つ状態で確かめていません。webで生成Scenarioを実行するのも、描画の1本だけです。→ #58、#60。#137で、Branchの分岐、時系列のseek、trace level、保存したframe、frameの一括再生のファイルを生成Scenarioに置き換えました。
6. **UIでは作られない組み合わせで確かめているファイルがある。** `test_effect_program.py`、`test_facility_queues.py`、`test_population_demand_integration.py`、`test_staff_support_travel.py`は、fixtureに図面だけを足し、認知を持たない状態で実行します。→ #58で共通fixtureを用意。#88で4本とも移しました。生成器が作らない形（1つの施設に複数の列、迂回経路、到着の接近経路、清掃担当の職員）が要るテストは、生成された図面に手で足すか、理由を書いてfixtureのままにしています。
7. **再現性の固定が生成Scenarioにない。** `fixtures/regression-contract.json`が結果を固定するのは、`baseline_100_7d`と`small_replay`（どちらもfixture）だけです。→ #58で対応済み（`generated_small`を追加）
8. **生成Scenarioのテストが遅く、印もない。** 実行時間の上位5件のうち3件が生成Scenarioのテストです（`test_one_meal_station_keeps_up_with_one_hundred_people`約104秒、`test_staffed_water_station_serves_a_queue_longer_than_its_room`約58秒、`test_generated_scenario_with_entered_stock_completes_a_day`約52秒）。どれにも`long_simulation`の印がありません。新しいテストで生成Scenarioを気軽に使うには、小規模な生成Scenarioが要ります。→ #58で小規模な生成Scenarioの共通fixtureを用意済み。既存の遅いテストのうち、1件で30秒以上（直列）かかる4件（上の3件と`test_default_generated_scenario_serves_water_and_meals`）に`long_simulation`の印を付けました。
9. **名前から担当が分からないファイルがある。** `test_round2_common_services.py`（24件）と`test_round3_common_services.py`は、LLMの利用記録、保存、web projectionをまとめて扱っています。また、今の`tests/integration/`には有料APIのライブテストだけが入っていて、「統合」の意味では使われていません。→ #59で置き場所と名前の決まりを作り、ライブテストを`tests/live/`へ移しました。既存ファイルの改名は、移すときに行います。→ #115で2本を担当と範囲ごとに分け、`tests/`直下からなくしました。

## 確認できていないこと

- 今回、live有料Provider testは実行していません。
- Windows/macOSの実機上で起動確認していません。
- 30日間の性能結果は確立していません。過去の30人・30日試行は10分以内に完了しませんでした。
- 現在のbrowser証跡だけでは、全map layer、実施設図面取込、介護・会話・職員の組合せ、大規模人口を含む一連の操作を確認したとは言えません。
- 地域人口、避難所、天候、医療、結果の照合データを用いた外部検証はできていません。
- 心理、健康、社会、容量、incidentの係数は実測校正されていません。合成Branchの差は、条件付き出力であり、予測や現実の因果効果ではありません。

再現可能な1日性能値と環境はbenchmarks.md、モデル仮定とsource recordはmodel-validation.mdとfixtures/source-manifest.jsonを参照してください。
