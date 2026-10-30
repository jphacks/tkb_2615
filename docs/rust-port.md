# Rust 移植の設計（段階B）

ブランチ `feature/rust-port` で進める移植の設計です。計測と経緯は Issue #272 にあります。

## 目的

100人・7日分の Run を、画面から約5分（約318秒）以内で計算できるようにします。Python で入れられる改善は入れ終わり、develop（タグ `python-perf-2026-10-30`）で7日分は 650.5秒です。このブランチでは、B1、tick の計算、tick の終わりの検証、保存、B4 の変更を合わせて 177.0秒です（2026-10-31。他の重い処理がなく、fsync が 7.5秒と軽かった回です。fsync が 37秒だった回の負荷なら約207秒です）。

## 前提になる事実

### 時間の内訳（7日分、このブランチ）

| 中身 | 時間 | 何に比例するか |
|---|---:|---|
| tick | 約283秒 | 今の状態（人・関係）の Python のオブジェクトの読み書き |
| Checkpoint の作成 | 約192秒 | 主に EventLog（ハッシュ・保存前の検証・文書化） |
| ファイルの書き込み | 約91秒 | 新しい chunk の量と fsync |
| 7日目近くでの分岐 | 24秒 | ほぼ Checkpoint の読み込み（約18秒、EventLog の組み立て） |

### 世界の中身（7日目の終わり、正規化した JSON で約352 MB）

| 部分 | 大きさ | 件数 |
|---|---:|---:|
| EventLog（`event_log`） | 329.6 MB（94%） | 273,176件 |
| `model_state` | 21.1 MB | 19の名前空間。大きいのは体調・共有の運営・資源台帳・給水配食の履歴の窓（合わせて約18 MB）と、社会関係（1.5 MB）、体調の関係（0.7 MB） |
| 支援の依頼（`support_requests`） | 1.0 MB | 3,309件 |
| 人（`agents`） | 0.07 MB | 100人 |
| その他 | 0.2 MB未満 | |

### 移植の前提になる作り

- **「写して差し替える」処理：** 介入と一部のイベントの処理は、`WorldState.from_dict(world.to_dict())` で世界を写し、写しを変えてから、元の世界の中身を差し替えます（約15か所。`events/effects.py`、`events/handlers.py`、`domain/population_lifecycle.py`、`experiments/branch.py`）。
- **状態の持ち方：** `model_state` の状態は素の dict で、保存（`json.dumps`）・復元（`from_dict`）・比較（`==`）にそのまま流れます。人の状態（`AgentState`）は、約20個のファイルから属性として直接読み書きされます。
- **EventLog の使われ方：**

| 使い方 | 箇所 |
|---|---:|
| 足す（`append`） | 22 |
| ID で引く（`find`） | 15 |
| 全件を読む（`records`。保存・報告・比較・完全な検証） | 19 |
| 種類・来歴で読む（`of_types` など） | 6 |

- **試作で分かったこと：**
  - 計算そのものは、Rust で Python の約95倍速い（心理の更新）。
  - Python と Rust の間で毎回状態を受け渡すと、その手間で得がほぼ消える。
  - Python のオブジェクトを作る・読む処理は、Rust からでも減らない。

## 方針

1. **状態の持ち主を、まとまりごとに Rust へ移す：** まとまりは「状態と、それを変えるコードと、検証と、保存の形」を一緒に移せる単位です。移したまとまりは、Python からは今と同じ名前・同じ形で読める窓口（PyO3 のクラス）を通して使います。
2. **保存の形は、各段階で変えない：** Rust が持つ状態からも、今と1バイト違わない正規化した JSON を作ります（試作で、7日目の世界の文書で同じ出力になることを確かめました）。Checkpoint・索引・ハッシュは、段階B3まで今のままです。
3. **段階ごとの合格の条件は、ハッシュの一致：** 7日分の world・events・KPI のハッシュ（`benchmarks/investigate_performance.py --expect`）と、全テストです。Linux では Python と Rust が同じ数学ライブラリを使うので、結果が今と同じになります。
4. **Python の実装は、段階が合格するまで残す：** 段階の作業中は、`SHELTERSIM_NATIVE=0` で今の Python の実装に戻せるようにし、毎 tick の比較テスト（`tests/integration/test_native_kernels.py`）で同じ結果になることを確かめます。合格した段階の Python の実装は消し、その部分は Rust を必須にします（下の「決めたこと」）。

## 段階

### B1：EventLog を Rust に持たせる（保存・読み込み・分岐に効く）

**中身：**

- EventLog の記録を、Rust の中に正規化した JSON の bytes（と、索引に要る項目：ID・順序番号・実現時刻・種類・状態・来歴の印）で持ちます。
- 今の索引（ID、順序番号、時刻ごと、種類ごと、来歴ごと）は Rust の中に持ちます。
- Python からは、今の `EventLog` と同じメソッドで使えます。記録のオブジェクト（`EventLogRecord`・`ScheduledEvent`）は、Python が読むときに初めて作ります。
- **保存：** 記録の bytes をそのまま chunk にします。
- **読み込み：** chunk の bytes から、Python のオブジェクトを作らずに Rust の中で組み立てます。
- **復元の確認：** 記録の bytes を比べます。
- **EventLog の監査：** 重複と実現時刻の確認を、Rust の中で行います。

**変えるところ：**

| 場所 | 変えること |
|---|---|
| `events/queue.py` | `EventLog` を窓口のクラスにする |
| `core/world.py` | `from_dict`・`_canonical_data`、完全な検証 |
| `core/checkpoint_cache.py`・`core/checkpoint.py` | 保存・復元 |
| `storage/history_index.py`・`storage/index_writer.py` | 新しい記録を Rust から受け取る |
| `events/models.py` | 記録を後から作る |

**合格の条件：** 7日分のハッシュの一致、全テスト、7日目の Checkpoint の読み込みと分岐の時間。

**読み込みの作り（実装済み）：**

- 復元のための読み込み（`CheckpointArchive.decode_restored`）は、`world_state.event_log` の chunk を記録ごとの bytes に切り分けます（`sheltersim_native.split_json_array`）。文書の中では、bytes を持つ読み取り専用の列（`core/saved_items.py` の `SavedItems`）になります。要素は、Python が読むたびに新しく解析します。
- 2つのハッシュは、記録の bytes をつないで計算します（書き出し直しません）。
- EventLog は bytes から Rust の中で組み立てます（`EventLog.from_saved`・`extend_saved`）。解析は並列で行い、足す順は今と同じです。
- bytes だけでハッシュを確かめると、書き方の違う JSON も通ってしまいます。そこで組み立てるときに、各記録について次を確かめます。合わない記録は `CheckpointError` になります。
  - 保存が書く正規化した JSON と1バイト違わないこと
  - `EventLogRecord`・`ScheduledEvent` と同じ検査（項目、状態、種類、ID、整数）に通ること
