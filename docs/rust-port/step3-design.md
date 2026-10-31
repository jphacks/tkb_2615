# 手順3の設計：tick の状態と計算を Rust に移す

[rust-port.md](../rust-port.md) の B2・B4 で入れた改善の先にある、`domain`・`events`・`policies`・runner の毎 tick の経路と状態を Rust に移す設計です。ここに書くのは今の決定です。P1 と P3 の実験の後で変わったところは、元の案を残さず、変わった後の内容を一度だけ書いてあります。

計測値には出どころを付けます。

- **設計調査：** Python の試作（メモリ上）で、日7の Scenario を使った回
- **P0：** ゲートと digest の道具の整備
- **P1：** 追跡ミラーをゲート全体に掛けた実験（[lanes/p1-tracked-mirror.md](lanes/p1-tracked-mirror.md)）
- **P3：** 境界の費用の実験（[lanes/p3-skeleton.md](lanes/p3-skeleton.md)）

レーンごとの記録は `docs/rust-port/lanes/<レーン>.md` に置きます。

## 利用者の決定

- 完了の定義：`domain`・`events`・`policies`・runner の毎 tick の経路と状態をすべて Rust に移すこと。その後に手順4（libm、RNG の整理、並列化、モデル版を上げる、Python のモデルコードの削除、旧モデルの Run は閲覧のみ、CI の「Rust なし」の回の削除）と、3つの OS での決定性の確認を行います。
- LLM の呼び出しと、web・region・storage の外側は Python のままです。
- 手順3の間は Python と1バイト違わない結果にします。区切りごとにタグを付けます。
- 係数と式の意味は変えません。保存の間隔と fsync の回数も変えません。

## 状態の持ち方

### 基本の作り

- **Python から見たとき：** 名前空間の移植を始めた時点から、Rust が状態を持ちます。まだ移していない Python のコードは、`model_state` には双方向に追跡するミラー、エンティティには窓口のクラスを通して動かし続けます。名前空間ごとに少しずつ移し、先に直列の前段の PR を入れます。tick の駆動は最後まで Python に置きます。
- **採らなかった案：**
  - dict 風の PyO3 の view：`isinstance(dict|Mapping)` が520か所、`json.dumps` があり、abi3 では dict を継承できません。
  - 名前空間の貸し出しと返却（check-out/check-in）：境界を越えるたびに名前空間の大きさに比例します。`_update_movement` は `advance_movement` の間も `state` を持っています。
  - 規則を先に、状態を最後に移す順序。
- **`WorldCore`：** `world.__dict__["_core"]` に入れる pyclass です。WorldState は Python の dataclass のままなので、`world.__dict__.clear()/update()` で差し替える処理はそのまま動きます。Rust は Python のコールバックの後に毎回 `world._core` を読み直します。
- **`SHELTERSIM_NATIVE=0`：** core を作らず、素の dict と dataclass で動きます。

### `model_state`

- `NamespaceTable(dict)` にします。Rust が持つ名前空間は、`TrackedDict(dict)` と `TrackedList(list)` の本物の木です。読みは C の速さで、`isinstance`、json、別名、入れ子の書き換えがそのまま動きます。
- すべての書き込み（`__setitem__`、pop、setdefault、update、sort、スライスなど）は、`(path, key, value)` を Rust にも転送します。Rust 側の書き込みは、行を更新し、C API でミラーを書きます（上書きしたメソッドを通りません）。
- **切り離し：** 毎 tick に読む Python のコードが名前空間になくなったら、ミラーを捨てます。後で Python が触れたら作り直します。
- **ミラーを持つ範囲：** Rust が持つ名前空間だけを、できるだけ少ない数で同時に写します（P3 の結果。下の「P3 の結果」を参照）。
- **PEP 697 の dict 継承：** Rust の `mp_ass_subscript` を持つ dict の継承クラスは abi3-py312 で動きます（転送 21〜168 ns。Python の上書きは、path のタプルを付けて 130〜821 ns）。Linux の 3.12 だけで、`d[k]=x` だけです。P4 の予備にします。path のタプルではなく、ノードの ID を使います。

### エンティティ

