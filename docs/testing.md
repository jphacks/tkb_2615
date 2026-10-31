# テストと検証

テストは、決定的な状態遷移、Checkpointの保存と復元、独立したBranch、イベントの順序、施設・資源・職員の操作、履歴と時系列の検索、モデルの出典、LLMのfallbackと応答テープ、Projectの保存、Webの経路、図面と地域データの取り込みを確かめます。

この文書には、決まりと手順だけを書きます。テストは変わり続けるので、件数・網羅率・計測時間のように変わる数は載せません。これまでの分析と経緯は、git履歴とIssue（テストの整理は#53・#57・#126、網羅の確認は#176・#178）を見てください。

## 通常のoffline確認

開発toolを導入し、次を実行します。

~~~shell
python -m pip install -e ".[dev]"
SHELTERSIM_LLM_MODE=offline python -m pytest -n auto
ruff check .
ruff format --check .
~~~

- `ruff check .`と`ruff format --check .`は指摘なしで通る状態を保ちます。1行100桁を超える文字列は、文言を変えずに、隣り合う文字列リテラルの連結に分けます。分けると値が変わる複数行の文字列（SQLiteの表定義など）だけ、閉じる引用符の行に`# noqa: E501`を付けます。
- テキストのファイルの読み書き（`Path.read_text`・`write_text`、`open`）と、`subprocess`の出力を文字列で受け取るとき（`text=True`）は、`encoding="utf-8"`を明示します。指定しないとOSの既定の文字コード（Windowsではcp1252やcp932）になり、日本語を読み書きできません。
- 一括整形のcommitは`.git-blame-ignore-revs`に登録してあります。手元の`git blame`で整形前の変更をたどるには、一度だけ`git config blame.ignoreRevsFile .git-blame-ignore-revs`を設定します。GitHubのblame表示はこのファイルを自動で使います。
- 有料APIを呼ぶテスト（`live_api`の印）は、`SHELTERSIM_ALLOW_LIVE_API_TESTS`と`ANTHROPIC_API_KEY`の両方を明示した場合だけ実行されます。通常の実行とCIでは設定しません。
- `tests/conftest.py`は、テストごとにProjectの保存先（`SHELTERSIM_PROJECTS_DIR`）、状態の保存先（`XDG_STATE_HOME`）、LLMのcache（`SHELTERSIM_LLM_CACHE_PATH`）を一時ディレクトリに向けます。並列のworkerがこれらを共有せず、開発者のRunやPC全体の利用記録にも書き込みません。テストの中で設定した値が優先されます。`live_api`のテストは、実費をPC全体の利用記録に残すため対象外です。

### 並列実行と長いテスト

- `-n auto`（pytest-xdist）で並列に実行します。`pyproject.toml`の`--dist worksteal`で、手の空いたworkerが残りのテストを引き取ります。
- `long_simulation`の印は、7日間のsimulationを実行するテストと、1件で30秒以上（直列）かかるテストに付けます。`tests/conftest.py`の`pytest_collection_modifyitems`が、並列のときにこれらを各workerの塊の先頭に1本ずつ置くので、長いテストは最初から並んで走ります。
- 開発中は`-m "not long_simulation"`で長いテストを除けます。印のない`pytest`は全件を実行します。`オフラインCI`（`.github/workflows/ci.yml`）は、mainへのPull Requestごとに自動で、または手動で、Linux・Windows・macOSの3つで長いテストを除いた全件を`-n auto`で実行します（lintとformatの確認はLinuxだけ）。3つのOSで動くことを確かめるためです（#48）。結果がそろうまで、Windowsで13〜15分ほどかかります。手動実行の`長時間simulation回帰`（`.github/workflows/long-regression.yml`）の`long`は、長いテストだけをLinuxで実行します。

### 結果の残り方と見方

pytestを実行するたびに、`tests/results_log.py`（`tests/conftest.py`が読み込むplugin）が、結果をリポジトリ直下の`.test-results/<UTCの日時>-<プロセスID>/`に書きます。`.test-results/`はgitに登録しません。全件でも一部でも、並列でも同じです。