- 保存側の履歴キャッシュ（`CheckpointHistoryCache`）は、Rust の EventLog の記録をオブジェクトの同一性ではなく位置と bytes で追います。読み込んだ記録は、続きの保存でも作り直しません。読み込み・分岐の直後の保存では、復元の確認は新しい記録だけを比べます。
- 分岐の始点の数え方（`history_index`）は、記録の bytes から実現時刻を読みます（`saved_realized_minute`）。

**計測（7日目、2026-10-30）：**

| 処理 | 前 | 後 |
|---|---:|---:|
| Checkpoint の読み込み（`load_checkpoint`） | 17.4秒 | 4.2秒 |
| 7日目近くでの分岐 | 24.0秒 | 6.2秒 |
| 7日分の計算（画面からの経路、world・events・KPI のハッシュは一致） | 631.7秒 | 556.2秒 |
| └ Checkpoint の作成 | 約192秒 | 163.1秒 |
| 最大メモリ（RSS） | 5.8 GB（develop の 650.5秒の回） | 4.8 GB |

**見込み：**

- 読み込み：約18秒 → 数秒
- 7日目近くでの分岐：24秒 → 約5秒
- 7日分の計算：Checkpoint の作成と復元の確認で約100秒減って、約530秒

### B2：人ごとの今の状態と、毎 tick の計算を Rust に持たせる（tick に効く）

**中身：**

- **Rust に持たせる状態：** 人（`AgentState`・`NeedState`）、心理（`psychological_distress_v1`）、体調の人ごとの状態と関係（`body_state_v1` の `agents`・`relationships`）、社会関係（`social_network_v1`）、社会の伝播の状態（`social_propagation_v1`）、情報の伝播（`information_spread_v1`）。
- **Rust に移す計算：** 状態の更新の段階（心理・社会・情報）、その検証、`WorldView` の組み立て。
- **Python のコードからの見え方：** 人の属性（`agent.stress` など）は、窓口のクラスの属性として今までどおり読み書きできます。社会関係・体調の関係を変える関数（`record_contact` など）は、Rust の関数を呼びます。
- **「写して差し替える」処理：** 窓口が、Rust の状態の写し（`clone`）を作れるようにします。

**合格の条件：** 7日分のハッシュの一致、全テスト。

**見込み：** tick 約283秒 → 約150秒。7日分は約400秒。

**毎 tick の検証（実装済み）：**

- `TickEngine.step` の終わりの検証は `WorldState.validate_after_tick` です。`validate(validate_event_history=False)` と同じ規則を通しますが、前の分から変わっていないと示せるものを省きます（`core/tick_validation.py`）。何を省くかとその根拠は `docs/architecture.md` にあります。完全な検証（`validate()`）の結果は変わりません。
- 大きな辞書の走査は、Rust の確認（`native/src/validation.rs`）を先に通します。Rust の確認は「すべて規則を満たす」と分かったときだけ受け入れ、それ以外は Python の規則が判定します。エラーの文は Python の規則からだけ出ます。
- 社会関係・体調の関係の記録は、長い Run ではメモリ上に散らばり、Rust で全部を読むだけで 1 tick に数ミリ秒かかります。そこで CPython の dict watcher（Python 3.12 以降）で、書き込まれた記録だけを確かめます（`native/src/watch.rs`）。安定 ABI の外なので、関数は ctypes で探します。
- Rust の部品がないときは、毎 tick `validate(validate_event_history=False)` を実行します。

**計測（7日分、画面からの経路、world・events・KPI のハッシュは一致、2026-10-31）：**

| 処理 | 前 | 後 |
|---|---:|---:|
| 毎 tick の検証（1 tick あたり） | 約7 ms（`step` と各段階の差） | 1.45 ms |
| 毎 tick の検証（7日分） | 70.9秒（同） | 14.6秒 |
| `TickEngine.step` と各段階の合計の差 | 70.9秒 | 15.3秒 |

ほかの計算も同時に動く中での計測で、段階の合計は同じ回で 192秒から 233秒に増えています。

**ここまでに入れたもの（状態の持ち主はまだ Python）：**

計測すると、次の一歩は状態の持ち主を移すことではなく、計算を移すことと、毎回の作り直しをやめることでした。これらの段階の計算は、状態を Rust に移さなくても下がります。

- **社会の伝播（`ExpressivenessChannelOpennessPolicy`）：** 接触の抽選と式を Rust で計算します（`native/src/rules/social_propagation.rs` の `social_channel_update`）。入力は毎 tick 受け渡しますが、100人分で小さいので、受け渡しの手間で得は消えません。
- **情報の伝播：** メッセージが1件もない間は、接触の抽選と記録を Rust で計算します（`information_contacts_without_messages`）。このとき `WorldView` を作りません。メッセージ・信念・支援の依頼の記録が1件でもあれば、これまでどおり Python で計算します。
- **`WorldView.from_world`：** 人ごとの `AgentView` を生成された `__init__` を通さずに1つの dict で作り、同じ区域の項目を共有します。解析済みの `PersonContext` はコントローラーごとに持ち、元の記録と違えば解析し直します。監査用のハッシュは `canonical_sha256` で計算します。
- **毎回の作り直しをやめたもの：** `selected_models`（段階ごとに1回）、心理の長期状態の `deepcopy`、資源台帳の予約IDの確認、移動の経路が物理的に通れるかの判定（入力が変わるまで）、区域の中心、`dispatch_due` のキューの並べ替え。
- **`AgentStateDelta` の確認：** Rust の計算結果は Rust の中で範囲を確かめ、外れたときだけ Python の確認に任せて、同じエラーにします。

Python に任せる場合（型の違う値、不正な状態、trace が要るとき）は、Rust の関数が `Unsupported` を返します。`SHELTERSIM_NATIVE=0` では、すべて Python で計算します。

**計測（7日分、画面からの経路。移植前の commit と同時に実行して同じ負荷で比較、2026-10-31）：** world・events・KPI のハッシュは一致しました。

