# L8a レーン：保存されるイベントの文（抑止と失敗）

手順4で Python の規則を消す前に、kernel が出しうる `EventSuppressed` と `EventApplicationError` の文を Rust に移しました。この2つの文は `dispatch_one` が `detail = str(exc)` としてイベントの記録に書き、ハッシュと画面に出る**保存されるデータ**です。**結果は変わりません（ハッシュ、ゲート、7日分は同じ）。係数は触っていません。** 保存されない文（`ValueError`、`StateInvariantError`、契約のエラー）は L8b（`rust/l8-texts-rest`）です。

## 比べる道具

| もの | 場所 | すること |
|---|---|---|
| 文の1つの出口 | `native/src/core/errors.rs` | `errors::suppressed(py, text)`、`errors::failed(py, text)`。クラスは `events/models.py` のものを import して投げます。作った例外には `_sheltersim_native_outcome` の印が付き、`errors::made_here` で Python の呼び出しが投げたものと区別します（`effects::refused_by_python` は印のあるものを断りに替えずに通します）。比較が入っているときだけ、作った場所（`#[track_caller]`）を数えます |
| 型の検査 | 同じファイル | `int_not_bool`、`exactly_int`、`str_of`、`nonempty_str`、`real_number`、`member_of`、`repr`、`repr_str`。Python の `isinstance` と同じところで「違う」と言い、Rust で決められないもの（`str` の派生型、64 bit を超える int）だけ `Unsupported` |
| 比較 | `benchmarks/rule_shadow/text_check.py` | kernel が文を出すたびに、同じ入力で Python の規則を動かして、クラスと文を比べます。入口は2つ：`core/kernels.py` の `_outcome_check`（`kernel_first`。`before` と `after` を持つ物）と、dispatch の直接の呼び出し（`dispatch.texts_checker`）。`SHELTERSIM_TEXT_CHECK=DIR`（ゲート、7日分は sitecustomize、pytest は `-p text_check`）。`SHELTERSIM_TEXT_CHECK_STRICT=1` で違えばその場で `AssertionError`。`report` は Rust の出口ごとの作った回数と比べた回数、Python の `raise` のか所ごとの到達（比較の中か外か）を出します |
| 変更の後に投げる規則 | 同 `AFTER_CHANGE` | 人口の先送りは世界に再試行を予定してから抑止するので、kernel の前に写しを取り、その上で Python の規則を動かします。それ以外は前後の指紋（`event_queue`、`model_state` の id、`id_counters`）が変わっていれば比べずに数えます（全テスト、ゲート、7日分で0回） |
| テスト | `tests/integration/test_event_texts.py`、`support/world.py` の `kernel_says_python_s_text` | 1か所ずつ状態を作り、kernel 自身が出したこと（印）、世界が変わらないこと、写しの上の Python の規則が同じクラスと文を出すことを確かめます |

**感度：** 移した文を2つ1文字変えた木（`facility outage exceeds currently working capacity.`、`Agents are no longer in a modeled contact zone.`）で、テストは1件失敗、ゲートの比較は 304 件の違いを出しました（ゲートのハッシュも3つの Scenario で違いました）。戻して全部通ります。

## 移したもの

| ハンドラー（Python） | `raise` のか所 | Rust が文を出す | 場所 |
|---|--:|--:|---|
| `FacilityOutageHandler`、`FacilityRecoveryHandler` | 18 | 18 | `handlers.rs` `facility_units` |
| `ProximityContactHandler` | 6 | 6 | `handlers.rs` `proximity_on` |
| `ResourceLedgerHandler` | 4 | 3 | 同。残り1は `ResourceLedgerError` の包み（`resources.rs`） |
| `DeliveryDelayHandler`、`WaterOutageHandler`、`WaterRestorationHandler` | 14 | 14 | 同 |
| `StaffUnavailabilityHandler`、`StaffRestorationHandler`、`_interrupt_support_task`、`_ensure_event_identity_free` | 18 | 18 | 同。すべて写しの上（取引）で、変更の後に投げるものも世界は変わりません |
| `InfrastructureEventHandler` と `_infrastructure_state`、`_outage_for_start`、`_recalculate_infrastructure_capacity` | 20 | 20 | 同。`ValueError` の包みは `combined infrastructure failures exceed installed units` だけ。pydantic の検証の失敗は断りのまま |
| `EffectProgramPopulationHandler` | 4 | 3 | 同。残り1は `TypeError`、`ValueError` の包み（Rust の型変換のエラーの文が混じるため） |
| `ResourceServiceCancellationHandler` | 1 | 1 | 同 |
| `InformationRumorHandler`、`InformationCorrectionHandler`、`InformationContactHandler`、`_information_runtime_state` | 37 | 36 | `information_events.rs`。残り1は `upgrade_information_runtime_state` の包み（古い版の状態、O） |
| `StaffOperationArrivalHandler`、`StaffOperationCompletionHandler` と `arrive_`、`complete_`、`handoff_staff_operation` | 10 | 8 | `staff_operation_events.rs`。残り2は「職員が使えなくなった」抑止（作業を止めてから投げる） |
| `StaffSupportCompletionHandler`（`complete_support_task` の包みを含む） | 4 | 4 | `support.rs` `complete_support_event`。dispatch の直接の呼び出しも記録します |
| `SupportRequestTimeoutHandler` | 3 | 3 | `support.rs` `timeout_event`。同 |
| `ToiletServiceCompletionHandler` | 3 | 2 | `toilet.rs` `complete_event`。残り1は `complete_toilet_service` の包み |
| `AgentTravelCompletionHandler` | 19 | 3 | `travel_completion.rs`。最初の3つ（ID、作業、人）だけ |
| `SocialInteractionHandler` | 38 | 6 | `social_interaction.rs`。型つきの最初の3つ（出典と時刻、理由、人）。共存、家族、必要の3つの抑止は前のレーンから Rust |
| `EffectProgramFacilityHandler` | 10 | 10 | `effects.rs` `facility_rule` |
| 計 | 209 | 155 | うち前のレーンから Rust だった8つ（接触の4、社会の3、期限切れの1）を除いた **147 がこのレーンで移したもの** |