- `summary.txt`：実行したコマンド、件数、失敗（FAILED）とエラー（ERROR）の一つ一つについてtracebackと捕まえた出力、時間のかかったテスト15件。実行の終わりに書きます。
- `results.jsonl`：テストの段階（setup・call・teardown）ごとに1行で、結果、時間、並列のworker、失敗ならtraceback。実行中に書き足すので、途中で止まった実行でもそこまでの結果が残ります。
- `.test-results/latest.txt`：いちばん新しい実行のディレクトリ名。ディレクトリは新しいものから20回分だけ残します。

出力の最後の行は、pytestの集計行のあとに足す次の1行です。失敗があれば、失敗したテストの名前（5件まで）と`summary.txt`の場所が入ります。

~~~text
test results: 1 failed, 967 passed, 4 skipped; failed: tests/unit/x/test_y.py::test_z; tracebacks in .test-results/20261028T010203Z-1234/summary.txt
~~~

失敗の原因は、もう一度実行せずに`summary.txt`で確かめます。`.pytest_cache`の失敗記録（`--lf`）は、次に全件が通ると消えるので、記録としては使いません。コーディングエージェントも、`| tail -1`などで集計行だけを見て判断しないでください。失敗があったら、この最後の行で名前を、`summary.txt`で原因を確かめ、テストの名前と原因を報告します。結果を残したくない実行では`-p no:results_log`を付けます。

`オフラインCI`は、OSごとの`.test-results/`を`test-results-<OS>`のartifactとして7日間保存します。失敗したときは、そのartifactの`summary.txt`で原因を確かめます。

### 網羅率の計測

網羅率（行と分岐）は、テストの届いていない処理を探すときだけ測ります。開発依存にpytest-covとcoverageを入れてあり、設定は`pyproject.toml`の`[tool.coverage.*]`です（対象は`sheltersim`、分岐も数える）。通常のpytestは計測しません。

~~~shell
SHELTERSIM_LLM_MODE=offline python -m pytest -n auto -m "not long_simulation" --cov --cov-report=html
python -m coverage report --sort=cover
~~~

- 結果はリポジトリ直下の`.coverage`と`htmlcov/`に書かれます（どちらも`.gitignore`の対象）。`htmlcov/index.html`で、ファイルごとに通っていない行と分岐が見えます。
- `long_simulation`の印のテストは外します。計測すると極端に遅くなる（全件で2時間を超える）一方、外して通らなくなる行はわずかです。
- 数字を上げること自体は目的にしません。通っていない処理のうち、利用者の操作から届くものを探し、その経路でテストを足します。

## テストの置き場所と名前

テストは範囲ごとにディレクトリを分けます。範囲は、1つのテストが通る一番広い範囲で決めます。

| 置き場所 | 範囲 | 名前 |
|---|---|---|
| `tests/unit/<担当>/` | 単体：`TickEngine`で時間を進めず、HTTP・CLIも通さない | `test_<srcのモジュール名>.py`。例：`src/sheltersim/domain/facility_queues.py`なら`tests/unit/domain/test_facility_queues.py` |
| `tests/integration/` | 統合：`TickEngine`で時間を進めるか、HTTP・CLIを通す（どちらか一方）。runner、Branch、timelineを通す場合を含む | `test_<機能>.py`。例：`test_toilet_service.py`、`test_branch_interventions.py` |
| `tests/e2e/` | e2e：HTTPかCLIから入り、時間も進める | `test_<利用者の流れ>.py`。例：`test_project_scenario_to_results.py` |
| `tests/live/` | 有料APIを呼ぶテスト（`live_api`の印） | `test_live_api.py`だけ。APIキーを使えるようにするゲート（`sheltersim.llm.config`）が、このパスのテストにだけ開くため |

