# M6 レーン：人の出入り、仮設の空間、形状の上書き、`model_configs` の持ち主

**Rust が `population_presence_v1`、`population_lifecycle_v1`、`population_dynamics_v1`、`temporary_spaces_v1`、`geometry_overlay_v1`、`model_configs` を持ちます**（`native/src/state/population.rs`。どれも `V` の行です）。結果は変わりません。**規則は1つも Rust に移していません。** 調査で、毎 tick に動くのは検証の入り口だけだと分かったので、持ち主になって得られる「世代」で、読まずに「変わっていない」と言えるようにしました（`NamespaceMirror.generation_of`、`state_mirror.namespace_stamp`、`field_stamp`）。

| もの | 場所 |
|---|---|
| 行（`ROW`。6つで共用） | `native/src/state/population.rs` |
| 世代 | `native/src/core/world_core.rs` の `NamespaceMirror.generation` |
| 検証の省略 | `core/tick_validation.py`（`begin`）、`domain/simulator_state.py` |
| 分の最初の設定の比較 | `experiments/runner.py`（`__call__`） |
| 履歴の追記をその場で | `domain/population.py`（`_append_presence_history`）、`domain/population_lifecycle.py`（`_set_presence`） |
| 比較 | `benchmarks/rule_shadow/shadow_population.py` |
| テスト | `tests/integration/test_population_row.py` |

## 調査（7日分、`cProfile`、`TickEngine.step` 33.9 秒。ゲートは `sys.monitoring` で数えた回数）

| 規則 | 7日分の回数 | 累計秒 | ゲートの回数 | 扱い |
|---|---:|---:|---:|---|
| `TickValidationMemo.begin` | 10,080 | 0.99 | | **世代で 109 → 13 µs/tick** |
| `reserve_sleep_bed` | 14,585 | 2.07 | 5,845 | **触っていません。** M4 サービスのレーンの `apply_effect` が、部屋がなければ呼びません |
| `release_sleep_bed` | 72,879 | 0.10 | 57,641 | 同上 |
| `_parsed_config` | 396,708 | 1.10 | 584,188 | Python（下） |
| `effective_geometry` | 58,005 | 0.39 | 90,503 | Python（pydantic のモデルを返す） |
| `parsed_facility_geometry` | 21,526 | 0.28 | 80,914 | Python |
| `current_household_id` | 104,110（保存の `world_frame` が 100,800） | 0.11 | 44,270 | Python |
| `capacity_metrics` | 8,088（`world_frame` 5,376、完全検証 2,712） | 0.06 | 126,384 | Python |
| `incoming_travelers` | 8,151 | 0.05 | 97,258 | M4 サービスのレーンの `travel.rs` にあります |
| `validate_temporary_spaces_state` | 10,421 | 0.04 | 35,061 | Python（空なら読むだけ） |
| `admit_pending_returns`、`admit_pending_arrivals` | 各 10,080 | 0.01 未満 | 各 31,533 | Python |
| `validate_geometry_overlay_state`、`validate_presence_state`、`validate_population_lifecycle_state` | 各 約 340（保存だけ） | 0.06〜0.10 | 約 7,800 | Python。tick では省きます |
| `queue_arrivals`、`queue_departures`、`_admit_profile`、`_depart_agent`、`apply_population_presence_command`、`apply_arrival_approach_completion`、`complete_presence_relocation`、`apply_geometry_change`、`register_temporary_space`、`expire_temporary_space` など **書き手のすべて** | **0** | | 1〜161 | Python |

**7日分では書き手が1回も動きません。** 7日分の Run の間、この5つの名前空間に書くものはなく（`model_configs` にも）、kernel にしても 7日分では動きません。ゲートでも1回 µs の規則が数百回です。書き手を移さない判断の理由はこれで、下の「Python に残した規則」に全部を載せました。

**`begin` の中身は、読むことの費用でした：** `model_configs ==`（133 KB の JSON）と、`saved_json` 3回（`population_presence_v1` が 23 KB）です。どちらも C の中なので kernel にしても速くなりません。**同じ比較がもう1つありました：** `experiments/runner.py` の分の最初の `world.model_configs != self._config_scopes_configs` で、これも約 50 µs/tick です。M5 の表には出ていません。

## 世代

`NamespaceMirror.generation` は、木に届くすべての書き込みで増えます（転送の `apply`、`replace_root`、taint、Rust の `*_through`、`store`、`refill_record_map`、`with_row_tree`）。`generation_of(tree)` は、渡した値がその木の根のときだけ数を返します。**世代と根が同じなら、書き込みは届いていません。**