前のレーンから Rust が出していたもの（`population.rs` の再試行の抑止、`population_lifecycle.rs` の4つ、接触の4つ、社会の3つ、期限切れの1つ）も、同じ出口（`errors.rs`）を通るようにして、比較に入れました。

**順序：** Python の規則が `or` で並べる検査は、Rust でも同じ順に評価します（前の検査が通ったときだけ次を読む）。Rust が決められない値（派生型など）は、その検査より前のどこで断っても Python が同じ文を出すので正しいままです。逆に、Python より先に Rust が文を出すことはありません。`text_is`（完全一致の str）で比べていたところは、文を出す検査では `!=`（Python の比較）に替えました。

## 結果

すべてこのレーンの最後のコード（`b2e47a0`）で、比較を入れた状態です。ログは `speed/lane-l8-texts-events/final2/`（前の回は `final/`）。

| 確かめたこと | 結果 |
|---|---|
| 全テスト、比較あり（`-p text_check`、違えば失敗） | 4,681 件成功、1件失敗、8件 skip。失敗は `test_refusal_census.py::test_a_run_of_the_driver_shows_its_fallbacks` だけで、基準の木（#353 の前の版）にある既知の古い期待値です（#353 で直り、`6d5662c` でマージ済み。この枝にはまだ入っていません） |
| 全テスト、`SHELTERSIM_NATIVE=0` | 2,079 件成功、2,611 件 skip |
| 全テスト、比較なし（前の回 `final/`、最後のコードの1つ前） | 4,679 件成功、2件失敗（上の1件と、断りを期待していた `test_a_timeout_of_another_person_s_request_is_python_s_error`。意味を保って直しました） |
| ゲート（`--expect`、比較あり） | 55 Scenario、問題0件 |
| ゲート（比較なし、`--reversed-order`。前の回） | どちらも 55 Scenario、問題0件 |
| 7日分（`--expect`、比較あり） | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。一致 |
| `first_difference.py --both` | interventions 33,930、operational 7,005、population 14,920、arrivals 12,656、conditions 3,782、geometry 16,632、split_queues 4,202 の段階で同じ（前の回。interventions と operational は最後のコードでも同じ） |
| 予算（`tick_budget.py`、`fork-b1` の 9600分から 480 tick、5回） | **1.005**（1.005、1.025、0.978、1.158、0.996）。A/A 1.030。上限 1.10 以内、状態は同じ。load average 3.6 → 35（ほかのレーンと同時） |

**比較の回数（Rust が出した文を Python の規則と比べた回数。違い0）：** テスト 22,206、ゲート 7,013、7日分 3,447。7日分の比較は前のレーンから Rust の3か所（社会の共存と必要、期限切れ）で、このレーンで移した枝には7日分は届きません。

**比べたか所：** Python の `raise` 366 か所のうち、比較の中で Python の規則がその `raise` を通ったものは **112 か所**（テスト、ゲート、7日分のどれか）。移した 155 のうち、どの比較も通らないものは約 40 か所です。そのうち `test_effect_kernels.py` の設備のテストは kernel と `__wrapped__` を直接比べるので、道具の数には入りません（4か所）。残りは上の「有効な入力から届かないと読んだもの」と、状態を作るのに手間のかかる枝（水の停止の対象、職員の重複、経路の途絶の既存の途絶など）です。**coordinator の決定（届かないか所は手順4で1つの出口に）に従い、1つずつのテストは書いていません。**

**Run で Python がまだ文を出すか所（ゲート）：** `ResourceServiceManager.complete`（取消し済み、1回）、`validate_staff_operation_order`（2か所、各1回）、`effect_event_guard_or_suppress`（1回）。どれも下の「残したもの」です。7日分では0です。