- AgentState、NeedState、FacilityState、StaffState、SupportRequestState、ActiveTask は PyO3 の窓口にします（Rust の行につながっている状態と、切り離された状態があります）。
- コンテナは `IdMap(dict)` にします。読みは継承し、書きだけを横取りします。`world.agents` の書き込みは5か所だけです。
- enum と ID はキャッシュした単一のオブジェクトを返すので、`is Activity.IDLE` が成り立ちます。
- `WorldState.__setattr__` が、`world.support_requests = ...` のようなフィールド全体の代入を捕まえます。
- **不正な値：** 型付きのフィールドに合わない値の転送書き込みは、その名前空間を汚染（taint）します。Rust は `Unsupported` を返し、Python の規則がミラー上で動くので、壊した状態のテストは今のエラーの文のまま通ります。

### 表現

- **型付きの行：** `agents`、`psychological_distress_v1`、`social_propagation_v1`、`agent_dynamics_v1`、`staff_dynamics_v1`、`facility_queues_v1`。`staff_operations_v1`、`movement_v1`、`information_spread_v1` も型付きにします（自由な形の部分は V）。`social_network_v1.relationships`、`body_state_v1.agents/relationships` も同じです。
- **追記専用の正準 bytes と、規則が読む項目の索引：** `shared_operations_v1.history`、`body_state_v1.history`、`resource_ledger_v1.audit`、`resource_service_v1.request_history`、`queue_stall_history`（21 MB のうち約18 MB）。`care_receipt_for_request` は 1 tick に140回呼ばれるので、索引が要ります。
- **V の木と、世代ごとの解析済みキャッシュ：** `geometry_overlay_v1`、`effect_program_v1`、`operational_events_v1`、`infrastructure_v1`、`zone_environment_v1`、`temporary_spaces_v1`、`population_*`、`llm`、実行時の名前空間、`model_configs`。
- **P1 の結果を受けた表現の変更：**
  - 履歴は先頭を切り詰められる必要があります（`shared_operations_v1` は追記のたびに `del [:-N]` を呼びます）。
  - `population_presence_v1.history` は毎回丸ごと置き換えるので、V のままにします。
  - `psychological_distress_v1`、`social_propagation_v1`、`information_spread_v1` は型付きのままですが、どれも 1 tick に名前空間ごと1回の書き込みで、700〜900個の値になります。
  - 次の項目は Option が要ります：`social_network_v1.relationships`（trust、last_contact_minute）、`body_state_v1.relationships`、`agent_dynamics_v1`、`staff_operations_v1.staff`。`facility_queues_v1.last_stall` と `movement_v1.positions.point` は dict か None なので、V か Option にします。
  - 書き込みを受ける要素の木：`population_dynamics_v1.arrivals[]` と `pending_arrivals[]` だけです。
  - `information_spread_v1` のキーの順序は Rust と Python で違いますが、結果には影響しません（2つの逆順の回の両方で順序に依存しません）。
  - ゲートには int と float の混在、タプル、str 以外のキーはありません。str 以外のキーはテストの入力にだけあります。

### バイト一致

- `V = Null|Bool|Int(i64)|Big(Box<str>)|Float(f64)|Str(Arc<str>)|List|Map(IndexMap)`。
- 型付きの f64 は正確な float だけ、i64 は正確な int だけを受け入れ、それ以外は汚染します。
- 正準な書き出しは、`native/src/lib.rs` の `Encoder` と `write_json_string` を使い回し、キーはバイト順に並べます。
- `IndexMap` は常に `shift_remove` を使い、`swap_remove` は禁止です。
- 行は Py のハンドルを持ちません。ミラーのハンドルは別の表に置くので、行は Send + Sync です。

## 壊れやすいところ

1. **挿入してから別名で書き換える：** `domain/body_state.py:805`（`record = relationships[key] = {...}`）で確認済みです。直さないと10分目でずれます。AST の走査で見つけた確認すべき候補は `events/effects.py:1590`、`:2317`、`domain/population.py:695` です。直します。lint は `tests/unit/repo/test_tracked_write_lint.py` にあります。
2. **型を厳密に見る kernel：** `native/src/validation.rs` の `is_exact_dict` は追跡の型で false を返し、毎 tick の検証を Python に戻します。P4 で受け入れるようにします。ほかに `native.py` の `type(current) is not list`（`same_json_items`）と `contacts.rs` の `exact_dict` も、追跡の型を拒みます（P4 で直す）。
3. **切り離しをまたいで Python が持つ別名：** テストに多いと見ています。死んだノードへの書き込みはエラーになり、古い値を読むテストは直す必要があります。切り離したノードへの書き込みは、`src` から0件、テストから5件です（`test_tick_validation.py`）。
4. **`asdict`：** `asdict(item.needs)`（`experiments/comparison.py:103`）、職員・タスク・辺への `asdict` は、`.to_dict()` に変えます。
5. **`deepcopy(world)`：** `storage/projects.py` の `_copy_restored` は `__setattr__` を通らず、素の dict の世界になります。P4 で `WorldState.__deepcopy__` が要ります。
6. **共有と別名は実在する：** `staff_dynamics_v1.config.shift_windows_by_staff` と `staff_operations_v1.config.*` は `model_configs` と、`geometry_overlay_v1` の `source_ids` は `history[]` と、`population_dynamics_v1.profile_traits` は `pending_arrivals[]`・`departures[]` の traits と、同じオブジェクトです。共有の後に書き込みはなく、JSON にすると共有は消えるので結果には影響しませんが、共有がないとは仮定しません。
7. **段階をまたぐ別名：** 社会関係の記録は `watch.rs` が、履歴の項目は `TickValidationMemo` が、解析済みの設定は `model_configs` のノードを持ちます。
8. **Rust の `set_item`：** `native/src` では新しい dict か `.copy()` だけを書くので、P4 で型の検査をゆるめても、今は迂回できる道は開きません。