| 処理 | 前（秒） | 後（秒） |
|---:|---:|---:|
| `update_agent_state` | 76.4 | 26.1 |
| `propose_actions` | 59.4 | 37.3 |
| `sample_endogenous_events` | 38.6 | 1.8 |
| `dispatch_due_events` | 29.6 | 22.4 |
| `arbitrate_actions` | 25.4 | 23.2 |
| `collect_metrics` | 10.0 | 8.9 |
| 段階の合計 | 239.4 | 119.6 |
| tick（`TickEngine.step`） | 361.4 | 236.0 |

他の作業と同時に動かした回で、時間は単独の回より長く出ています。単独の回の 556.2秒と直接は比べられません。

**窓口ごしのアクセスの計測：** PyO3 のクラスごしに Rust の表の値を読むと約26 ns、書くと約25 ns でした。dataclass の属性は読み約13 ns、書き約14 ns です。1回あたり約2倍ですが、1 tick に数千回読んでも数十マイクロ秒で、tick の約 12 ms に比べて小さく、ここは障害になりません。文字列・enum・入れ子（`agent.needs`）の窓口は、この計測に入っていません。

**残り（まだしていないこと）：**

- 人の状態（`AgentState`・`NeedState`）、体調、社会関係、情報の伝播の状態の持ち主を Rust に移すことと、「写して差し替える」処理の窓口。今の段階の時間の大半は `domain`・`events` の Python の規則（B4 の範囲）で、状態を移すだけでは大きく下がりません。B3 で保存の形を Rust から作るときに、合わせて考えます。
- メッセージがあるときの情報の伝播の計算。

### B3：Checkpoint の保存を Rust にし、ハッシュを chunk の木にする

**中身：**

- Rust が持つ状態から、chunk・目録・ハッシュを直接作ります。（未着手：状態はまだ Python にあり、B2 の後に進めます）
- Checkpoint のハッシュを、文書全体の SHA-256 から、chunk のハッシュの木の根に変えます（Checkpoint の版を上げます）。増えた chunk だけを計算すればよくなります。（実装済み）
- 古い版の Checkpoint は、今の決まりで読めるようにします。（実装済み）

**合格の条件：**

- world・events・KPI のハッシュ（文書から計算するもの）は一致
- 古い Checkpoint の読み込み、再生、分岐、比較のテスト

**実装済みの作り：**

- **Checkpoint v2 のハッシュ（`core/checkpoint_cache.py`）：** 履歴のリスト（`event_log`、`model_state` の追記専用の履歴、KPI と資源の標本）は、項目数と128項目ごとの SHA-256 からハッシュを作ります。この SHA-256 は archive の chunk の名前と同じなので、同じ bytes を二度ハッシュしません。ほかの値は正準 JSON の SHA-256 で、履歴を含む object はフィールドごとのハッシュからハッシュを作ります。決まりは文書だけで決まるので、キャッシュがなくても同じ値になります。v1 の Checkpoint は文書全体の SHA-256 で確かめて読み、続きの保存は v2 になります。
- **秘匿情報の確認：** 新しい chunk の bytes をまとめて Rust で読み（`sensitive_candidates`）、異なるキーと、パターンに一致しうる異なる文字列だけを Python の同じ関数で判定します。保存済みの chunk は読みません。
- **再利用：** 復元の確認は、新しい KPI の標本だけを読み、この保存で等しいと分かった履歴の項目を比べ直しません。frame のケア履歴と EventLog の bytes は、増えた記録だけを読みます。正準 JSON は Rust で作ります（Rust が扱えない値は `json`）。
- **分岐・再開：** 活動中の行動は、EventLog の索引から決め手の記録だけを読むので（`active_lifecycle_records`）、17万件の記録を作りません。
- **索引：** 行の秘匿情報の確認を Rust で読み、記録は bytes のまま索引のプロセスに送ります。索引は計算の外のプロセスなので、計算の時間には待たされたときだけ効きます。
- **書き込みのスレッドは入れない：** 書き込みだけを別のスレッドに渡す作りも試しました。1回の保存あたりの短縮は約50 ms（7日分で約17秒）で、計算の途中で強制終了すると失う保存が最大で1回分増え、保存の失敗も遅れて分かります。短縮に見合わないので、保存は今までどおり計算のスレッドで終えます。

**計測（7日分、画面からの経路、2026-10-31。B2 の tick と検証の変更を含む。他の重い処理がないときの1回。world・events・KPI のハッシュは一致）：**

| 処理 | 前（B1、556.2秒の回） | 後 |
|---:|---:|---:|
| 全体 | 556.2秒 | 261.0秒 |
| `TimelineStore.record`（336回） | 288.8秒 | 152.4秒 |
| └ `Checkpoint.capture` | 163.1秒 | 76.0秒 |
| └ `ProjectStore.save_captured_branch` | 96.7秒 | 57.4秒 |
| `os.fsync`（3,034回） | 36.9秒 | 37.1秒 |
| `TickEngine.step` | 263.3秒 | 105.3秒 |

最初の基準の 1590秒から約6.1倍で、目標の約318秒を超えました。保存の間隔と fsync の回数は変えていません。

7日目近くの1回の保存（他の作業が少ないとき）：

| 処理 | 前 | 後 |
|---:|---:|---:|
| `TimelineStore.record` | 1,076 ms | 420 ms |
| ハッシュ（2つ） | 376 ms | 20 ms |
| 秘匿情報の確認 | 119 ms | 10 ms |
| frame | 97 ms | 35 ms |

**保存の前の完全検証（実装済み）：**

保存の前の `world.validate()` は、追記専用の履歴（共有の運営、資源台帳の監査記録、給水配食の依頼と停止、体調の社会履歴）を毎回全部読み直していて、7日目近くで1回あたり約130 ms でした。書き手の履歴キャッシュがあるときは、前の保存で通った項目を読みません（`core/save_validation.py`）。ほかのものは保存のたびに全部読みます。

- **変わっていないことの確かめ方：** 各履歴の全項目を、前の保存の文書と毎回比べます（`same_json_items`、約10 ms。`==` の比較を置き換えたものです）。型まで比べるので、`1` と `True`、タプルとリストも見分けます。
- **規則が読むほかの状態：** 古い項目の規則が読むものも比べます。