**ゲートで届いていた E の断り：** `the units are for the Python rule to judge`（`handlers.rs`）は設備の停止、復旧の文の移動でなくなりました。`a marker's guard the Python rule decides`（`effects.rs`、2回、失敗する tick）は、`phase` が `start` でないときの `ValueError("only a start event may store a guard")` を kernel が出すようにしました（保存されない文ですが、ゲートが届くので。`errors::marked_value_error`）。ガードの形を Rust が読めないときの断りは残します（有効な入力では届きません）。

## 残したもの（手順4で要るもの）

**この表の多くは L8c（`l8-texts-saved.md`）で Rust に移りました。** `raise EventSuppressed/EventApplicationError` は `src` に 366 か所（AST）。Rust が文を出すのは 155、**残りは 211** です。どれも今は kernel が断り、Python の規則が文を出します。手順4ではこれも Rust が要ります。

| 残り | か所 | 理由 |
|---|--:|---|
| `events/effects.py` の環境、床図、構造、一時空間のハンドラー | 54 | 時間が足りず手を付けていません。`m6-effects.md` のとおり、成功以外は断ります |
| `effects.py` のガード（`_evaluate_guard_node` 5、`should_apply_effect_guard` 1、`effect_event_guard_or_suppress` 1） | 7 | **印（`effect_program_v1`）のあるイベントは dispatch が丸ごと Python の dispatcher に渡します**（`dispatch.rs`）。ガードの文はその中の Python です。ゲートで1回届きます。Rust の道を作るのは dispatch の仕事です |
| `SocialInteractionHandler` の残り | 32 | 自律の行動は毎 tick の道で、Rust の検査の順序が Python と大きく違います（人の照合が先）。並べ直すと速さに効くので、比べるテストを先に足してから移すべきです。苦情、協力、情報への反応の枝も同じ |
| `AgentTravelCompletionHandler` の残り | 16 | 列の歩きと到着の後の処理で、多くは書いた後に投げます。L3 の範囲と重なります |
| `domain/population.py`、`population_lifecycle.py` | 58 | 再試行の抑止など一部は前のレーンから Rust（上）。残りはコマンドの解析（pydantic）と入所の検査で、このレーンでは見ていません |
| `domain/resource_service.py`（`service.rs`） | 21 | ゲートで1回（`complete` の取消し済み）。`service.rs` は L4 の範囲です |
| `domain/staff_operations.py` の `order_staff_operation`、`validate_staff_operation_order` | 15 | **Rust の道がありません**（`effects.rs` が名前で呼ぶ Python）。`validate_staff_operation_order` は `validate_effect_program` も使う残るコード（D3）です。ゲートで2回届きます |
| 職員が使えなくなった抑止（到着、完了） | 2 | 作業を止めてから投げます（書いた後）。止める処理ごと Rust に要ります |
| 包み（`ValueError` を `EventApplicationError(str(exc))` にするもの）：`ResourceLedgerError`、人口の primitive、`complete_toilet_service` | 3 | 中の規則の文（`ValueError`）が L8b の範囲です。中の文が Rust に移れば、包みは `errors::made_here` と同じ形で移せます。人口の primitive は Rust の型変換のエラーの文が混じるので、そのままでは移せません |
| `ResourceServiceCompletionHandler`（WorldState v5） | 1 | 古い版（O）。dispatch は v6 以外を Python に渡します |
| `_information_runtime_state` の `upgrade_information_runtime_state` | 1 | 古い版の状態（O） |
| `_apply_operational_transaction` の `transactional follow-up records must be EventLogRecord values` | 1 | **有効な入力から届きません。** Rust のハンドラーは `EventLogRecord` しか作りません |

**移したが、有効な入力からは届かないと読んだもの（テストなし）：** `event sequence changed during staff preflight` と `... support preflight`（直前に読んだ `id_counters["event"]` を `allocate_event_sequence` が返すので、同じ世界では違いません）、`generated event identity ... is not unique`（連番の ID なので、壊れた状態でだけ）。`information-spread message limit was reached`（1024 件の噂が要ります。届きますがテストは書いていません）。

**モデル担当の方へ：** 係数、式は変えていません。新しい Rust だけの条件もありません（`Unsupported` の条件は減っただけです）。

## 他のレーンとの重なり

- **L8b：** `native/src/core/errors.rs`、`core/mod.rs`、`lib.rs` の登録、`src/sheltersim/core/kernels.py`、`benchmarks/rule_shadow/sitecustomize.py` を両方が触ります。L8b は早い版の `errors.rs` と `kernels.py` の写しを持っています。マージでは `_outcome_check` を `before`/`after` の形（このレーン）にし、`errors.rs` は2つの節を並べます（`speed/lane-l8-texts-events/coordination.md`）。
- `rules/population.rs`、`population_lifecycle.rs`、`support.rs`、`toilet.rs` は他のレーンの kernel ですが、触ったのはイベントの文の出し方だけです。