- `model_configs`：**手段が見つかりました。** 設計の表のとおり `OWNED_FIELDS` に入れて `V` の木にしました。世代が同じなら読みません。書かれたあと、または丸ごと代入されたあと（根が変わる）だけ、今までの `==` に戻ります。値の比較を残すので、`deepcopy` の写しも残ります。
- 名前空間：`same(key)` が世代を比べます。ミラーがない世界（追跡の session、`dict.__setitem__(model_state, ...)`）は今までの `saved_json` です。
- **増やしたもの：** `population_dynamics_v1` の検証も、自分の状態、人、区域が変わらず、待っている人がいなければ省きます（`dynamics_unchanged`）。待っている人の検証は、イベント、時刻、`population_presence_v1` を読むので省きません。動的な文脈（`profile_context`）は自分の状態しか読みません。

**JSON の比較より弱くなるところ：** 木の節点への `dict.__setitem__` は世代に届きません。今までは `saved_json` がこれを見ていました。`src/` にはなく（`test_tracked_write_lint.py`）、次の完全検証が読むのは同じです（`test_a_namespace_written_past_the_tree_is_left_to_the_full_validation`）。行を読む検証と同じ性質です（[t-tick-validation.md](t-tick-validation.md)）。

**同じ値の書き込み：** 同じオブジェクトの再代入は何も起こしません。等しい別のオブジェクトの書き込みは世代を動かすので、その tick は読み直します（遅くなるだけです）。

## 書き込みの形を変えたところ（結果は同じ）

`V` の木は、新しい list を入れると記録を1件ずつ変換し直します。

- `raw["history"] = [*raw["history"], x][-20_000:]`（4か所）をその場の追記にしました。別名を持ち続けるコードはありません。
- `admit_pending_arrivals` は、待っている人の並びが変わらなければ `pending_arrivals` を書きません。待っている人がいる間、毎 tick に全員を写すことになるからです。

ほかの list の丸ごとの書き込み（`pending_returns`、`relationship_archive`、`resource_service` の履歴）は残しました。人の出入りのイベントのときだけです。

## 試して、やめたもの

**`Geo::of` の世代のキャッシュ：** `rules/movement_geometry.rs` は呼ぶたびに Python の `_parsed_config` を2回呼びます（7日分で 396,708回）。世界の core に計画を持ち、世代が同じなら読まない形を試しました。測定ループで 1 回 990 → 320 ns、1 tick に約 20 回なので **約 14 µs/tick（0.5%）** です。ただし scope の中で設定をその場で編集すると、古い解析から作った計画が新しい世代で残ります（Python は次の分に読み直します）。`src/` にその編集はありませんが、消す口を2か所足すほどの得ではないのでやめました。

## Python に残した規則（手順4で Rust が要るもの）

- **`domain/population.py`：** すべて。`queue_arrivals`、`apply_arrival_approach_completion`、`admit_pending_arrivals`、`queue_departures`、`_depart_agent`、`_admit_profile`、`_record_*`、`_generated_arrival_context`、`_schedule_arrival_approach_completion`、`schedule_population_retry`、`_cancel_agent_events`、`_choose_admission_target`、`_zone_has_room`、`validate_population_dynamics_state`（M5 の kernel は、待っている人も文脈もない状態だけです）。
- **`domain/population_lifecycle.py`：** すべて。`apply_population_presence_command`（世界を写して差し替えます。設計の「写して差し替える」の難所）、`admit_pending_returns`、`complete_presence_relocation`、`current_household_id`、`_archive_person`、`_restore_person`、`_schedule_relocation`、`_set_presence`、`validate_population_lifecycle_state`。
- **`domain/temporary_spaces.py`：** すべて。`capacity_metrics`、`effective_zone_capacity`、`effective_total_zone_capacity`、`incoming_travelers`（M4 サービスの `travel.rs` に Rust の版があります）、`register_`、`expire_temporary_space`、`validate_temporary_spaces_state`。
- **`domain/geometry_overlay.py`：** すべて。`reserve_sleep_bed`（部屋がなければ M4 サービスの kernel が呼びません）、`release_sleep_bed`、`available_temporary_room`、`admit_to_temporary_room`、`room_admission_point`、`effective_geometry`、`effective_spatial_constraints`、`installed_facility_point`、`place_installed_facility`、`validate_geometry_change`、`apply_geometry_change`、`validate_geometry_overlay_state`。
- **`contracts/presence.py`：** `validate_presence_state`。pydantic です。
- **`core/tick_validation.py`：** `TickValidationMemo` のすべて（M5 の項のとおり）。`begin` は残り約 13 µs で、人と区域の ID の比較、`facility_zones` の dict、世代の読みです。
- **7日分の外で重い道：** 待っている人がいる Run は、1 tick に待っている人ごとに `_zone_has_room`（`capacity_metrics` と人の全員の走査）を呼びます（ゲートで 84,949回）。7日分にはありません。到着があふれる Run を速くしたいなら、次に見るところです。