| 読まないもの | 変更を見つけるもの | 見つける時 | 違反を入れるテスト（`tests/integration/test_save_validation.py`） |
|---|---|---|---|
| `shared_operations_v1.history` の古い記録（解析・順序・EventLog との対応・支援の再生） | 項目の比較。`event_log` の同一性、時刻、`staff_operations_v1.history` の正準JSON | 同じ保存で全体を再生 | `operation_*`、`clock_moved_back`、`test_a_changed_entry_or_input_...`、`test_a_changed_staff_history_...` |
| `resource_ledger_v1.audit` の古い記録（項目の検査・再生） | 項目の比較。倉庫ID、既知の人のID、時刻 | 同じ保存で全体を再生 | `audit_*`、`resident_of_old_audit_forgotten` |
| `resource_service_v1.request_history` の古い記録 | 項目の比較。既知の人のID、区域のID、設備の種類、時刻 | 同じ保存で全体を読む | `request_*`、`zone_of_old_request_removed`、`facility_kind_of_old_request` |
| `resource_service_v1.queue_stall_history` の古い記録 | 同上 | 同じ保存で全体を読む | `stall_*` |
| `body_state_v1.history` の古い記録 | 項目の比較。過去の人を含むID、時刻 | 同じ保存で全体を読む | `body_*` |

各テストは、壊した世界について、記憶のない `validate()` と同じ判定（同じエラーの文）になることを確かめます。

- **`core/tick_validation.py` が完全検証に任せているもの：** 属性の代入を通らない書き込み、設定の `1`→`True` は、保存のたびに今までどおり読みます。古い共有の運営の記録と、履歴の中のタプルとリストの差し替えは、上の比較で同じ保存で見つけます。Run の終わりまで待つものはありません。Run の開始・終了（`TickEngine.run`）、読み込みなどの完全検証は、全項目を読みます。
- **覚えたことを捨てる時：** 読み込み・分岐・再開の世界は新しいキャッシュで始まるので、最初の検証は完全です。Checkpoint の作成に失敗すると捨てます。Rust の部品がないときは毎回全部読みます。
- **限界：** 共有の運営の履歴が上限（20,000件）を超えて `history_truncated` になると、再生の始点が変わるので、その履歴は毎回全部読みます。今の Scenario の7日分は約8,000件で、ここに届きません。

ほかに、モデルの署名を書き手ごとに設定の正準JSONあたり1回だけ計算し（1回の保存で2回計算して約10 ms でした）、archive が文書の大きさを 64 KiB を超えると分かった時点で数えるのをやめました（EventLog の全記録の長さを足していました）。

**計測（7日分、画面からの経路、2026-10-31。world・events・KPI のハッシュは一致。保存の間隔と fsync の回数は同じ）：**

| 処理 | 前（261.0秒の回） | 前（同じ日の別の回） | 後 |
|---:|---:|---:|---:|
| 全体 | 261.0秒 | 315.3秒 | 207.3秒 |
| `TickEngine.step` | 105.3秒 | 133.1秒 | 109.0秒 |
| `TimelineStore.record`（336回） | 152.4秒 | 178.5秒 | 94.8秒 |
| └ `Checkpoint.capture` | 76.0秒 | 100.4秒 | 52.6秒 |
| └ `ProjectStore.save_captured_branch` | 57.4秒 | 48.4秒 | 23.1秒 |
| `os.fsync`（3,034回） | 37.1秒 | 21.8秒 | 5.2秒 |

他の作業と同時に動かした回で、`os.fsync` はディスクの混み具合で大きく変わります（前の2回でも 37.1秒と 21.8秒です）。全体の差の 54秒をこの変更の効果と読むことはできません。`step` に対する比で比べると、`capture` は 0.72〜0.75 から 0.48 に、fsync を除く保存は 0.19〜0.20 から 0.16 に下がりました。`step` が 105秒の負荷に直すと、合わせて約28秒の短縮です（見込みの約40秒には届きませんでした）。

**残り：** 7日目近くの `capture`（1回あたり約140 ms、他の作業と同時）で大きいのは、文書の作成（約70 ms。正準JSONの作成 8 ms と `json.loads` 17 ms はその一部で、3.7 MB を毎回読み直します）、ハッシュの木（約20 ms、同じ JSON をもう一度作ります）、復元の確認の EventLog の比較（約20 ms、新しい記録の数に比例）です。

**保存の残り（2026-10-31）：**

`TimelineStore.record` のうち `capture` と `save_captured_branch` の外にあった約40秒は、`BackgroundIndexer.submit` の 25.8秒（CPU は 1.2秒）と `_cache_frame` の 16.7秒でした。`submit` の待ちは、保存1回分の送信がパイプの大きさを超え、登録のプロセスが前の登録を終えて読むまで止まっていたためです。パイプを別のスレッドで読むと、待ちは「登録が2回分遅れたら待つ」に移るだけで、減りませんでした。登録1回が CPU で約0.5秒（7日分で 168秒）かかり、計算の1周期とほぼ同じだったからで、登録のプロセスが計算全体の律速になっていました。

| 変更 | 効くところ |
|---|---|
| 新しい索引では `entity_time`（読むクエリがない）を作らず、`entity_turn_order` を会話の行だけの部分索引にする | commit がジャーナルと本体に書くページ。索引の大きさは 4.9 GB → 3.9 GB |
| 登録のプロセスが1つの接続と 256 MiB のキャッシュを保つ。今の版の索引を開くときに版を書き直さない（内容のない commit がなくなる） | 登録。ほかの処理の commit と同期はそのまま |
| 索引の行を Rust で1回で読む（`index_row`）。`digest` の正準JSONも Rust | 登録 |
| 世界の文書の各フィールドの正準JSONを1回だけ作る | ハッシュの木 |
| 復元の確認の新しい記録を、復元した記録を作らずに bytes と比べる（`same_saved_records`）。同じと言い切れないものは今までどおり `==` | 復元の確認 |
| 新しい記録の bytes を凍結したまま作る（`record_json`） | 文書の作成 |
| chunk の圧縮を最大4スレッドにする（保存はスレッドの終わりを待つ。bytes は同じ） | 書き込み |
| 秘匿情報のキーのキャッシュを広げる（関係のキーは約5,000個で、4,096個のキャッシュは毎回全部外れていました）、v1/v2 の chunk のファイルがないときは確認しない | 書き込み |
| frame を Rust で符号化し、最後の dump が写すものを途中で写さない | frame |

EventLog の比較の保証：`same_saved_records` は保存した bytes を読み、live の記録の全フィールドと、bytes の全キーについて、型と数値の文字列まで一致するかを見ます。一致すれば、その bytes から復元した記録は live の記録と `==` で等しくなります。bytes を作る `record_json` とは別の経路で読みます。

