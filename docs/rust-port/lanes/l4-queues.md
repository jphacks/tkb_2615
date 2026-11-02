# L1・L4 レーン：すぐ閉じるものと、列・資源・人口の残り

手順4で Python の規則を消す前に、有効な Run で Rust が断っていたものを閉じました。**結果は変わりません（ハッシュは同じ）。係数の値は変えていません。** Python の規則は比較の基準として残ります。

## 棚卸しの前と後

`SHELTERSIM_REFUSAL_CENSUS`。前は `84d8eb6` をこのレーンの直前の木として測った値、後はこのレーンの最後のコードです。7日分の「前」は step4-plan.md の値です（このレーンの基準で7日分の棚卸しはしていません）。

| 理由 | 前 ゲート / 7日分 | 後 ゲート / 7日分 |
|---|---:|---:|
| `the policy has no long-term state this kernel knows` | 2,680 / 0 | **0 / 0** |
| `the namespace has no row`（`with_row`） | 67 / 1 | **0 / 0** |
| `the population state is the Python rule's` | 821 / 0 | **0 / 0** |
| `a recent receipt was not checked here` | 689 / 0 | **0 / 0** |
| `population_dynamics_v1: its kernel did not take it`（`after_tick`） | 348 / 0 | **0 / 0**（上の副作用） |
| `no longer in a contact zone` | 304 / 0 | **0 / 0** |
| `someone gives up on the queue` | 167 / 4 | **0 / 0** |
| `the residents are busy: the Python rule defers` | 108 / 0 | **0 / 0** |
| `another kind of receipt` | 69 / 0 | **0 / 0** |
| `a call raised EventSuppressed` | 54 / 0 | **0 / 0** |
| `the person is busy: the Python rule defers` | 20 / 0 | **0 / 0** |
| `the sender's belief changed` | 2 / 0 | **0 / 0** |

driver の `kernels_refused`：ゲート 3,708 → **713**、7日分 **3**（L2、L3 がマージされる前の木です。`a free worker can patrol` 2（L2）と `a request closed at once` 1（L3））。

**ゲートに残る断り（すべて他のレーンか L8）：** L2 の職員 `a free worker …`、`a staff member has a task`、`edge capacities …`。L3 の `no floor plan`、`another movement state`、`at once`。L7 の `after_tick`。L8 の文を出す枝（`the sample is stale`、`the modifier is not active`、`the provenance does not match the sender`、`no active task`、`the units …`、`a call raised StateInvariantError`、`a marker's guard`、`the Python rule refuses the change`）。**L4 の範囲で有効な Run に残る断りは0です。**

**書いた後の `__wrapped__` は `rules/population.rs`、`population_lifecycle.rs` からなくなりました。** ただし `rules/care_receipts.rs` の `ingest` に1か所足しました（下の「まだあるもの」）。

## 移したもの