kernel が断るものはありません（kernel を足していないので）。`Unsupported` は増えませんでした（下）。

## モデルの担当の方に見ていただくコード

**Rust に移した式、係数、Rust だけの条件はありません。** Python の規則で変えたのは、上の「書き込みの形」の2つと、検証を省く条件だけです。値も式も変えていません。

## 確認

`594993c`、2026-11-01、WSL2、16コア、ほかのレーンと同時です。基準は `e2621ad`（`rust/m5-metrics` の先頭）。その後の変更はこの文書だけです。

| `tick_budget.py`（9600分から480 tick） | 基準との比 | A/A | load average |
|---|---:|---:|---|
| `594993c`（5回） | **0.945**（0.952、0.945、0.924、0.941、0.964） | 1.025（1.05、0.985、0.969、1.043、1.025） | 25〜40 |
| 最初の commit の前（3回） | 0.905（0.889、0.972、0.905） | 0.956（0.951、0.956、1.04） | 約 8 |

状態はどの回も3つとも一致しました。A/A の揺れの中に近いので、この表だけでは強く言えません。下の部品のほうがはっきりしています。

部品（同じ分岐から480 tick、基準2本とレーンを同時に1組、スレッドの CPU 時間、µs/tick、計測の枠を含む）：

| | 基準 | A/A | レーン | load average |
|---|---:|---:|---:|---|
| `TickEngine.step` | 2,672 | 2,664 | 2,428 | 約 6 |
| `validate_after_tick` | 445 | 441 | 335 | |
| `TickValidationMemo.begin` | 109 | 107 | 13 | |
| 分の最初の `__call__`（`_run_phase` の外） | 96 | 96 | 48 | |
| `TickEngine.step`（高負荷の1組） | 3,681 | 3,715 | 3,401 | 約 25 |
| `begin`（同） | 154 | 153 | 16 | |

`begin` の 96 µs と分の最初の 48 µs で、tick の約 5〜9% です。保存は `Checkpoint.capture` 3,626 / 3,582 → 3,457、`WorldState.from_dict` 1,663 / 1,969 → 1,560（高負荷の組は 5,071 / 5,089 → 5,143、2,240 / 2,151 → 2,207）で、揺れの中です。**保存は遅くなっていません。** 復元の確認の世界は行を持たないからです（`_bare_world`）。

7日分の全体（基準2本とレーンを同時に1組、load average 30〜40、スレッドの CPU 時間、秒）：

| | 基準 | A/A | レーン |
|---|---:|---:|---:|
| 全体 | 128.6 | 129.6 | **126.0** |
| `TickEngine.step` | 46.6 | 46.7 | **44.3** |
| `validate_after_tick` | 7.44 | 7.48 | 5.69 |
| `TimelineStore.record` | 79.5 | 80.3 | 79.0 |

| 確認 | 結果 |
|---|---|
| 両モードの全テスト | Rust あり 3,782件成功、6件 skip。`SHELTERSIM_NATIVE=0` 2,073件成功、1,715件 skip |
| `gate_all_paths.py --expect` | 55シナリオ、問題0件。全部の影の回も同じ |
| `--reversed-order` | 55シナリオ、問題0件。このレーン、M5、T の影の回も同じ、違い0件 |
| 7日分 | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。`--expect` と一致（素の回、影の回） |
| `ruff check`、`ruff format --check`、`cargo clippy`（`disallowed_methods`） | 問題なし |
| `tracked_mirror_gate.py` | **動かしていません**（指示どおり） |

| `rule_shadow` | ゲート（全部の影） | 7日分（このレーン、M5 の `shadow_metrics`、T の `shadow_tick_validation`） |
|---|---:|---:|
| `begin` の回数 | 31,530 | 10,080 |
| 比べた bytes | 2,511,687,599 | 2,372,702,516 |
| `model_configs` 省いた / 読んだ | 31,083 / 10 | 10,079 / 0 |
| `presence` 省いた / 読んだ（うち本当に書かれた） | 27,316 / 3,777（30） | 10,079 / 0 |
| `lifecycle` 同 | 27,313 / 3,780（1） | 10,079 / 0 |
| `dynamics` 同 | 28,053 / 3,040（29） | 10,079 / 0 |
| `overlay` 同 | 27,262 / 3,831（23） | 10,079 / 0 |
| 行と木の違い、省いたのに bytes が違った回、省いた `dynamics` の規則が断った回 | 0 | 0 |
| taint、印のない状態 | 0 | 0 |