**計測（7日分、画面からの経路。他の重い処理が常に動いていたので、同じ負荷で同時に動かした2回で比べます。world・events・KPI のハッシュは一致。Checkpoint 674個と frame 337個は全ファイルの bytes が同じ。保存の間隔と fsync の回数は同じ）：**

| 処理 | 前（同時の回） | 後（同時の回、`record_json` の前） |
|---:|---:|---:|
| 全体 | 338.8秒 | 217.9秒 |
| `TimelineStore.record`（壁時計） | 242.1秒 | 130.4秒 |
| `record` − `os.fsync` | 220.1秒 | 101.1秒 |
| `Checkpoint.capture`（壁時計 / CPU） | 81.3 / 66.7秒 | 57.2 / 48.6秒 |
| `save_captured_branch`（壁時計 / CPU） | 49.3 / 21.2秒 | 47.6 / 11.4秒 |
| `BackgroundIndexer.submit` | 85.6秒 | 3.2秒 |
| `os.fsync` | 22.0秒 | 29.3秒 |
| `TickEngine.step` の CPU | 81.8秒 | 78.6秒 |
| 登録のプロセス（壁時計 / CPU） | 計っていません | 171.0 / 99.2秒 |

軽い回の計測はできていません。`record` の CPU の `step` の CPU に対する比は 1.31〜1.36 から約1.0 になりました。`step` が CPU 60秒、fsync が 7.5秒の負荷に直すと `record` は約71秒です（見積もりです）。

**残り：** 登録の CPU は今も約100秒で、軽い回でも計算と同じくらいの時間がかかります。これより速く計算しても、登録の待ちで止まります。`capture` で大きいのは `json.loads`（3.7 MB）、支援の依頼の復元（3,309件を毎回作ります）、履歴の比較です。

**見込み：** Checkpoint の作成 約190秒 → 約50秒。7日分は約250〜300秒（5倍前後）。

### B4：tick の残りの段階を Rust に移す

行動の提案・調停・内生イベント・予定イベントの処理（domain の規則、約2万行）を、時間のかかる順に移す計画でした。段階ごとにハッシュの一致を確かめます。

**計測で分かったこと：** 段階の時間は、規則の計算ではなく、同じものを何度も読み直す処理に広く薄く散らばっていました。そこで、まず作り直しをやめ、受け渡しの少ない2か所だけを Rust にしました。モデルの式は Rust に移していません（モデル担当の方に見ていただく式の移動はありません）。

**入れたもの：**

| 処理 | 変えたこと | 根拠・読む入力 |
|---|---|---|
| `WorldView`（提案の段階） | 更新の段階の view から作る（`WorldView.after_state_update`）。人の needs・stress・trust・体調・長期の苦痛だけを世界から読み直す | 2つの段階の間に世界へ書くのは、これらと、view が読まない状態（社会の伝播、関係の記録、指標）だけ。毎 tick の比較テスト（`tests/integration/test_tick_views.py`、メッセージと信念のある Scenario を含む） |
| 設備の形状の解析、`selected_models` | 段階ごとの作り直しをやめ、コントローラーが持ち続ける | `model_configs` を毎分（その分の最初の段階で）写しと値で比べ、違えば捨てる。分の途中で `model_configs` をその場で書く処理はなく、写して差し替える処理は別のオブジェクトになるので解析し直す |
| 部屋の中の経路（`plan_in_room_route`） | 同じ入力の結果を覚える（1分あたり約6回の計画の92%） | 形状と制約の設定（オブジェクトの同一性）、`geometry_overlay_v1` の有無、`facility_points`・`partitions`（呼ぶたびに値で比べる）。座標が 0.0 の点は `-0.0` と区別できないので覚えない |
| 区域の環境（`zone_environment_view`） | 結果を覚える | `model_configs`（同一性）と `modifiers`（呼ぶたびに値で比べる）。読むのはこの2つだけで、時刻は読まない |
| `EventQueue` | ID・順序番号・並べた列の索引を持つ | `_events` を書くのはこのクラスのメソッドだけ |
| `ScheduledEvent` の確認 | 確認と凍結を Rust で行う（`scheduled_event_checked`） | Python の確認が受け入れると分かるときだけ受け入れ、それ以外は Python が判定する |
| `WorldView` の監査ハッシュ | 文書を作らず Rust で書く（`view_state_hash`） | 出力は `canonical_sha256` と同じ。扱えない値は Python で計算する |
| 資源台帳の確認（行動の適用と指標） | 毎 tick の検証の記憶（`TickValidationMemo.reservation_ids`）を使う | 予約 ID の列を値で比べ、新しい ID だけを確かめる。失敗した操作の ID は次の比較で外れる |
| 記録の比較 | 同じオブジェクトなら `to_dict()` で比べない | `dispatch_due`、`record_applied_action_completion` |
| 共有の運営の記録の重複確認 | その分に実現したイベントは、新しい記録だけを見る | 記録はイベントの実現時刻を持つ（`record_shared_operation` の確認） |
| 支援の依頼の走査 | 指標の3回の走査を1回にする。苦情の確認は並べ替える前に絞る。同じ種類の依頼はスタッフの読み取りを共有する | 苦情の判断は依頼を pending にしない。スタッフの技能は `assign_pending` の中で変わらない |
| 値の確認 | 型が float で範囲内なら関数を呼ばない（`apply_state_deltas`、`record_distress_monitor_tick`、`validate_social_state_context`）。`ActionCandidate`、`ActionProposal.with_outcome`、行動の記録の receipt は、確認に通るときだけ生成された `__init__`・pydantic を通さない | それ以外の値は今までの確認が判定し、同じエラーになる |
| ベッドの空きの確認 | 他の人の位置を `Point2D` にせず座標で読む | 素の有限の float 以外は解析する |

**計測（7日分、画面からの経路。移植前の commit と同時に実行して同じ負荷で比較、2026-10-31）：** world・events・KPI のハッシュは一致しました。

| 処理 | 前（秒） | 後（秒） |
|---:|---:|---:|
| `propose_actions` | 34.5 | 16.7 |
| `update_agent_state` | 21.3 | 15.5 |
| `arbitrate_actions` | 21.1 | 9.4 |
| `dispatch_due_events` | 20.9 | 10.0 |
| `collect_metrics` | 9.1 | 4.0 |
| `sample_endogenous_events` | 1.6 | 1.6 |
| 段階の合計 | 108.7 | 57.3 |
| tick（`TickEngine.step`） | 122.2 | 69.0 |
| └ 毎 tick の検証（`validate_after_tick`） | 13.1 | 11.3 |
| 全体 | 291.2 | 237.0 |