| もの | 場所 | 比べているもの |
|---|---|---|
| `_abandon_and_retry`（普通の列、独立した列、帰り道、記録） | `rules/queues.rs` の `plan_leave`、`write_all_leaving` | 影（世界全体）。ゲート 167回 168人、7日分 4回 4人。テスト |
| `ResourceServiceManager.cancel`、`interrupt_for_staff` と小さな部品（`_audit_event`、`_provenance`、`_next_queue_task_id`、`_release_staff`、`requeued`） | `rules/service.rs`。Python のマネージャーを呼びません。`handlers.rs` からも `Manager::of_world` で作ります | `interrupt_for_staff` はゲート3回とテスト（独立した列の `requeued` を含む）。`cancel` のイベントは**テストの世界だけ** |
| `arbitrate` のループ（独立した列を含む）、`_arbitrate_independent_facility`、`_record_independent_queue_stall` の呼び出し | `rules/tick.rs` | `first_difference.py --both`、テスト（下）。**影は driver を通りません** |
| `_defer_while_busy`、`queue_departures` の忙しい枝、`schedule_population_retry` の止まる2つ、`temporary exit dropped`、`return skipped` | `rules/population.rs`、`population_lifecycle.rs`。Rust が `EventSuppressed` を同じ文で出します | 影（文と世界）。ゲート 54 + 20回 |
| `apply_population_presence_command` の外側 | `apply_presence_command`（`kernel_first`）。契約と写しの検証は Python を呼びます | 影。ゲート 27回 |
| `_state` の初めて使うときの状態（`create_population_dynamics_state`）、`_initial_dynamics` | 同。`rules/agent_dynamics.rs` の `create_state` | **テストだけ** |
| `record_shared_operation` の4種類の受領記録（継続、引き継ぎ、復旧、職員の作業） | `rules/common_operations.rs` の `record_other` | 影 69回（復旧 43、職員の作業 14、継続 11、引き継ぎ 1）。テスト |
| `verified` でない直近の記録 | `recent_receipts_hold`。Python と同じく `SharedOperationsState` の契約に読ませます | 影、テスト |
| 待っている人のいる `validate_population_dynamics_state` の tick の検証 | `rules/tick_validation.rs` から kernel を直接 | ゲートのハッシュ、既存の影 |
| 情報の接触の `EventSuppressed` 4つ | `rules/information_events.rs` | 影（文と世界）。ゲート 5,339回 |
| `interrupt_staff_trips_on_closed_routes`、`_interrupt_trips_of_event` | `rules/staff_operation_events.rs`、`dispatch.rs` | **ゲートでは影が0回。テストだけ**（`test_a_closed_route_stops_a_trip_as_the_rule_says`、`test_closing_a_route_stops_a_worker_walking_on_it`） |
| 長期の状態がないモデル | `rules/psychology.rs` は何もしません（Python の `getattr` と同じ）。長期の状態を持つ未知のモデルだけ断ります | 影 |
| `social_propagation_v1` が最初の tick でない | `rules/social_propagation.rs` が空から計画し、書く前に作ります。世界の組み立ては変えていません（初期の Checkpoint が変わるため） | テスト（全 tick を Python と比較）、影 |
| 独立した列の `request` と `validate_state=True` | `rules/service.rs`、`toilet.rs`。`world.validate()` は Python を呼びます（D3） | テスト |
| `rules/population.rs` の部屋の読み | `rules/overlay.rs` の `capacity_of`、`available_room_after`、`admission_point_after` に寄せました | 既存のテストとゲート |

**`handlers.rs` の直接呼び：** `queue_arrivals`、`queue_departures`、`admit_pending_returns`、`apply_arrival_approach_completion`、`finish_movement`、`record_shared_operation`、`apply_presence_command`。写しの上で断れば、ハンドラーごと Python がやり直します（前と同じ結果）。`travel_completion.rs` の `complete_presence_relocation`、`staff_operation_events.rs` の `admit_pending_arrivals` も直接です。

## 決めたこと

- **書いた後の呼び戻しの閉じ方：** 先に、後の入所が読むものを計画の段階で読みます（`check_waiting_for_admission`、`check_waiting_returns`、`check_relocation_completes`）。断るなら何も書く前です。そこを通ったあとの断りは `RuntimeError`（不具合）です。テスト：基準の木で `__wrapped__` を2か所とも通ることを `speed/lane-l4/reach-base.txt` で確かめてから閉じました（1か所は待っている人の `created_minute` が bool、もう1か所は `agent_dynamics` のない人の帰還）。
- **列を離れる人の計画：** 全員を計画（何も書かない）し、書くときは1人ずつ計画し直します。誰が離れるかは先の人に左右されません（経路は固定の間取り、混み具合は区域の人数で、離れても区域は変わりません）。
- **`EventSuppressed` は断りにしません。** 文と、世界に残る再試行が Python と同じことを影で比べます。エラー（`EventApplicationError` など）の文は今までどおり L8 です。
- **受領記録の新しい4種類の書き込み** は、行に直接ではなく Python の規則と同じ入れ物に書きます（転送で行に届きます）。1回の Run で数十回です。`verified` は付けません。tick の検証を Python が通したら付きます（今までどおり）。

## まだあるもの（手順4で要るもの）

