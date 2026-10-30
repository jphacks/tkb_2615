# Rust 移植の設計（段階B）

ブランチ `feature/rust-port` で進める移植の設計です。計測と経緯は Issue #272 にあります。

## 目的

100人・7日分の Run を、画面から約5分（約318秒）以内で計算できるようにします。Python で入れられる改善は入れ終わり、develop（タグ `python-perf-2026-10-30`）で7日分は 650.5秒です。このブランチでは、B1、tick の計算、tick の終わりの検証、保存の変更を合わせて 261.0秒です（2026-10-31、B4 の前）。

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

- **社会の伝播（`ExpressivenessChannelOpennessPolicy`）：** 接触の抽選と式を Rust で計算します（`native/src/contacts.rs` の `social_channel_update`）。入力は毎 tick 受け渡しますが、100人分で小さいので、受け渡しの手間で得は消えません。
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

**残り：** 保存の前の完全検証（`world.validate()`）が、7日目近くで1回あたり約150 ms、`Checkpoint.capture` の約半分です。追記専用の履歴を毎回読み直しています（共有の運営の履歴と資源台帳の監査記録、各約8,000件）。検証の本体を変えずに減らす方法がないので、検証を担当する作業に任せます。確かめ済みの項目を飛ばせれば、全体で40秒ほど減る見込みです。

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

@@B4TABLE@@

**移さなかったもの：** 提案の計算（`UtilityBehaviorPolicy.propose`）を Rust に移すと、入力の読み取りと、結果の `ActionCandidate` の組み立てが残るので、1 tick あたり 0.2 ms ほどしか減らない見込みです（計測ではなく見積もり）。式の移動の確認の手間に見合わないので入れていません。`AgentState` の持ち主を移すことも、この段階では要りません。

**残り：** 毎 tick の検証を除くと、残りは 1 tick あたり約 5 ms で、最大のものでも `WorldView.from_world` の約 0.5 ms です。これより先は、状態の持ち主を Rust に移さないと大きくは下がりません。支援の依頼（7日目で約3,300件）を毎分3回走査する分（約 0.2 ms）は、終わった依頼を飛ばせば減りますが、依頼の書き込みの通知を毎 tick の検証と共有する必要があるので、検証を担当する作業と合わせて考えます。

### B5：OS をまたいだ決定性と、モデル版の確定

数学関数を純 Rust のライブラリ（`libm`）に固定し、3つの OS で同じ結果になることを確かめます。ここで結果が変わるので、モデル版を上げます（モデル担当の方の合意が必要です）。

## 検証のしかた

| 確かめること | 方法 |
|---|---|
| 計算の結果 | 7日分の world・events・KPI のハッシュ（`--expect`）。1日分・短い Run は各 PR で、7日分は段階ごとに測る |
| 移したまとまりの値 | 毎 tick、Python の実装と Rust の値をビット単位で比べる（`tests/integration/test_native_kernels.py`） |
| 毎 tick の検証 | 壊した状態を tick で止めること、止めるエラーが省かない検証と同じこと（`tests/integration/test_tick_validation.py`） |
| tick の中で作り直さないもの | 毎 tick、世界から読み直した値とビット単位で比べる（`tests/integration/test_tick_views.py`） |
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