7日分は `generated` の `project` の経路で、全部の影の7日分は動かしていません（約 2.5 時間かかるため）。読んだ回の多くは、部屋、保留、`archive_path` があるからで、書かれたからではありません。7日分は **1回も書かれません**（どの比較も「省いた」だけ）。

**kernel の断りが増えていないこと：** `model_configs` の型が変わるので、`Unsupported` を `sys.monitoring` で数えて、基準とレーンで比べました。7日分は 3,459回で理由ごとに同じ。ゲートは 21,874 → 20,418 回で、違いは「the population state is the Python rule's」の 1,929 → 473 だけです（待っている人のいない動的な文脈を、kernel を呼ぶ前に省くようになったため）。

**ゲートの行の網羅（kernel ありとなしの合計 / kernel ありだけ）：** `domain/population.py` 491行のうち 417行（85%）/ 416行（85%）、`domain/population_lifecycle.py` 557行のうち 407行（73%）/ 同じ、`domain/temporary_spaces.py` 92行のうち 81行（88%）/ 同じ、`domain/geometry_overlay.py` 795行のうち 560行（70%）/ 同じ、`core/tick_validation.py` 171行のうち 153行（89%）/ 同じ、`experiments/runner.py` 170行のうち 157行（92%）/ 同じ。

### 感度（コピーで変更。`--only population --only geometry --only arrivals`、影はこのレーンだけ）

| 変更 | 影 | ゲートのハッシュ | テスト（`test_population_row.py`、`test_tick_validation.py`、`test_tick_views.py`） |
|---|---:|---|---:|
| 行の小数を 1.01 倍で書き出す | 60件以上 | **不一致**（保存の確認で止まる） | 2件失敗 |
| 転送で世代を増やさない（1% ではない） | 2件（`presence`、`lifecycle` が 35分目） | 一致 | 26件失敗 |
| 待っている人がいても `dynamics` を省く（1% ではない） | 0件 | 一致 | 最初は0件。`test_a_pending_arrival_is_read_at_every_check` を直して1件失敗 |
| 履歴の上限 20,000 → 20,200（1%） | 0件 | 一致 | 1件失敗 |
| 分の最初に設定の書き込みを見ない（1% ではない） | 0件 | 一致 | 1件失敗（`test_a_config_edited_between_ticks_is_seen_by_the_next_phases`） |

**検証を省くことの間違いは、ハッシュには見えません。** ゲートには良い状態しか来ないからです。世代の見落としは影とテストが、省く条件の間違いはテストが見ます。履歴の上限は、ゲートも7日分も 20,000件に届かないので、テストだけが見ます。

## 確かめていないこと、危ないところ

- **Windows と macOS では動かしていません。** 足した C API はありません。
- `tracked_mirror_gate.py` は**動かしていません**（指示どおり）。`core/state_mirror.py` の変更は `OWNED_NAMESPACES`、`OWNED_FIELDS` の行と、読むだけの `namespace_stamp`、`field_stamp`、`same_stamp` です。
- **`model_configs` の節点の型が `TrackedDict` になりました。** `src/` の `isinstance` と kernel の `exact_dict` は通ります（全テスト、断りの数の比較）。pydantic の `Any` の項目が節点をそのまま持つと、後の書き込みが転送されますが、`src/` に設定をその場で書くのは `experiments/branch.py:493` の `setdefault` だけです。
- `dict.__setitem__` で節点に書くと、tick の検証は次の完全検証まで気づきません（上）。
- **`tick_budget.py` の比は A/A の揺れに近いです。** 低負荷（load average 約 6）で測ったのは部品の組だけです。
- **このレーンのあとに来る kernel が `model_configs` や overlay を `downcast_exact` で読むと、静かに `Unsupported` になります。** `exact_dict` を使ってください。M3 と M4 サービスのブランチの `native/src` を見た範囲では、該当するのは今もある `queues.rs:138`（`selected_models` の結果で、設定の木ではありません）だけです。

## レーンとの重なり

- `core/tick_validation.py`、`experiments/runner.py`、`native/src/core/world_core.rs`：M4 サービスと M3 dispatch のブランチは、この3つを変えていません（`git diff` で確認。`core/state_mirror.py` は `OWNED_NAMESPACES` の行だけが重なります）。
- `docs/architecture.md`、`native/README.md`：同じ段落に追記があるので、統合で衝突するかもしれません。
- **M4 サービスのレーンへ：** `proposals.rs` の `temporary_rooms` の読みは、overlay が追跡の木になっても `exact_dict` で通ります。このレーンの `reserve_sleep_bed` には触っていません。