保存の違いは、同じ回で 674 個の Checkpoint のファイルを比べ、すべて1バイトも違わないことを確かめました。

**移さなかったもの：** 提案の計算（`UtilityBehaviorPolicy.propose`）を Rust に移すと、入力の読み取りと、結果の `ActionCandidate` の組み立てが残るので、1 tick あたり 0.2 ms ほどしか減らない見込みです（計測ではなく見積もり）。式の移動の確認の手間に見合わないので入れていません。`AgentState` の持ち主を移すことも、この段階では要りません。

**残り：** 毎 tick の検証を除くと、残りは 1 tick あたり約 5 ms で、最大のものでも `WorldView.from_world` の約 0.5 ms です。これより先は、状態の持ち主を Rust に移さないと大きくは下がりません。支援の依頼（7日目で約3,300件）を毎分3回走査する分（約 0.2 ms）は、終わった依頼を飛ばせば減りますが、依頼の書き込みの通知を毎 tick の検証と共有する必要があるので、検証を担当する作業と合わせて考えます。

### B4 の続き：tick の残りの時間

同じ負荷で start commit（`478feae`）と並べて測りました。行の単位で見ると（cProfile は関数呼び出しの多いコードを重く見せるので、スタックを抜き取る計測を使いました）、残りは「規則の計算」ではなく、終わった依頼の走査と細かい作り直しでした。モデルの式と係数は変えていません。

**入れたもの：**

| 処理 | 変えたこと | 根拠・読む入力 |
|---|---|---|
| 支援の依頼の走査（`assign_pending`、指標、苦情の確認、スタッフの支援の完了） | `WorldState.unsettled_support_requests()` で、終わっていない依頼だけを読む | 前の毎 tick の検証の記憶（`TickValidationMemo.unsettled_requests`）。解決済み・未解決の依頼は書き込みを知らせる。辞書が同じオブジェクトで始まること（`ValuesSnapshot`）を呼ぶたびに確かめ、新しい依頼を足す。終わった依頼が開き直された、辞書が差し替わった、記憶がない（分岐の直後、`SHELTERSIM_NATIVE=0`）ときは全部を読む。順序は辞書の順で、呼ぶ側が状態を読むので、結果は全部を読むときと同じ。毎 tick の全段階の比較テスト（`test_the_unsettled_requests_equal_a_scan_in_every_tick_phase`）と、開き直し・追加・削除・差し替え・写しのテスト |
| 段階ごとのスコープ（設備の形状、`selected_models`、部屋の経路、検証の設定） | ジェネレーターの `contextmanager` をやめ、クラス（`core/context.py`）にする | 値を入れて戻すだけで、入れ子のときの動きも同じ |
| Python の GC | 計算の間の最も若い世代の回収を 50,000 個ごとから 200,000 個ごとにする（`core/collection.py`） | 結果に関わらない。後述の計測 |
| 依頼の状態・イベントの種類と状態の enum | すでに enum なら呼び直さない（検証で 1 tick 約33回、記録で約29回） | `Enum(x)` は `x` がその enum なら `x` を返す |
| `.value` の読み取り（提案、`from_world`、EventLog の索引） | `._value_` を読む | `.value` は enum の property を通すだけで、同じ値 |
| 関係のキー（`[受け手, 送り手]` の JSON） | `ordered_pair_key` で同じ入力の結果を覚える（1 tick 約44回） | 文字列2つだけの純粋な関数。ほかの型は `json` |
| 指標の `source_ids_json` | `native.canonical_json` で作る | 同じ設定の `json.dumps` と1バイト違わない（比較テストあり）。扱えない値は `json` |
| `NeedView`、`AgentStateDelta` | 生成された `__init__`・関数呼び出しを通さず dict で作る | 確認のない `NeedView`、確認済みの値の `AgentStateDelta`（`within_bounds` と同じ） |
| `from_world` の人の文脈の項目 | 解析済みの `PersonContext`（凍結）と一緒に覚える | 元の記録が変われば解析し直すので、一緒に作り直される |
| EventLog の索引（`fields_of_record`） | 属性名を intern し、来歴の値が文字列なら `Mapping` の確認を省く | 文字列は `Mapping` ではない |
| 苦痛の監視の値の確認 | `math.isfinite` を比較 1 回にする | `0.0 <= value < inf` は有限で負でないことと同じ（NaN は偽） |

**モデル担当の方に見ていただく Python の変更（結果は同じ）：** `policies/behavior.py`（`propose` の `action._value_`、`_need_view`、`_person_fields`）、`policies/psychology.py`（`native_deltas` の組み立て）、`domain/distress_monitor.py`（値の確認の書き方）、`domain/social_network.py`・`domain/body_state.py`（キーの作り方）。式と係数の変更はありません。

**GC の計測（7日目近くの 1,500 分、回収にかかった時間の合計）：** 50,000 個で 1.03秒、200,000 個で 0.45秒、1,000,000 個で 0.27秒。回収の対象の多くは、保存までの間だけ生きるオブジェクトなので、間隔を広げると読まずに済みます。200,000 より広げても得は小さくなります。