- `<担当>`は、AGENTS.mdのモジュール（`core`、`domain`、`events`、`policies`、`experiments`、`region`、`llm`、`storage`、`web`）と`cli`、それにリポジトリ自体（テストの配置、文書、fixture、出典台帳）を確かめる`repo`です。
- 名前に、不具合名（例：`resource_loss_without_operational_state`）、開発の回の名前（例：`round2_common_services`）、`regressions`を使いません。不具合の再発防止テストはその機能のファイルに足し、守るべき振る舞いを関数名に書き、Issue番号をdocstringかコメントに書きます。
- ブラウザー用の静的ファイル（JavaScript、HTML）を確かめる単体は、Pythonのモジュール名がないので`tests/unit/web/test_static_<ファイル名>.py`にします。例：`demo-v2.js`なら`test_static_demo_v2.py`。nodeで関数を動かすテストは、nodeがない環境ではskipします。
- 何を確かめるか（結果・契約・画面）は、ディレクトリにも印にもしません。実行の選び分けに使わない印は、付け忘れても気づけず、すぐ実態とずれるためです。確かめる振る舞いは、関数名とファイルのdocstringに書きます。
- 背景で動くjob（`DemoJobManager`、`ProjectAPI`）の終わりは、実時間の短い期限で状態を読み続けて待ちません。CPUが混むと期限を超えて失敗するためです（#143）。どちらもworkerが1つなので、`wait_for_job`のfixture（`DemoJobManager`）か、`api.wait_idle(timeout=120)`（`ProjectAPI`）で、それまでのjobが終わるのを待ってから状態を確かめます。`timeout`は止まったときにテストを終わらせるためだけのものです。
- `tests/unit/repo/test_test_layout.py`は、`tests/`直下のテストファイル、決まった場所以外のテストファイル、`tests/live/`以外での`live_api`の印を見つけると失敗します。`scripts/`のファイルに書かれたテストファイルのパスが存在しないときも失敗します（#146）。
- 件数が要るときは、`pytest --collect-only -q <ディレクトリ>`で数えます。

### 共有する準備の置き場所

同じ名前のファイルを別のディレクトリに置けるよう、pytestは`--import-mode=importlib`で動かします（`pyproject.toml`）。テストファイル同士ではimportできないので、共有する準備は`tests/support/`に題材ごとのモジュールとして置き、`tests/conftest.py`の`pytest_plugins`で読み込みます（#138）。`pyproject.toml`の`pythonpath`に`tests`を入れているので、`support.<モジュール名>`として読み込めます。

- 状態や後片付けがある準備（保存先、API、偽のサービス、保存済みRunなど）は、fixtureにします。テストファイルはfixtureとして受け取ります。
- 状態を持たないものは、テストファイルから直接importしてかまいません。`fixtures/`のファイルのパス（`support/paths.py`の`SCENARIO_100_AGENTS`など。テストの中で`Path(__file__).parents[N]`から組み立てません）と、合成データの定数とそれを作る関数（`support/plans.py`の`SHELTER_TRACE`・`shelter_plan_png`、`support/public_data.py`の`MUNICIPALITY`など）です。
- `tests/conftest.py`に残すのは、すべてのテストに効く設定（ローカルの状態の隔離と、並列実行のときの長いテストの並べ替え）と、結果を残すplugin `tests/results_log.py`の読み込みだけです。
- 新しい準備は、題材が合うモジュールに足します。合うモジュールがなければ、新しいモジュールを作って`pytest_plugins`に加え、下の表にも足します。