- **`rules/care_receipts.rs` の `ingest`：** 2件目以降の受領記録を kernel が断ると `__wrapped__` で Python の文を出します（前は名前で呼んで同じことをしていました）。文を移す L8 まで残ります。全部の実行で0回ですが、**このレーンの棚卸しでは数を確かめただけで、通すテストは書いていません。**
- **名前で呼ぶもの：** `staff_position_snapshots`（`interrupt_trips`。L3 に Rust の入口があり、マージの後に直せます）、`service.cancel`（ハンドラーの持つオブジェクトのメソッド）、契約（`model_validate`、`PopulationSpec`、`PopulationPresenceCommand`、`FacilityQueueTransitionReceipt`、`SharedOperationsState`）、`copy.deepcopy`、`world.validate`、`validate_simulator_extensions`。
- **世界の組み立て：** `create_population_lifecycle_state`、`initialize_population_lifecycle_state`、`create_temporary_spaces_state`（tick の外）。
- **`arbitrate` の Python のメソッド：** Python から直接呼ぶ入口と、別のクラスのマネージャーのために残ります。Run は通りません。`_consider_due_complaints` は L2。
- **エラーの文のすべて**（L8）。`the task was already cancelled` など `EventSuppressed` でも `repr` を含むものは断りのままです。

## モデルの担当の方に見ていただくコード

**新しい係数はありません。** 同じ値が Rust にも書かれました：`retry_delay_minutes` の既定値 15、列を離れるイベントの優先度 0、ID の形（`facility-queue-abandon-`、`queue-abandon-arrival-`、`queue-abandon-travel-`、`resource-service-cancel-`）、出典 ID `ASM-NEED-WEIGHTED-PHYSICAL-QUEUE-V1`。式は `in_room_travel_minutes` の既存の Rust（`room_minutes`）を使います。上限 1,440回と 20,000件は今までの Rust のままです。

**Rust だけの条件：** 再試行の回数が bool なら断る（Python は `isinstance(True, int)` で通します）。`record_other` の `created_minute` が今より後なら断る（Python は `CareRequestState` の検証で失敗します）。

## 確認

2026-11-02、WSL2、16コア、ほかのレーンと同時です。基準 `84d8eb6`。

| 確認 | 結果 |
|---|---|
| 全テスト | Rust あり 4,508件成功、6件 skip。`SHELTERSIM_NATIVE=0` 2,079件成功、2,435件 skip。ログ `speed/lane-l4/tests-native.log`、`tests-python.log` |
| `gate_all_paths.py --expect` | 55シナリオ、問題0件（棚卸しつき） |
| `--reversed-order` | 55シナリオ、問題0件（driver は通りません） |
| `first_difference.py --both`（`split_queues`、`arrivals`、`interventions`、`operational`、`geometry`） | 5つとも違いなし（Rust と `SHELTERSIM_NATIVE=0`、driver の道） |
| 7日分（`--expect`） | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。素の回、棚卸しの回、影の回とも一致 |
| `ruff check`、`ruff format --check`、`cargo clippy`（`disallowed_methods`） | 問題なし |
| `tracked_mirror_gate.py` | **動かしていません**（指示どおり。`core/state_mirror.py` を変えていません） |

`tick_budget.py`（9600分から480 tick、3回）：**0.997**（1.081、0.915、0.997）。A/A 0.975（0.966、0.975、1.061）。上限 1.10、状態は3つとも一致。load average 約 10〜28。**揺れの中で、速くも遅くもなったとは言えません。** 7日分の素の回のスレッド時間 118 秒（同時の1回）。部品ごとの費用は測っていません。このレーンの規則は7日分で4回しか動かないものがほとんどで、毎 tick の増分は `has_independent` の読み（設備ごと）と長期の状態の入口だけです。

### 影

| 回 | 影 | 比べた量 | 違い |
|---|---|---|---:|
| ゲート全体 | `queues,population_rules,handlers_rest,staff,overlay,service`（`python_path_only`） | 値と bytes 2,813,269,615。379プロセス | **0** |
| ゲート全体 | `update,handlers_rest` | 110プロセス。`no_long_term_state` 2,680、社会の kernel 28,410 | **0** |
| 7日分 | `queues,population_rules,update`（`--duration 10080`、`timeout` 内で完走、`Error in sitecustomize` 0件） | 112,615,568。`leaving` 4回 4人 13,909,792 bytes | **0** |