## 計測

### 設計調査（Python の試作、メモリ上）

- 転送する書き込みの流れを持つ追跡の dict/list の継承クラスと素のものを、日7の Scenario の240分で同じ歩調で動かし、別名を1か所直した状態で、どの段階でも差は0でした。ゲートの `with_rumors` の360分も、最終のハッシュが同じでした。直さないと10分目でずれます。
- 段階の終わりごとに、`model_state` のすべての dict と `world.agents` を逆順にしたり並べ替えたりしても、2つの Scenario でハッシュは変わりませんでした。
- 追跡した書き込みは、最初の Scenario で 1 tick あたり961件（`body_state_v1` が734件）、`with_rumors` で329件です。純 Python では目に見えるオーバーヘッドはありませんでした。
- 240分目の生きている状態には、タプル、str 以外のキー、共有された変更可能なオブジェクトがありません。日7の文書には、int と float が混ざる数値の path がありません。イベントの内訳は、`agent_action_lifecycle` 60.7%、`social_interaction` 20.8%、`agent_travel_complete` 8.8% です。
- **確かめていないこと：** Rust の kernel を有効にした状態でのミラー、「写して差し替える」Scenario、段階の途中の dict の順序、PyO3 の実際の転送の費用（後の P3 で計測）。

### P1 の結果

- 追跡ミラーはすべての書き込みを見ていました。55個の Scenario × 4つの変種で、31,530 tick、段階の終わり 220,710回、ずれは0件。ミラーを掛けた全テストでは、段階の終わり 627,284回、ずれは0件です。Rust の境界そのものは、ここでは試していません。
- `resource_ledger_v1` が転送量の92.9%です。操作のたびに、名前空間を `{**current, "audit": list(current["audit"])}` に置き換えています（`domain/resources.py`）。その場で追記する形に変えるか、到着時に見分ける必要があり、台帳と一緒に移します。
- ハーネス：`tests/support/tracked_mirror.py`、`tracked_mirror_plugin.py`（`-p support.tracked_mirror_plugin`）、`benchmarks/tracked_mirror_gate.py`、lint の `tests/unit/repo/test_tracked_write_lint.py`。

### P3 の結果（境界の実験）

- 19個の名前空間をすべて同時に写すと、tick が +18%〜+36% 遅くなり、止める基準の25%を超えます。
- 1項目の転送書き込みは問題ありません（0.12〜1.3 µs）。記録を丸ごと入れる書き込みは 2.2〜8.2 µs で、1 tick に約115回あります。
- 名前空間ごとの書き込みは基準を大きく超えます：
  - `resource_ledger_v1`（`domain/resources.py:642`）60 ms
  - `psychological_distress_v1`（`runner.py:661`）240 µs
  - `information_spread_v1`（`runner.py:760`）300 µs
  - `social_propagation_v1`（`runner.py:598`）30 µs
- 読みの多い場所：後半の 1 tick のエンティティの読みは24,388回で、`SupportRequestState.status` だけで10,397回です。窓口の費用は 1 tick あたり約 0.24 ms（計算）です。Rust の行の書き込みとミラーの書き込みは、合わせて 11〜88 ns です。
- 骨組み（マージ済み）の内容は次の「前段」にあります。`powf(x, 2.0)` が `x * x` になり、`x ** 2` と100万個中790個で値が違う例も、そこで確かめています。

## 前段（直列）