| モジュール | 題材 | 主なfixture |
|---|---|---|
| `support/generated.py` | UIで作る生成ScenarioとProject画面のAPI | `generated_scenario`、`generated_scenario_with`、`generated_previews`、`ui_project_api`、`generated_request`、`place_three_at_facility`、`add_toilets_program` |
| `support/fixture_scenarios.py` | `fixtures/`のScenarioを基に、1つの仕組みを確かめるために組み立てたScenario | `small_replay_scenario`、`small_benchmark_scenario`、`resource_service_scenario`、`information_spread_scenario`、`research_psychology_scenario` |
| `support/demo.py` | 旧UIのdemo（`web/demo.py`）と、その計画・HTTP | `small_replay_demo`、`failure_day3_demo`、`day3_demo`、`day4_plan`、`multi_plan_draft`、`wait_for_job`、`demo_world_hash` |
| `support/storage.py` | Projectの保存、保存済みRunとCheckpoint、ほかのOSでの保存 | `project_store_at`、`saved_timeline`、`no_checkpoint_decode`、`windows_like` |
| `support/web.py` | 保存したRunのinspectorと、socketのないrequest handler | `demo_timeline_inspector`、`audit_inspector`、`full_trace_inspector`、`bare_request_handler` |
| `support/llm.py` | 偽のLLM Providerと、有料APIのライブの確認 | `counting_provider`、`recording_router`、`live_model_check`、`live_source_search_check` |
| `support/reports.py` | PDFレポートの保存済みRun、根拠つきの文章、偽のOpus | `report_saved_run`、`prose_document`、`report_router`、`install_report_router` |
| `support/plans.py` | 取り込む図面：合成の避難所の図面（画像・GeoJSON）と、手で描いたそのトレース | `shelter_trace`、`upload_plan` |
| `support/public_data.py` | 地域の調べが呼ぶ公開サービス（国土地理院の住所検索・指定緊急避難場所、e-Stat、気象庁の予報、設定した地域の公開データ）の偽物。存在しない自治体（合成県合成市）の合成の記録を返し、`fail()`で接続の失敗を起こせる。ネットワークには出ない | `public_data`、`researching_project_api`、`research_request` |
| `support/world.py` | WorldState・イベント・検証の部品 | `make_event`、`record_handler`、`stress_delta`、`count_validations`、`live_checkpoint_world` |

`support/paths.py`（`fixtures/`のファイルのパス）と`support/isolation.py`（隔離の処理）は、fixtureを持たない部品です。テストで使う`fixtures/`のファイルが増えたら、`support/paths.py`に定数を足します。

関数を返すfixtureは、呼ぶたびに新しいものを作るだけで状態を持ちません。そのうち`saved_timeline`、`add_toilets_program`、`demo_world_hash`、`recording_router`はsession単位にして、module単位のfixtureからも使えるようにしています。テストファイルの中のhelperがこれらを使うときは、テストが受け取ったfixtureを引数で渡します（例：`integration/test_intervention_outcomes.py`の`_main_and_child`）。

### 時刻を手で動かすテスト

単体テストでは、handlerの反応を確かめるために、テスト自身で時刻を動かして`EventDispatcher.dispatch_due`を直接呼んでかまいません。ただし、tickの順序（AGENTS.mdの不変条件）を通らないので、部品ごとには正しくても組み合わせると壊れる不具合（#51）を見逃します。次を守ります。

- tickの順序やモジュールの組み合わせで決まる振る舞い（「シミュレーション中にこうなる」）は、`TickEngine`で時間を進める統合テストでも確かめます。時刻を手で動かす単体テストを足すときは、対になる統合テストを足すか、既存の統合テストの名前をコメントに書きます。
- KPI、比較、レポートを確かめるために、イベント記録や履歴を手で書き込んで実行結果の代わりにしません。共通fixture（`generated_scenario`）を実行して作ります。

## 利用者の経路を通る共通fixture

UIで作るScenarioは、必ず図面（`facility_geometry`）と認知（`agent_dynamics`）を持ちます。一方、`fixtures/`のScenarioはどちらも持ちません。#51の不具合は図面と認知がある場合にだけ起き、fixtureを使う多くのテストをすり抜けました。新しいテストは、特に理由がなければ`tests/support/generated.py`の共通fixtureを使います。生成器が作らない形（1つの施設に複数の列、迂回経路など）が要るときは、生成された図面に手で足すか、理由を書いてfixtureのScenarioを使います。