**計測（7日分、画面からの経路、2026-10-31。world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041` は一致。もう1人の作業と同時に実行し、start commit と同じ負荷で比較）：**

| 処理 | 前（秒） | 後（秒） |
|---:|---:|---:|
| `propose_actions` | 19.65 | 18.55 |
| `update_agent_state` | 18.43 | 17.70 |
| `dispatch_due_events` | 11.90 | 9.84 |
| `arbitrate_actions` | 12.02 | 7.10 |
| `collect_metrics` | 4.67 | 2.13 |
| `sample_endogenous_events` | 1.85 | 1.79 |
| `validate_after_tick` | 14.16 | 12.50 |
| tick（`TickEngine.step`） | 84.05 | 70.47 |
| 計算スレッドの CPU 時間（全体） | 191.3 | 174.0 |
| 全体（実時間） | 304.6 | 303.3 |

実時間の全体が下がっていないのは、この回では `TimelineStore.record` の実時間（228秒）の約半分が CPU を使わない待ち（CPU 時間は 104秒）で、索引のプロセスとディスクを待っていたためです。同時に複数の Run が同じディスクに書いていました。tick の短縮は、負荷がないときの `step` の 60.9秒に比（16%）で当てはめると約10秒です。7日目近くの 1,100 分だけを並べて測ると、1 tick あたり 6.14 ms から 5.46 ms（0.68 ms、7日分に直すと約7秒）で、見込みの幅は約7〜10秒です。

保存の違いは、同じ回で 1,024 個のファイルを比べ、すべて1バイトも違わないことを確かめました。残りの1つ（`history.sqlite`）はファイルの配置が違いますが、全表の 約900万行は同じです。GC の間隔を広げたときのメモリは、Run の全体では測っていません。7日目近くの分岐（読み込みの山が 4.9 GB）から 1,500 分では、山は増えませんでした。

**入れなかったもの：**

- 共有の運営の `support_requests` を、書き込みの通知（`RecordWatch`）で読み飛ばす：検証の側だけで約 0.05 ms。`record_shared_operation` の側は同じ通知を読めません（`changed` は読むと通知を消します）。短縮に見合いません。
- `used_reservation_ids` のリストの検索（1 tick 約 0.04 ms）：覚えた集合を使うには、失敗した操作の差し戻しを確かめる比較が要り、その比較も全部を読みます。
- `model_configs` の比較（検証とコントローラーで各 約 0.05 ms）：省くと、その場の書き換えを見つける根拠がなくなります。
- `view_state_hash`（約 0.16 ms）、`social_channel_update`（約 0.17 ms）、`information_contacts_without_messages`（約 0.10 ms）：時間の多くは Python の値の読み書き（1項目あたり約 30 ns）で、SHA-256 ではありません。社会の伝播は、結果の接触の dict を Python に作る分が大きく、状態の持ち主を移さないと減りません。

**残り：** 1 tick あたり約 5.0 ms（検証 約 0.9 ms を含む）。最大は `from_world` の約 0.4 ms で、ほかは 0.2 ms 以下の Python の処理に散らばっています。これより先は、1か所あたり 0.05 ms 未満の変更を重ねるか、状態の持ち主を移すことになり、前者は危険と手間に見合わないので、ここで止めました。`experiments/timeline.py` の `record_decision` の `json.dumps`（大きさの確認だけ、約 0.07 ms）は、保存の担当の範囲なのでそのままです。

### B5：OS をまたいだ決定性と、モデル版の確定

数学関数を純 Rust のライブラリ（`libm`）に固定し、3つの OS で同じ結果になることを確かめます。ここで結果が変わるので、モデル版を上げます（モデル担当の方の合意が必要です）。

## 検証のしかた

| 確かめること | 方法 |
|---|---|
| 計算の結果 | 7日分の world・events・KPI のハッシュ（`--expect`）。1日分・短い Run は各 PR で、7日分は段階ごとに測る |
| 移したまとまりの値 | 毎 tick、Python の実装と Rust の値をビット単位で比べる（`tests/integration/test_native_kernels.py`） |
| 毎 tick の検証 | 壊した状態を tick で止めること、止めるエラーが省かない検証と同じこと（`tests/integration/test_tick_validation.py`） |
| tick の中で作り直さないもの | 毎 tick、世界から読み直した値とビット単位で比べる（`tests/integration/test_tick_views.py`） |

| 保存の前の完全検証 | 古い項目と規則が読むものを壊すと同じ保存で止まること、止めるエラーが省かない検証と同じこと（`tests/integration/test_save_validation.py`） |
| 保存の形 | Rust が作る正規化した JSON・chunk・目録が、Python のものと1バイト違わないこと（B3 までは目録・ハッシュも同じ） |
| 速さ | 計測器の「Run の終わりだけを測る」機能（`--from-project`・`--fork-minute`）で、7日目の保存と tick を数分で測る |
| 3つの OS | このブランチの CI（Rust の部品を作って入れたテストと、Linux では部品なしのテスト） |

## 決めたこと（2026-10-30）

1. **Rust を必須にする：** 段階が合格したら、その段階の Python の実装は消してよい。チームには、CI が作る wheel を配るか、Rust の開発環境を入れてもらう。
2. **モデルの式も Rust に移す：** B2・B4 で、モデルの式は Rust のコードになる。式を変えるときは Rust を書く。各移植は、モデル担当の方に見ていただく。
3. **main へは、移植が終わるまで戻さない：** `feature/rust-port` で最後まで進め、終わってからまとめて戻す。main の変更は、必要に応じてこのブランチに取り込む。
   - 開発は、作業の区切りごとに作業ブランチから `feature/rust-port` への Pull Request で統合する。全テスト、ハッシュの一致、7日分の計測結果を Pull Request に書く。
   - `オフラインCI` はこの Pull Request でも自動で動く。main へ戻すときに、`ci.yml` の `pull_request` の宛先を `[main]` に戻す。
4. **B1 から始める。**

期間の目安：

| 段階 | 期間の目安 |
|---|---|
| B1 | 1〜2週 |
| B2 | 2〜3週 |
| B3 | 1〜2週 |
| B4 | 数週 |

## 危ないところ

- **窓口ごしのアクセスの遅さ：** Python のコードが窓口ごしに属性を読むと、1回あたりは今より遅くなります。まだ移していない Python のコード（移動・設備など）が人の属性を頻繁に読む場合、B2 の途中で一時的に遅くなることがあります。段階ごとに計測します。
- **「写して差し替える」処理：** 窓口の写しが、今の `from_dict(to_dict())` と同じ結果になることを、テストで確かめる必要があります。
- **EventLog を後から作ることの影響：** 記録を全件読む処理（報告・比較・完全な検証）は、B1 の後もしばらく遅いままです。全件を読む19か所のうち、時間の重いものから、Rust の中で済む形にします。

## 全経路の確認の計測（2026-10-31）

決まりと手順は[testing.md](testing.md)の「全経路の確認」「振る舞いの比較」にあります。ここには、その時点の数字を書きます。tick 側のコードは `core`、`domain`、`events`、`policies`、`experiments/runner.py` で、16,632行です（行の網羅で、分岐は数えません）。

### 届いている範囲

| 範囲 | 届いた行 |
|---:|---:|
| 今の確認（生成 Scenario、100人）1日 | 49.4% |
| 同 2日 | 49.8% |
| 同 7日 | 50.1% |
| 全経路の確認（55 Scenario、Rust の部品あり） | 79.9% |
| 同（`SHELTERSIM_NATIVE=0` の回を合わせる） | 81.4% |
| 参考：長いテストを除く全テスト | 87.2% |

7日分は1日分より116行しか増えません。行の数え方は、モジュールの読み込みで通る行（定義など）も含みます。

### まだ届いていない行（なしの回も合わせて 3,100行）

| 種類 | 行 | 理由 |
|---|---:|---|
| 拒否（`raise`、`except`） | 1,562 | 壊れた状態や不正な入力でしか通りません。全テストが、エラーの文まで確かめています |
| 検証・設定の正規化の残り | 295 | 同じです |
| tick から呼ばれないもの | 92 | `domain/layouts.py`（画面の図面の確認）と `policies/agents_social_manifest.py`（モデルの一覧） |
| 振る舞い | 1,151 | 下の表 |

| 振る舞いのうち大きいもの | 届かない理由 |
|---|---|
| 職員の交代（`schedule_staff_operation_relief`、`handoff_staff_operation`） | 手で指示した引き継ぎは別の経路で通ります。これは作業中にシフトが終わり、同じ場所に交代できる人がいるときの自動の交代です。その組み合わせを作っていません |
| 仮設の部屋への受け入れ（`room_admission_point`） | 部屋と到着を合わせた Scenario でも通っていません。通る条件は未確認です |
| 列へ歩く途中の経路の引き直し（`_replan_physical_queue_trip`） | 通路や仕切りが、その人の歩く部屋の中の経路を塞ぐ時刻を作っていません |
| 物資の輸送路の途絶（`InfrastructureEventHandler._interrupt_route`） | `supply_route_interruption` は画面の入力になく、この確認の Scenario にも入れていません |
| 一時退出からの復帰の待ち（`admit_pending_returns`、`_restore_*` の一部） | 戻る先が満員で待つ場合を作っていません |

**移植の前の決まり：** 振る舞いの行を移すときは、この確認に Scenario を足して届かせるか、毎 tick の比較テストで確かめます。拒否の行は、エラーの文を確かめるテストが守ります。

### 確認の強さ

小さな変更を入れたコードで `--expect` を実行しました。

| 変更 | 結果 |
|---|---|
| 噂を忘れる確率を3倍 | 37個の Scenario で検出 |
| 訂正を受け入れる確率を半分 | 37個の Scenario で検出 |
| 高温のストレスの率を1%増（連続の値） | `conditions` の1個で検出 |
| 噂を忘れる確率を1%増 | **検出されない** |
| 訂正を受け入れる確率を1%減 | **検出されない** |

確率のわずかな違いは、乱数との比較の結果が変わらない限りハッシュに現れません。短い Scenario では抽選の回数が少ないためです。確率の式を移すときは、ハッシュだけでなく、毎 tick のビット単位の比較テストも使います。

### 決定性

55個の Scenario すべてで、次がそろって同じでした（別のプロセス、`PYTHONHASHSEED` を変える、Rust の部品なし：各55回。保存を読む8個は `RAYON_NUM_THREADS` を1と4に変える）。分岐（保存の間の時刻、保存の時刻、分岐の分岐）、別のストアからの再開、Checkpoint の JSON の往復、記録と再生、trace level、保存の有無も、1つの Scenario の中でまっすぐの Run と同じでした。決定的でない Scenario はありません。

Linux で確かめた結果です。Windows と macOS では実行していません。

### 有効な入力で Run が止まるもの

どちらも main にもあります（`known_stops` の Scenario が今の結果として固定しています）。

1. **介入の終わりと支援の時間切れが同じ分：** `set_route_access` の期限切れと `support_request_timeout` が同じ分に来ると、`StateInvariantError`（`shared_operations_v1 care status disagrees with the persisted support request`）で止まります。共有の運営の記録は due のイベントのまとまりの後で書かれ、効果の処理はまとまりの途中で `world.to_dict()` を呼んで検証するためです。
2. **期限のある職員の不在の guard：** `set_staff_availability` に `end_minute` と guard があると、`StateInvariantError`（`only a start event may store a guard`）で止まります。`StaffUnavailabilityHandler` が復帰のイベントに `effect_program_v1` を写すときに、guard も写すためです（`events/handlers.py`）。

### 時間

他の重い作業と同時に、16コアの機械で計測しました。負荷で大きく変わります。

| 実行 | 時間 |
|---|---:|
| `--expect` | 86〜114秒 |
| `--coverage`（Rust の部品ありとなしの2回） | 199〜328秒 |
| `--determinism` | 532〜960秒（ロードアベレージが約15と30のとき） |

`--expect` は目安の5分に収まります。`--determinism` は同じ計算を6回ほど繰り返すので、混んでいると収まりません。

### seed の揺れ（`compare_behaviour.py`）

100人・2日・10個の seed を選びました。1組は1〜2分で作れます（2日は1日より日付をまたぐ処理を含み、10個で揺れの大きさが安定して見えます）。今のコードで seed 2026〜2035 と 3026〜3035 を比べた結果は、差が標準誤差の2倍を超えた KPI が 0個でした。主なものを示します。これより小さい差は、変更の効果として読めません。

| KPI | 集計 | A 平均 | A ばらつき | B 平均 | B ばらつき | 差（B−A） | 差の割合 |
|---|---|---:|---:|---:|---:|---:|---:|
| 平均ストレス | 期間平均 | 0.263 | 0.0119 | 0.268 | 0.0087 | +0.00429 | +1.6% |
| 高ストレス人数 | 期間平均 | 0.2 | 0.115 | 0.261 | 0.14 | +0.0613 | +30.7% |
| トイレ待ち人数 | 期間平均 | 1.36 | 0.279 | 1.31 | 0.283 | -0.0534 | -3.9% |
| 平均トイレ待ち時間 | 終了時 | 4.29 | 0.801 | 4.23 | 0.763 | -0.0613 | -1.4% |
| 最長トイレ待ち時間 | 終了時 | 104.3 | 17.5 | 95.3 | 19.7 | -9 | -8.6% |
| 食事待ち人数 | 期間平均 | 1.12 | 0.241 | 1.23 | 0.311 | +0.109 | +9.7% |
| 平均食事待ち時間 | 終了時 | 11.2 | 2.26 | 12.1 | 2.71 | +0.96 | +8.6% |
| スタッフ負荷率 | 期間平均 | 0.459 | 0.00396 | 0.459 | 0.00334 | -0.000723 | -0.2% |