- **P0（マージ済み）：** ゲートのマージ。段階ごと・名前空間ごとの digest の流れ（2つの流れの差で、最初の悪い分・段階・名前空間が分かります）。すべてのゲートの Scenario での型・タプル・別名の調査。逆順の変種。道具は `benchmarks/first_difference.py`（最初の分・段階・名前空間・値）と `benchmarks/tick_probe.py` です。`--both` で Rust と Python を比べます。Rust の中の抽選は記録されないので、確率の式を移すときは [lanes/p0-digest.md](lanes/p0-digest.md) の手順で、ビット単位の比較を足します。
- **P1（マージ済み）：** 挿入してから書き換える場所の修正、AST の lint、追跡ミラー（Python の試作）を「写して差し替える」Scenario を含むゲート全体に掛けること。結果は上の「P1 の結果」。
- **P2（マージ済み）：** 機械的な分割。`_run_phase`、`_apply_proposal`、`_update_movement` を `experiments/phases/{dispatch,update,propose,arbitrate,sample,metrics}.py` に。`policies/behavior.py` を `policies/world_view.py` と scorer に、再エクスポートを付けて。`contacts.rs` を social と information に。
- **P3（マージ済み）：** Rust の骨組み。
  - `native/build.rs` が `src/state/*.rs` と `src/rules/*.rs`（それぞれ `register(m)` を出す）を走査するので、`lib.rs` は編集しません。
  - `native/src/core/{value,mathx,rng,interner}.rs`、`native/src/rules/parity.rs`、`clippy.toml`。
  - レーンは `sheltersim_native.rules.<名前>` と `.state.<名前>` として現れ、`native.rules()`・`native.state()` で使います。新しい Python の wrapper は `native.rules("movement")` を通し、`native.py` には足しません。新しい crate はここですべて足します。
  - `clippy.toml` で `f64::{exp, exp_m1, powf, hypot, max, min}` を `mathx` の外で禁止し、`swap_remove` も禁止します。
  - `exp`、`expm1`、`pow`、`hypot`、`max`、`min`、`sum`、`round` はすべて `mathx` を通します。`pow(black_box(x), black_box(y))` は本当に libm を呼びますが、`powf(x, 2.0)` は `mulsd` にコンパイルされ、`x ** 2` と100万個中790個で値が違いました。
  - 各 PR の前にローカルで実行します：`cargo clippy --manifest-path native/Cargo.toml -- -A clippy::all -D clippy::disallowed_methods`
  - 確率の式ごとのパリティの fuzz を持ちます。
- **P4：** `core/state_mirror.py`、何も持たない `WorldCore`、追跡の型を受け入れる厳密型の kernel、検証を省く登録表（レーンが `core/world.py` を編集しなくて済むように）。
- **P5：** 雛形になる名前空間を最後まで通します。`psychological_distress_v1` と `metric_state`。`state_row!` マクロが `from_py`、`to_py`、`write_json`、ミラーの setter、転送書き込みの処理を生成します。**心理の置き換え（`runner.py:661`）は残しません。**
- **P6：** すべてのエンティティの窓口と `IdMap` を1つの PR で（`domain/models.py`、`core/world.py`）。

引用した runner の行は、`experiments/phases/` に移っています。`update.py` の `_social_propagation` と `_update_long_term_psychology`、`sample.py` の `_sample_information_contacts`、`arbitrate.py` の `_consider_due_complaints`、`dispatch.py` の `_consider_social_contact_events`、`propose.py` の `_apply_proposal` と `_prioritize_support_request` です。

## 境界の費用のルール（P3 の結果で決めたこと）

1. **名前空間ごとの書き込みは転送しません。** それを行うコードは、その名前空間と同じ PR で移します：`apply_resource_operation` は台帳と、`update_long_term` と `runner.py:661` は心理と、というように。
2. **ミラーを持つのは Rust が持つ名前空間だけ**で、同時にできるだけ少なくします。レーンごとに tick の予算を持ちます。日7の分岐の上で、基準とレーンを同時に動かして測ります（スレッドの CPU 時間、3回以上、A/A の対照つき）。1レーンで tick +10% までとし、超えたレーンは、マージの前に理由を報告します。速さの判定はタグで行いますが、ミラーの合計の費用は常に +25% 未満にします。
3. 次に効く費用は記録を丸ごと入れる書き込みです。問題になったら、包む処理を Rust に移します。
4. 転送の書き込みが 3 µs を超える、または tick が 25% を超えて遅くなる場合は、止めて見直します。

## マイルストーン