- `generated_scenario`：`ScenarioSpec`をテストごとに新しく作って返します。テスト内で変更しても、ほかのテストには影響しません。
- `generated_scenario_preview`：元になったpreviewの結果（`scenario`、`approved_hash`、`can_start`、`request`など）です。session単位で1回だけ作るので、変更してはいけません。module単位のfixtureから使うときは、`copy.deepcopy(generated_scenario_preview["scenario"])`から`ScenarioSpec`を作ります。
- `generated_scenario_with(**fields)`：画面の入力欄の一部を変えたときのScenarioを、テストごとに新しく作って返します。たとえば`generated_scenario_with(duration_days=4)`は、期間の欄に4と入力した場合です。変えた欄は利用者の入力として扱います。画面にない`InitialScenarioRequest`の項目（`seed`、`operational_conditions.scheduled`、`population_conditions`など）も渡せます。
- `generated_previews(**fields)`：上のpreviewの結果を返します。session単位で、同じ入力のpreviewは1回だけ作って共有するので、変更してはいけません。
- `ui_project_api`と`generated_request(**fields)`：作ったpreviewにさらに画面の操作（世帯の修正、承認、図面の指定など）を続けるテスト用です。`ui_project_api`は、空の地域調査に差し替えた`ProjectAPI`（project名`project`）をテストの間だけ開いておきます。地域調査そのものを通すときは、`support/public_data.py`の`researching_project_api`を使います。

共通fixtureは、demo-v2のScenario画面が送る入力（`GENERATED_SCENARIO_REQUEST`：合成市・地震・20人・0.25日。各項目は利用者の入力として扱います）を、UIと同じ`ProjectAPI`のpreviewに渡して作ります。違いは地域調査だけで、本物は公開Webサービスに接続するため、空の調査結果に差し替えています。

この共通fixtureの実行結果は、`fixtures/regression-contract.json`の`generated_small`が固定していて、`tests/integration/test_generated_scenario_contract.py`が確かめます（fixtureそのものは`tests/unit/repo/test_generated_scenario_fixture.py`）。生成手順やモデルを意図して変えたためにhashが変わった場合は、変えた理由をPull Requestに書いたうえで値を更新します。理由が説明できない変化は不具合として調べます。

### 介入の効果を確かめるテスト

介入が利用者の見る結果を変えることは、`tests/integration/test_intervention_outcomes.py`で確かめます。手順はどの介入も同じです。

1. 共通fixtureの本線を分岐させ、子Branchに介入を1つ適用します。
2. 両方を`TickEngine`で同じ時刻まで進め、Project画面のKPI（`MetricSnapshot`と`operational_kpi_values`）を比べます。
3. 変化が、記録されたイベント（介入の適用、施設や職員の動作）を通して起きていることも確かめます。

比べるのは変化の向きだけで、大きさは確かめません。係数の多くは実測で校正されていないためです。

- 職員が待機中のときにしか出せない指示は、本線を先に進め、`TimelineStore.iter_worlds`で条件を満たす最初の時刻を探してから分岐させます（読み取りだけで、状態も乱数も変えません）。
- 作業の引き継ぎのように、別の介入で作った状態が前提になるものは、介入したBranchからさらに分岐させます。
- 仕切りや設備の移設のように、KPIに現れにくい空間の変化は、人の歩く経路（`movement_v1`の`journeys`）や移動の完了イベントが通る区画で確かめます。移動時間は分単位なので、数メートルの違いは時間に出ません。
- 区画の環境（騒音、明るさ、換気、プライバシー、気温、行動の起こりやすさ）は、続いている間に人の状態（疲れ、衛生の必要度、不満、ストレス）へ積み上がります。期限が切れる時刻に、まだ効いていることを確かめてから比べます。疲れ・衛生・不満はKPIにないので、全員の平均を直接比べます。

## 全経路の確認（Rust移植のゲート）

7日分のハッシュの確認は、生成Scenarioが通る処理しか確かめません（メッセージや信念、介入、記録した応答などを通りません）。tickのコードを移す前に、`benchmarks/gate_all_paths.py`で、そこを通る小さなofflineのScenarioの結果も確かめます。移植の段階の合格の条件のひとつです。結果と届いていない処理は[rust-port.md](rust-port.md)に書きます。

~~~shell
python benchmarks/gate_all_paths.py --output 出力先 --expect fixtures/gate-all-paths.json
python benchmarks/gate_all_paths.py --output 出力先 --determinism
python benchmarks/gate_all_paths.py --output 出力先 --coverage
~~~

