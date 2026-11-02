# L9 レーン：旧モデルの Run を閲覧のみにする仕組み

版を上げたとき、それより前に計算した Run を**閲覧だけ**できるようにする仕組みを入れました。**版はまだ上げていません**（上げるのは L11 の1回です）。そのため結果とハッシュは変わりません。tick の規則も係数も変えていません。

決めごと：利用者の決定（2026-10-31）「版を上げた後の旧モデルの Run は閲覧のみ。閲覧はできる。続き、分岐、submit、EffectProgram の適用ははっきりしたエラー。黙った読み替えはしない」と、D2「旧 Run は保存の時点だけを見せる。保存の間の分は保存の時点に丸め、丸めたことを画面に出す。新しいモデルで計算し直さない」。

## 入れたもの

| 項目 | 場所 | 中身 |
|---|---|---|
| 全体の版 | `core/model_version.py`、`contracts/policies.py` の `model_signatures` | `engine:model`（`ENGINE_VERSION = "1"`）と `engine:rng_algorithm`（`CounterRNG.algorithm_id`、`sha256-keyed-v1`）。**L11 は `ENGINE_VERSION` を1か所上げるだけで、Scenario ごとの署名は変えずに全部の旧 Run が閲覧のみになります** |
| 記録していない保存の規則 | `UNRECORDED_ENGINE` | 2項目がない保存は版1と `sha256-keyed-v1` として扱います。明示の規則で、`fixtures/saved_runs/engine-v1-small` で確かめます。今ある Run はすべてこれです |
| 版の比べ方 | `model_version_status` | 前の `Checkpoint._legacy_queue_migration_required` を置き換えました。全体の版の違い、署名の違い、列の旧方式の移行（`migration="legacy_queue"`）、署名を記録する前の保存を、前と同じ規則で分けます。今のコードが署名できない設定は、閲覧では旧モデル（`model_signatures`）として扱います |
| 読むだけの口 | `Checkpoint.restore_view`、`Checkpoint._load(view=True)`、`CheckpointArchive.decode_restored(view=True)`、`ProjectStore.load_view_checkpoint`、`ProjectStore.checkpoint_model_status` | hash と復元した状態は今までどおり確かめ、版の違いでは失敗しません。**返す世界には印（`SavedPointView`）が付き、`TickEngine`（作るときと `step`）、`OfflineSimulationController`、`BranchRuntime`（作るとき、`apply`、`apply_effect_program`）、`Checkpoint.capture` が拒みます**。旧モデルなら `ViewOnlyRunError`、今のモデルの閲覧なら `ViewOnlyWorldError` |
| tick の経路の確かめ | `Checkpoint._validate`（`restore_state`、`restore_world`、`from_dict`）、`ProjectStore.load_restored_checkpoint`（前に復元して残した世界の道も） | 版が違えば `ViewOnlyRunError`。保存は `StorageError`、`code="run_model_view_only"`、日本語の文に変えます。**文書が書き換えられていれば、旧モデルではなく hash の不一致として報告します** |
| 時刻の丸め（D2） | `TimelineStore.view_at`、`iter_worlds`、`frame`、`requested_minute` | 旧モデルの Run は、直前の保存の時点を復元して見せます。`iter_worlds` は求めた1分ごとに1つの世界を返し、印に求めた時刻と保存の時点が入ります。`frame` は `model_view` と `saved_point_rounding` を付けます。**`TickEngine` を作りません** |
| 止める道 | `TimelineStore.advance`、`fork`、`seek`、`require_current_model`、`ProjectAPI.submit`、`ProjectAPI.fork`（スレッドとプロセスの両方、EffectProgram の適用を含む）、`workspace.intervention`、`storage/commands.py` の `resume`、`fork` | `run_model_view_only`。Web の計算開始と分岐は worker を起動する前に止めます |
| 閲覧の道 | `web/replay.py`、`web/project_queries.py`（`history`、`compare`、`first_divergence`）、`web/project_services.py`（`compatibility`）、`web/pdf_report.py`、`web/demo.py:292,1024`（`restore_view` に）、`storage/usage.py`、`web/projects.py` の Run を開く道（`runtime_state` だけ読む） | 丸めた時刻は `exact: false` と `saved_point_rounding`。PDF は保存の時点を選ぶよう求めます（`pdf_saved_point_required`） |
| demo の job | `web/jobs.py` | 旧モデルの job は server の再起動で `failed` として残し、Branch を復元しません。保存したファイルはそのままです |
| 画面 | `project.js`（`/demo`、`/admin`）、`demo-v2.js`、`demo-v2-replay.js` | `/admin` は KPI の行の先頭に、`/demo-v2` は「発生中の事象・支援上の課題」の先頭に説明の文を出します。時刻の表示は保存の時点に戻ります。replay の player は丸めた frame を、求めた時刻の frame として受けます |