見積もりは 120 tick の1回のプロファイルからの換算で、移した後のコードを Python の時間の10〜15%として計算しています。現在の tick は 60.9秒です。

| 区切り | tick の見込み | 中身 |
|---|---:|---|
| M1 | 約53秒 | 心理、`apply_state_deltas`、苦痛の監視、長期の状態、社会の伝播、情報、検証。先行して `domain/staff_dynamics.py`（370行）を、テストとエラーつきで移し、かかった時間で M3〜M6 を計画し直す |
| M2 | 約41秒 | `WorldView`、`UtilityBehaviorPolicy.propose`、`action_lifecycle`、`agent_dynamics`、Rust の `EventQueue` と `EventStore` の追記 |
| M3 | 約33秒 | `body_state`、`social_network`、`social_dialogue`、ハンドラーの `SocialInteractionHandler` と `AgentTravelCompletionHandler`、Rust の dispatch ループ |
| M4 | 約17秒 | `movement`、`geometry_overlay` の読み、`facility_queues`、`queueing`、`facilities`、`staff*`、`resources`、`resource_service`、`care_receipts`、`common_operations`、`_apply_proposal` |
| M5 | 約12秒 | `metrics`、`operational_kpis`、毎 tick に残るハンドラー、残りの検証 |
| M6 | 約12秒 | `effects.py`、`handlers.py` の残り、`population*`、`temporary_spaces`、分岐のコマンド、Rust の driver |

保存：名前空間ごとの正準 bytes と検証の省略を、名前空間を持つたびに入れます。`capture` は約12秒の見積もりで、手順3の終わりの全体は約50〜75秒です。

**driver：** 各段階は、Rust か Python の部分の並びとして `experiments/phases/*.py` に書きます。dispatch は M3 で移します。Rust は `event_type -> Rust の関数 | PyHandler` の表を持ち、PyHandler には、必要になったときに作る `ScheduledEvent` と世界を渡します。コールバックをまたいで `PyRefMut` を持ちません。

## レーン

レーンごとに `docs/rust-port/lanes/<レーン>.md` を書きます。

**M1**

| レーン | 中身 | 場所（Rust / Python） |
|---|---|---|
| A 心理 | | `rules/psychology.rs` / `policies/psychology.py`、`domain/psychological_state.py`、`domain/distress_monitor.py` |
| B 伝播 | | `state/social_propagation.rs`、`rules/social_propagation.rs` / `policies/social.py`、`policies/alternatives.py` |
| C 情報 | メッセージがある場合を含む | `state/information.rs`、`rules/information.rs` / `policies/information.py`、`domain/information.py` |
| V 保存 | | `core/canonical.rs` / `core/checkpoint_cache.py`、`core/save_validation.py` |
| T tick の検証 | 最後に、直列で。`validation.rs`、`watch.rs` の一部をやめる | `core/tick_validation.py` |

**M2**

| レーン | 前提 | 場所（Rust / Python） |
|---|---|---|
| E イベント | | `state/events.rs`、`eventlog.rs`、`records.rs` / `events/queue.py`、`events/models.py` |
| F 行動の周期 | E | `rules/action_lifecycle.rs`、`state/agent_dynamics.rs` / `domain/action_lifecycle.py`、`domain/agent_dynamics.py` |
| G view | A、C | `rules/world_view.rs` / `policies/world_view.py` |
| H scorer | G | `rules/behavior.rs` / `policies/behavior.py` |
| I 移動（状態だけ） | | `state/movement.rs` / `domain/movement.py` の `_state` |
| J 判断の記録 | F、H | `rules/decisions.rs` / `experiments/phases/propose.py`。`record_decision` は7日分（trace なし）で87,464回 |

**レーンごとの完了の条件：** ゲートのハッシュが変わらない。digest の差が出ない。`SHELTERSIM_NATIVE=0` が通る。そのモジュールのゲートでの網羅を報告する。`Unsupported` の件数を報告する。

## 難所