- 各Scenarioは自分のプロセスで動き、終わりの世界ごとにworld・events・KPIのハッシュと時間を`出力先/summary.json`に書きます。ネットワークへの接続は失敗させます。LLMの応答は、手元の台本のProviderが書いた応答テープとその再生だけを使います。
- `--expect`：`fixtures/gate-all-paths.json`のハッシュと比べます。モデルや規則を意図して変えてハッシュが変わったときは、理由をPull Requestに書いて、`--write-expected`で更新します。理由が説明できない変化は不具合として調べます。この値はLinuxのものです（OSをまたいだ一致は段階B5で確かめます）。
- `--determinism`：別のプロセス、別の`PYTHONHASHSEED`、保存を読むScenarioでは別の`RAYON_NUM_THREADS`、Rustのkernelなし（`SHELTERSIM_NATIVE=0`）で動かして、1回目と比べます。WindowsとmacOSでは`--no-python-kernels`を付けます（RustとPythonの数学関数が同じなのはLinuxだけです）。
- 分岐・再開・Checkpointの往復は、1つのScenarioの中でまっすぐのRunと比べます（`must_match`）。tickの順序に効くものを省いて同じになることは、この比較で確かめます。
- `--coverage`：tick側のコード（`core`、`domain`、`events`、`policies`、`experiments/runner.py`、`experiments/phases/`）のどの行に届いたかを、ファイルごとに`coverage-native.md`（Rustのkernelあり）と`coverage-both.md`（なしの回も合わせたもの）に書きます。
- `policies`のモデルや、EffectProgramの操作を足したら、ここにもScenarioを足します。`tests/integration/test_all_paths_gate.py`は、モデルの一覧がここに入っていないと失敗します。
- 介入が拒否された結果と、有効な入力でRunが止まる結果（`known_stops`のScenario）も、期待値に入れます。直したら期待値が変わるので気づけます。止まるものは、見つけたときにIssueにします。
- `--reversed-order`：`world.agents`と`model_state`のすべてのdictの順序を、各段階の終わりに逆にする回（`reversed_order`）と、段階の途中でも逆にする回（`reversed_within_phase`）を動かして、1回目と比べます。結果が変わったScenarioは、dictの順序に頼る処理があるということです。途中で逆にする場所は`benchmarks/tick_probe.py`の`WITHIN_PHASE`にあり、処理を移したら一緒に直します。
- 全件を動かすテストには`long_simulation`の印を付けます。通常のpytestは、1つの小さなScenarioだけを動かします。

### 最初に違った場所を探す（`benchmarks/first_difference.py`）

ハッシュが変わったときは、原因をこの道具で絞ります。移植のレーンは、Rustの規則をPythonの規則と比べるのに使います。手順と計測は[lanes/p0-digest.md](rust-port/lanes/p0-digest.md)に書きます。

~~~shell
python benchmarks/first_difference.py --both Scenario名 --output 出力先
python benchmarks/first_difference.py --record Scenario名 --output A --draws
python benchmarks/first_difference.py A B
~~~

- 段階ごとに、`model_state`の名前空間、人、作業、イベントの列、イベント記録などの部分ごとのdigestを書きます。比べると、最初に違った分、段階、部分を示し、そこまでもう一度実行して、違う値をJSONのパスと浮動小数点の16進で示します。
- `--both`は`SHELTERSIM_NATIVE=1`と`=0`で動かして比べます。
- `--draws`は、乱数の抽選ごとに鍵と値、比べた相手の値を記録して比べます。確率がわずかに違っても比較の結果が変わらなければ世界もハッシュも変わらないので、確率の式を移すときはこれを使います。Rustの中の抽選は記録されないので、Rustの確率の式は、レーンの文書の手順で`tests/integration/test_native_kernels.py`にビット単位の比較を足して確かめます。
- これらのフックはベンチマークの側にあり、`src/`には何も足しません。`--draws`や`--reverse`は、`--expect`と同じ最終のハッシュになるはずです。ならなければ不具合として調べます。
- digestは、辞書の順序を区別しません（キーでそろえた正規のJSONです）。順序に頼る処理は`--reversed-order`で確かめます。追記だけの履歴の古い項目の書き換えは、一定の間隔（`--full-every-minutes`）でしか見えません。

## 振る舞いの比較