## 古い保存の形（計画の 6）

- **列の旧方式：** 今は前と同じに移行して続きを計算できます（同じ版の Run の振る舞いは変えない）。閲覧でも同じ移行をして読みます。版を上げた後は、全部の旧 Run が閲覧のみなので、tick には届きません。
- **情報の状態の schema 1：** 移行は tick の中（`upgrade_information_runtime_state`）で、続きを計算すると次の保存は schema 2 です。今のコードは schema 1 を書きません。
- **切り替え `LEGACY_SHAPES_TICKABLE`：** `False` にすると、この2つの形を含む保存は版が同じでも閲覧のみになります（`legacy:facility_queues_fifo`、`legacy:information_schema_1`）。**L10 は L11 より前に Python を消すので、その間は版1のままです。O の断り（`information.rs:386`、`information_events.rs:50`。`tick_validation.rs:430` は O ではなく v6 の状態の規則でした：L7b）の Python の道を消すときに、この切り替えを `False` にしてください。** テストは両方の値で確かめています。
- **WorldState v5 は古い形ではありません。** 資源の運用がない Scenario は今も v5 で始まります（`resource_service_enabled != (schema_version == 6)`）。`tick_validation.rs:258` の `not a WorldState v6`（棚卸しの O）は普通の Run で届くので、閲覧のみにしても消えません。**記号の付け直しが要ります。**（L7b で C に直し、v5 の検証も Rust にしました：[l7b-world-v5.md](l7b-world-v5.md)）

## テストと fixture

- **`fixtures/saved_runs/engine-v1-small`（約 440 KB）：** `scripts/build_saved_run_fixture.py` で `6d5662c` の worktree（`SHELTERSIM_NATIVE=0`）から作った、版を記録する前の保存です。h00_small_replay、90分、main は 0、30、60 分、child は 30 分で分岐して 60 分まで。履歴の索引 `history.sqlite`（約 320 KB）を含みます。**今のコードで作り直すと版を記録するので、作り直さないでください。**
- `tests/integration/test_checkpoint_versions.py`：版1を入れたときに続きが計算でき、新しい保存が版を記録すること。版を上げたとき（`newer_engine`、L11 の後は何もしない fixture）に、frame、`iter_worlds`、`view_at`、Project API の frame・replay-frames・比較・履歴・互換性、CLI の `frame` が保存の時点を見せて丸めを示すこと。続き、分岐、submit、EffectProgram の適用、介入の確認、CLI の `resume`、`fork` が `run_model_view_only` で止まり、保存したファイルが1つも変わらないこと。閲覧の間は `TickEngine` を作らず、`CounterRNG.random` も `LLMRouter.complete` も呼ばないこと（呼べば失敗する fixture）。閲覧の世界を tick、コントローラー、`BranchRuntime`、`Checkpoint.capture` に渡すと拒むこと。版0を記録した Run（版を偽った Run を今のコードで作る）が、版を上げなくても今すぐ閲覧のみになること。
- `tests/unit/core/test_model_version.py`：署名に2項目が入ること、記録していない保存は版1、hash の不一致は版の違いとして報告しないこと、署名できない設定、列の旧方式（今は続けられる、切り替えと版上げで閲覧のみ、閲覧では移行して読む）、情報の schema 1、文。
- `tests/integration/test_demo_jobs.py`：旧モデルの demo の job。`tests/unit/web/test_static_demo_v2_replay.py`：丸めた frame を受け、違う時刻の frame は今までどおり拒むこと（JS を戻すと失敗することを確かめました）。
- 前のテストで変えたもの：`test_registry.py` の `model_suite` が違う Checkpoint は `ViewOnlyRunError` に（前の文 `explicit migration required` はなくなりました）。この test は v1 の規則で hash を作り直していて、前は hash を確かめる前に版で止まっていたので通っていました。`test_checkpoint_cache.py` は `_world_document_for_restore` が状態も返すように。