- **「写して差し替える」処理：** 手順3では変えません（全部を作り直すと新しい `_core` ができ、`__dict__` の差し替えがそれを運びます。`to_dict` の並べ替えもそのままです）。後で `with transaction(world)` と `core.fork()` に置き換えます。
- **`register_policy`：** 呼び出しは `policies/registry.py` だけです。`rules/policy_table.rs` を `(domain, model_id)` で引き、知らない ID は `Unsupported` を返して、Python の factory が動きます。`model_signatures` は変えません。
- **trace：** 規則は `TraceSink`（`NoTrace`、`JsonTrace`）に対して汎用にします。レーンが同じ文書を出すまでは、`Unsupported` で trace 付きの Run を Python に任せます。ゲートを、trace のファイルも比べるように広げます。
- **tick の途中の Provider：** `experiments/runner.py:1366`、`llm/runtime.py:509,798`、呼び出しは runner の862、895、914、1031行です（引用した runner の行は、上の注のとおり移動済み）。同じ場所に hook を置き、借用は持ちません。`llm` と実行時の名前空間は V のままです。移すのは却下の事前確認だけです。
- **エラーと pydantic：** Rust は受け入れるか `Unsupported` を返し、エラーの文は Python が出します。pydantic は Scenario、効果のコマンド、LLM の境界に残し、熱い経路は素の struct にします。手順4に持ち越すエラーの負債は、raise が1,373か所、`match=` つきのテストが328件です。規則を移すときに文も一緒に移し、CI では Rust がエラーを返したら Python も動かして文を比べます。
- **規則への monkeypatch：** `WorldView.from_world`、`overlay.installed_facility_point`、`common_operations._staff_operations_json`、`resources._validate_audit_record`、`metrics.support_wait_reason_codes`、`behavior._view_state_hash`、`WorldState.validate/from_dict/same_state` が対象です。`@pytest.mark.python_rules` が Rust の dispatch を切ります。移した名前を、この印なしで patch していたら lint が失敗します。

## 手順4の準備

- **RNG：** 状態を持ちません（seed と JSON のキーの SHA-256）ので、抽選の順序は結果に関わりません。キーの多くは人ごとです。`events/handlers.py:438,643,772` は、割り当てた連番を含む `event_id` をキーにします。ラベルは `rng.rs` の1つのキー作りにまとめます。`CounterRNG` でないものは Python に任せます（`policies/social.py:398`、`policies/information.py:315`）。
- **libm：** `exp`（3か所）、`expm1`（5か所）、`**` だけが依存します。`hypot` と `fsum` は CPython のアルゴリズムです。すべて `core/mathx.rs` を通します。`psychology.rs:271` は移す必要があります。LLVM は `powf(x, 2.0)` を `x*x` にすることがあるので、指数に `black_box` を付けて libc の `pow` を呼び、確かめます。
- **並列：** 規則は `compute(&Frozen, ix) -> Out` と `apply(&mut World, ID 順の出力)` に分け、`for_each_agent` で回します。手順3では直列です。行に `Cell`/`RefCell` を置きません。並列にしやすいのは `propose_due`、心理、接触の抽選、長期の状態です。
- **並列を妨げるもの：**
  - `id_counters["event"]`（`allocate_event_sequence`、`core/world.py:1429`）
  - `_apply_proposal`：`reserve_sleep_bed`、列、職員の依頼が、先に処理した人の結果を見るので直列
  - `advance_movement`（`domain/movement.py:397`）：逆順のテストが要る
  - 履歴とイベントログの追記
  - ContextVar のメモ、`lru_cache`、`_person_contexts`
  - ミラーは GIL が要る
- 並列にできる部分は、100人では 2〜3秒かもしれません。手順4で優先度を考え直します。

## 危ないところと、最初にする実験

- **転送する書き込みの経路の見落とし（静かなずれ）：** ゲート全体への追跡ミラー、「写して差し替える」Scenario を含めて（P1、完了）。
- **境界の費用で得が消える：** P4 の前に、PyO3 の窓口（str、enum、`agent.needs`、転送書き込み、ミラーの書き込み）を計測し、M1 を日7の分岐から測ります。止める基準は上の「境界の費用のルール」の4です。P3 で一部を計測しました（上の「P3 の結果」）。
- **float の同一性の落とし穴：** P3 で、3つの OS で `mathx` を CPython と fuzz します。`round(x, 8)`、`math.hypot`（14か所）、`sum`、int を返す `max(0, x)`、`**` を含みます。
- **型付きの項目に int と float が混ざる：** ゲート全体で調べます（P0）。汚染するか、`Num` のフィールドにします。
- **ゲートが Rust の通る道に届かない：** レーンごとに、網羅の基準を持ちます。
- **保存：** 40秒の説明がつかない時間は、レーン A で計っています。
- **大きさで止まる：** `staff_dynamics` の先行の移植で、M3〜M6 を計画し直します。
- **メモリ：** P5 と各タグで RSS を測ります。
- **CPython は 3.12 に固定**します（手順4まで。`sum` と `hypot` の一致のため）。