ハッシュが変わる変更（段階B5など）では、合格の条件はハッシュの一致ではなく決定性になります。どれだけ振る舞いが変わったかは、`benchmarks/compare_behaviour.py`で確かめます。

~~~shell
python benchmarks/compare_behaviour.py --collect 変更前の出力先
python benchmarks/compare_behaviour.py --collect 変更後の出力先    # 変更したコードで
python benchmarks/compare_behaviour.py 変更前の出力先 変更後の出力先
~~~

- KPIごとの平均、seedごとのばらつき、差をMarkdownの表に書きます。ラベルは結果の画面と同じ日本語です。
- 同じseedどうしなら各seedを比べ、違うseedなら2つの集団として比べます。差が標準誤差の2倍を超えたKPIを「揺れより大きい」とします。何も変えていなくても、20個に1個ほどは偶然ここに入ります。
- 同じコードの別のseedどうしを比べると、何も変えていないときの差の大きさが分かります。
- 比べるのは差の大きさまでで、どちらが正しいかは決めません。係数の多くは実測で校正されていません。

## 不具合を見つけたとき

テストで不具合が見つかったら、テストで隠さずにIssueにします（例：#65）。

- 不具合で今は確かめられない振る舞いは、期待する形でテストを書き、`pytest.mark.xfail(strict=True)`にIssue番号を付けて置きます。失敗の種類が決まっているときは`raises=…`で限ります。不具合を直したのに期待と違う結果になった場合も気づけます。直したら印を外します。
- テストの前提（その時刻にトイレが使われている、など）が崩れたときは、`assert`ではなく`pytest.fail`で失敗させます。`xfail`の印が前提の崩れを隠さないようにするためです。
- 外部から来る入力（LLMの出力、取り込む図面、公開データ）の拒否は、理由付きで拒否されること（`StorageError`や契約の検証エラー）を確かめます。ほかの例外で落ちることを見逃さないためです。

## 出典台帳と根拠IDの整合

AGENTS.mdは、新しい係数の根拠を`fixtures/source-manifest.json`に記録するよう求めています。`tests/unit/repo/test_source_manifest.py`は、`src/`に固定の文字列で書かれた仮定ID（`ASM-…`）と出典ID（`SRC-…`）を集め、台帳にも既知の不足の一覧にもないIDがあると失敗します。

- 実行時に組み立てるID（`SRC-PLAN-<digest>`、`ASM-POLICY-OVERRIDE-<hash>`など）と、`str.startswith`に渡す接頭辞は、IDとして扱いません。
- 既知の不足は、テストファイルに分類ごとの一覧があります（#61）：別のファイルに記載があるが台帳にない仮定ID、どこにも記載がない仮定ID、実行時に取得する公開データの出典ID、利用者が入力した値の出典ID。
- 新しい仮定IDを一覧に足してはいけません。コードと同じ変更で台帳に記録します。台帳に記録する中身（出典、単位、上限、仮定、適用範囲）はモデル担当が決めます。
- 台帳に記録されたID、コードが使わなくなったIDが一覧に残っていると、テストが失敗します。一覧は減る一方です。
- 実行時のデータ提供元と利用者入力の出典IDを台帳に載せるかは、まだ決まっていません。新しく足す場合は、その分類の一覧に加え、理由をPull Requestに書きます。

## 確認できていないこと

- 有料のProviderを呼ぶライブのテストは、通常の確認では実行しません。
- Windows・macOSの実機上での起動は確認していません。
- 30日間の性能結果は確立していません。過去の30人・30日試行は10分以内に完了しませんでした。
- 画面の操作のうち、すべての地図レイヤー、実在施設の図面の取り込み、介護・会話・職員の組み合わせ、大規模人口を含む一連の操作は、ブラウザーで確かめていません。
- 地域人口、避難所、天候、医療、結果の照合データを用いた外部検証はできていません。
- 心理、健康、社会、容量、incidentの係数は実測校正されていません。合成Branchの差は、条件付き出力であり、予測や現実の因果効果ではありません。

再現可能な1日性能値と環境は[benchmarks.md](benchmarks.md)、モデルの仮定と出典の記録は[model-validation.md](model-validation.md)と`fixtures/source-manifest.json`を参照してください。