## 結果と確かめたこと

- **ハッシュ：** ゲート `--expect fixtures/gate-all-paths.json` は 55 Scenario で問題0、`--reversed-order` でも問題0。7日分（project、generated）は world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041` で `--expect` と一致しました。**Checkpoint の `model_versions` と `checkpoint_hash`、Run の記録の `model_versions`、trace の `policy_versions` には2項目が増えます。** world、イベント、KPI のハッシュには入りません。
- **既存の保存：** `fork-b1` の保存（このレーンより前のコードで保存）を版1として分岐し、計算を続けられました（下の予算）。
- **予算：** `tick_budget.py`、`fork-b1` の 9600 分から 480 tick、3回。lane/base 0.986（0.986、0.955、1.049）、A/A 1.010（0.958、1.010、1.015）、状態は同じ。tick に足したのは `TickEngine.step` ごとの印の確かめ（dict を1回引く）だけです。他のレーンも動いていて、負荷は 10〜16 でした。
- **テスト：** 全テスト、Rust あり 4663 passed、8 skipped。`SHELTERSIM_NATIVE=0` で 2094 passed、2577 skipped。ruff、`ruff format --check`、clippy（`disallowed_methods`）は問題なし。`tracked_mirror_gate.py` は動かしていません（指示どおり）。
- **Rust：** 変えていません。印は Python の入口（`TickEngine`、コントローラー）で確かめます。driver（`run_tick`）は `TickEngine.step` からしか呼ばれません。

## L11 に渡すこと

- `ENGINE_VERSION` を上げる。`newer_engine` の fixture は何もしなくなり、同じテストが本物の版上げで動きます。版1を入れる `test_a_run_saved_before_the_model_version_was_recorded_is_of_engine_1` は、版1の規則のテストとして残ります。
- D6（int の係数を float に）で保存の JSON が変わるなら、閲覧で旧 Run の `model_configs` を今の検証にかけるかどうかを決める。**今の閲覧は、復元した世界を今の `WorldState.validate` で確かめます。** 検証が厳しくなると、旧 Run が閲覧でも読めなくなります（`checkpoint_incompatible_or_corrupt`）。そのときは閲覧用の明示の移行を足すか、検証を古い形に合わせてください。
- 閲覧の frame と履歴の投影（`inspection/frames.py` など）は Python のままで、今のコードで描きます。描き方の変更は「計算し直し」ではありませんが、旧 Run の見え方は変わり得ます。

## 残したこと、確かめていないこと

- **ブラウザー：** `.venv` の Playwright で、版を2にした server（scratchpad の `speed/lane-l9/ui/`）に fixture の Run を開いて確かめました。`/admin` で 45 分を開くと KPI の行に2つの文が出て時刻欄が 30 になり、計算を押すと状態の行に `run_model_view_only: このRunは旧モデル（版 1）…` が出ました。`/demo-v2` では課題の欄の先頭に文が出て、45 分を選ぶと 30 分に戻りました。ページのエラーは0です。**自動テストにはしていません**（replay の player だけ node のテストがあります）。`/demo-v2` の計算ボタンは押せたままで、押すと server が拒みます（画面の側で先に無効にはしていません）。
- `first_divergence` は丸めた標本を `saved_point_rounding` に並べますが、旧モデルと今のモデルの Run を比べる意味は説明していません。
- 旧モデルの Run の frame はキャッシュに書きません（閲覧は書き込みをしない）。毎回 Checkpoint を復元するので、長い Run では遅いはずです。測っていません。
- README は変えていません。版を上げるまで利用者には見えないためです。