全部の影の回を最初に1回動かしましたが、**直す前のコードと影です**（2シナリオが影の不具合で落ち、35件の違いはすべて `shadow_staff.py` の `record.state_changes` の不具合）。直したあとの全部の影は回していません。組み合わせの影は上の2回です。

| 規則（ゲート） | 呼び出し / kernel が動いた | 内訳 |
|---|---:|---|
| `prepare_facility_queue` の離れる人 | 167 / 167 | 168人 |
| `queue_departures` | 56 / 56 | 54 は同じ `EventSuppressed`、世界も比較 |
| `apply_presence_command` | 27 / 27 | 20 は同じ `EventSuppressed`、世界も比較 |
| `record_shared_operation` の4種類 | 69 / 69 | 復旧 43、職員の作業 14、継続 11、引き継ぎ 1 |
| `information_contact_update` | 5,339 / 5,338 | 断り1 は Python も失敗 |
| `effect_program_population` | 92 / 92 | |
| `interrupt_for_staff` | 3 / 3 | |
| `prepare_independent_facility_queue` | 818 / 818 | |
| `interrupt_trips` | **0** | テストだけ |

### 感度（コピーで1か所変更）

| 変更 | ハッシュ | 影 | テスト |
|---|---|---|---|
| 再試行の既定 15 → 16（1% に一番近い整数） | 一致（`generated_project`） | 0件 | 7件失敗 |
| 帰り道の due +1（1% ではない） | **不一致**（`conditions`） | **12件**（`conditions`、`shadow_queues`）。`long_generated` では0件 | 1件失敗 |
| `requeued` の順番 1 → 2（1% ではない） | 一致（`split_queues`） | 0件 | **最初は0件。** テストを足して1件失敗 |
| 上限の文（`bounded deferral limit` を変更） | 一致（`arrivals`） | 0件（上限に届きません） | 1件失敗 |

**再試行の既定値と文はテストだけが見ます。** 既定値は生成 Scenario が設定に書くためです。

### 通る前の確認

基準の木で、書いた後の `__wrapped__` 2か所を実際に通しました（`speed/lane-l4/reach-base.txt`）。全テスト、ゲート、7日分で0回だった場所です。

## 確かめていないこと、危ないところ

- **Windows と macOS では動かしていません。** 新しい C API はありません。
- **7日分が届くのは `_abandon_and_retry` の4回と読むだけの入口です。** cancel、割り込み、初めて使う状態、閉じた経路は7日分にありません。ゲートでも回数は少なく、閉じた経路の影は0回です。
- **`driver` の独立した列のループは影を通りません。** `first_difference.py --both`（5シナリオ）とテストの比較だけです。
- **`check_relocation_completes` は歩く書き込みの前の世界を読みます。** 後の `complete_presence_relocation` がそれと違う理由で断ると `RuntimeError` です。ゲートとテストで起きていません。
- **影は Python を動かすので、影の回の時間は比較に使えません。**
- `check_waiting_for_admission` は、部屋のある古い形（`room_admissions` がない）を断ります。Python の規則が動きます。手順4の前に L8 か L9 で扱いが要ります。

## レーンとの重なり

- **L2：** `rules/staff_operation_events.rs` は `interrupt_trips` の追加と `Work::Intake` の2か所。`rules/staff.rs` は触っていません。
- **L3：** `rules/queues.rs`（L3 も46行変更）、`handlers.rs`（8行）、`care_receipts.rs`（10行）、`travel_completion.rs`（46行）、`tick.rs`（`arbitrate` まわり、L3 は6行）で衝突があるかもしれません。`movement.rs`、`proposals.rs` は触っていません。
- `benchmarks/rule_shadow/`：`shadow_queues.py`、`shadow_population_rules.py`（単独で動かせるよう `python_path_only`）、`shadow_handlers_rest.py`、`shadow_staff.py`、`sitecustomize.py`。
- `driver.py` の `DRIVEN_RULES` に6行。`native/README.md` の1段落。
