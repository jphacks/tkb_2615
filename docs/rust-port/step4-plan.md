# 手順4の計画：Python のモデルを消す

手順3の後に Python のモデルのコードを消すための計画です。レーン L0 が作りました。元の棚卸しは文を読んで分類したもので、L0 がそれを実測に置き換えました。実測は [2節](#2-断りの実測l0) です。

## 基準と読み方

**コード：** 数えた木は2つです。

| 名前 | 中身 | 使ったところ |
|---|---|---|
| **基準** | `feature/rust-port` の `ac39c84` + 計測（`rust/l0-refusals`） | ハッシュの証明。2節の「基準」の数 |
| **込み** | 基準 + `rust/m6-handlers-rest`（`df84748`）を試しにマージした木。公開していません | 2節の表と L1〜L8。L1 はこのレーンのマージの後に始まるからです |

**棚卸しの静的な数（3節以降の行数など）** は、その前の木（`6a7ad7a` + overlay + handlers-rest）のままです。数え直していません。ここで「2レーン込み」と書くのはその木のことで、`m6-validate`（#346）は入っていません。

**確かさの書き分け：**
- **読んで確かめた：** その行のコードを読みました。
- **AST・grep で数えた：** スクリプトで機械的に数えました。
- **実測：** L0 で動かして数えました。
- **推測：** 読んでいません。そう書きます。

### 棚卸しの後に入ったもの

| 変更 | 状態 | この計画への影響 |
|---|---|---|
| driver（#344、`rules/tick.rs`） | マージ済み | tick の段階の順序は Rust です。**`rule_shadow` と `--reversed-order` は driver を通りません**（`m6-driver.md`）。Provider を呼べる router と trace は tick ごと Python の経路です。L0 は driver の `or_rule!` も数えます |
| overlay（#345、`rules/overlay.rs` ほか） | マージ済み | `an independent FIFO*`、`a temporary room may have a bed`、`a surveyed service point` の断りはなくなりました。`rules/population.rs` の読みを `overlay.rs` に寄せる宿題が残ります（L1） |
| validate（#346、`rules/after_tick.rs`、`rules/metrics.rs` の `capture`） | マージ済み | **L7 が小さくなりました。** 7日分では Plan のない最初の1 tick を除く全 tick を kernel が受けます。実測の断りは下の L7 |
| `rust/m6-handlers-rest`（`wt-m6-handlers2`、`df84748`） | **未マージ。** 込みの木で計測済み | 7日分で Python に渡るイベントが 10,495 件から 8 件に。文書 `m6-handlers.md` は同じブランチにあります。マージのときの衝突は `sitecustomize.py` と `docs/architecture.md` だけでした（試しマージ） |

## 結論

1. **まだ消せません。** 残りは大きく5つです：依頼の割り当てと職員の作業の本体、trace つきの Run、tick の検証の断る分岐、区域の環境の式と人の文脈、エラーの文。
2. **消す順序：Python を消すのは、libm と RNG を変える前です。** 消す段階では結果が変わらないので、`fixtures/gate-all-paths.json` と7日分のハッシュが最後の安全網として効きます。先に結果を変えると、消した影響と区別できません。
3. **旧モデルの Run の閲覧は、今の作りでは動きません（読んで確かめた）。** 版が1つでも違うと `Checkpoint._validate` が `CheckpointError` を出します。さらに `TimelineStore.iter_worlds` は保存の間の分を**今のモデルで tick を回して**作ります。単に閲覧を許すと、まさに禁じられている「黙った読み替え」になります。一番危ないところです（L9）。
4. **実測で分かった一番大きいこと：`Unsupported` の 1,854 か所のうち 1,360 か所は、全テスト、ゲート、7日分のどこでも一度も作られませんでした（実測、込みの木）。** 手順4の「Python の規則と比べて文を移す」は、届く場所でしか効きません。L8 は移すより先にテストを書く仕事が中心になります（2節、6節）。

## 1. 決定

**決まったものです。** 変えられる人を書きます。D6、D11、D13 と下の既知の不具合は、**`main` に戻す前に担当の方に確かめていただきます。**

| # | 決定 | 変えられる人 |
|---|---|---|
| **D1** | **trace の文書は形を変えずに Rust に移します。** `AgentPsychologyTrace.to_dict` 187行、`explain_update` 計 約 500 行、接触の trace。provenance に効くので減らしません。L6 の最初に見積もります | 利用者、担当の方 |
| **D2** | **旧 Run は保存の時点だけを見せます。** 保存の間の分は保存の時点に丸め、丸めたことを画面に出します | 利用者 |
| **D3** | **tick の外の `validate_*`（約 4,400 行）は Python に残します。** tick の中の分だけ L7 で移します | 利用者 |
| **D4** | **デモの replay（`fixtures/demo_replay/`、48 MB）は作り直します**（`scripts/build_demo_replay.py`）。旧版は `submission` タグに残ります。Git の増え方は確かめます | 利用者 |
| **D5** | **RNG はキーの整理だけです。** SHA-256 は残します。速さが要ると測ってから考えます | 利用者、担当の方 |
| **D7** | **libm と RNG は分けて確かめ、マージは同じ日にします** | 利用者 |
| **D8** | **`sheltersim-native` は OS ごとの wheel を作って配ります。** README と `CONTRIBUTING.md` を直します | 利用者 |
| **D9** | **CI は、今のジョブの中にゲートの手順を足します。トリガーは変えません** | 利用者 |
| **D10** | **`known_stops` の2つの不具合は別の変更で直します。** ハッシュが変わる変更に混ぜると原因が分からなくなるからです | 利用者、担当の方 |
| **D12** | **手順4の後も CPython 3.12 に固定します。** Python に残る `sum()` が版で変わり得るからです | 利用者 |

### 担当の方に確かめていただくもの（`main` に戻す前）

| # | 確かめること | 今の案 |
|---|---|---|
| **D6** | **Scenario の int の係数（`0`、`1`）。** 有効な入力で、今は kernel が断り Python が動きます。**どの Run でもテストでも届いていません（実測：`a policy value is not an int`、`a flag is not a bool`）。** Python を消すと動かなくなります | Scenario を読むときに float にそろえます。保存の JSON が `0` から `0.0` に変わるので、版を上げる L11 に合わせます。**係数の値は変えません** |
| **D11** | **非 ASCII の設備の種類。** `casefold` が変えるかもしれないので断っています | 契約で ASCII に制限するか確認します。`to_lowercase` は `casefold` と違います |
| **D13** | **見ていただくコードの一覧。** L5 の式、L2 と L3 で移す枝、libm の前後の KPI の差 | **新しい係数がないことをレーンごとに書きます** |
| **既知の不具合** | **`validate_geometry_change` の `transfer_equipment`：** 足跡のある設備で障害物が1つでもあると、`item.facility_id` が `SpatialObstacle` にないので `AttributeError` になります（`m6-overlay.md`）。kernel は断るので Python と同じ例外です。変えていません | Python を消す前に、直すか、この例外の文のまま移すかを決めていただきます |

**係数と式の意味は移植で変わりません。** 結果が変わるのは libm、RNG のキー、順序だけで、L11 と、順序を変えるなら L12 に閉じます。

## 2. 断りの実測（L0）

### 測り方

`benchmarks/rule_shadow/refusal_census.py`。`SHELTERSIM_REFUSAL_CENSUS=DIR` で動きます。使い方はファイルの先頭にあります。

- **作られた場所：** `Unsupported` を作る口を1つにし（`native/src/lib.rs`、`core/census.rs`）、`#[track_caller]` で呼び手の行を数えます。**か所を1つずつ直していないので、これから足す断りも自動で数えます。** 込みの木も同じです。
- **受けたところ：** driver の `or_rule!`、`__wrapped__` の呼び戻し（`rule_of` と、`population.rs`、`population_lifecycle.rs` の書いた後の2か所）、`rules`、`state` のすべての関数の Python の入口（`kernel_first`、`segments.py`、`_kernel_first`、`native.py`、`state_mirror.py` はここを通ります）。
- **切っているときの費用：** Python の側は何も入りません。Rust は断りを作る途中でフラグを1回読むだけです（下の予算）。

**到達の分け方：** 「有効な状態か」は1行ずつ機械で決めました。読んで決めたものではありません。

| 到達 | 意味 | 確かさ |
|---|---|---|
| **Run** | ゲートか7日分で届きました。`--expect` に一致する有効な Run です | 実測 |
| **Run（判定のみ）** | Run で作られましたが、どの入口にも届きません。Rust の中で捨てる判定か（`exact_str(x).is_ok()`）、作り直される途中です（`after_tick` の `check` の断りは `after_tick` が作り直します。**同じ断りが2行に出ます**） | 実測 |
| **テスト・tick** | テストだけ。終わった tick の中（`validate_after_tick` を通った）で、そのテスト自身のソースは patch も壊す書き込みもしません | 上限 |
| **テスト・直接** | テストだけ。tick の外で kernel を直接呼び、`world.validate()` は通りました。**引数はテストが作ったものなので、世界は有効でも引数が壊れている場合を含みます** | 上限 |
| **示せず** | テストだけ。世界が `validate` を通らない、世界がない、tick が失敗、テストのソースが壊すか差し替える（`dict.__setitem__`、`taint`、`patch`、`SimpleNamespace` など）、`test_tracked_mirror` | 有効な状態から届かないとは言えません |
| **印** | patch、shadow、テストの偽物、`validate_state=True` への断り。理由の文で分けました。手順4で印ごと消えます | 文から |

**校正：** ゲートに同じ `validate` の確認を入れると、入口で断られた 59 件のうち有効 57、世界なし 1、不合格 1 でした（基準の木）。tick の途中の状態でも、この確認はほとんど誤りません。

**届いたことと届き得ることは別です。一度も届かないか所は「有効な状態から届かない」という意味ではありません。** たとえば `a policy value is not an int` は有効な Scenario で届きますが、どの Scenario もそう書いていないので0回です。

### 確かめ方

| | 基準 | 込み |
|---|---:|---:|
| 計測ありのゲート（`--expect`） | 55シナリオ、問題0件 | 55シナリオ、問題0件 |
| 計測あり + `validate` の確認のゲート | 55シナリオ、問題0件 | 55シナリオ、問題0件 |
| 計測ありの7日分（`--expect`） | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。一致 | 同じ。一致 |
| tick（`TickEngine.step`）テスト / ゲート / 7日分 | 138,363 / 31,530 / 10,080 | 138,247 / 31,530 / 10,080 |
| **driver が動いた tick（`tick.stats()`）** | テスト 88,928、ゲート 28,892、**7日分 10,080** | 込みの木の計測の時は未実装でした。`or_rule` の行が0でないことで見ています |
| **`or_rule!` の合計と driver 自身の数（`kernels_refused`）** | テスト 55,924 = 55,924、ゲート 6,280 = 6,280、7日分 720 = 720 | |
| 全テスト | 計測なし 4,284件成功、6件 skip。`SHELTERSIM_NATIVE=0` 2,079件成功、2,211件 skip。計測あり 4,277件成功、1件失敗（下）、12件 skip | 計測あり 4,323件成功、1件失敗（下）、12件 skip |

**全テストの1件の失敗は計測の確認の副作用です：** `test_a_registered_kernel_is_used_and_can_turn_the_state_down` は登録した検証が呼ばれた回数を数え、`validate` の確認がもう1回呼びます。確認なしの計測では成功します（確かめました）。

### 結果の大きさ（込み）

- 静的なか所 1,854。届いたか所 495。**一度も届かないか所 1,360**（基準は 1,663、480、1,184）。
- 届いたか所の内訳（一番強い到達で1回ずつ）：**Run** 36 / Run（判定のみ） 8 / テスト・tick 8 / テスト・直接 153 / 示せず 265 / 印 25。
- 表の行（場所と文の組）577 行の内訳：**Run** 43 / Run（判定のみ） 9 / テスト・tick 10 / テスト・直接 158 / 示せず 330 / 印 27。
- 数えた範囲は `Unsupported` だけです。**kernel のない規則（3節の c）はここに出ません。**

### 棚卸しの分類と実測（行の数。場所の file と文で突き合わせ）

| 棚卸しの記号 | か所（文から） | Run | Run（判定のみ） | テスト・tick | テスト・直接 | 示せず | 印 | 一度も届かないか所 |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| C | 1305 | 6 | 2 | 0 | 81 | 203 | 0 | 1011 |
| E | 267 | 6 | 0 | 5 | 51 | 24 | 0 | 181 |
| V | 53 | 13 | 0 | 1 | 5 | 4 | 0 | 30 |
| T | 26 | 1 | 0 | 0 | 3 | 3 | 0 | 19 |
| M | 27 | 1 | 0 | 0 | 6 | 7 | 0 | 14 |
| HT | 6 | 6 | 0 | 0 | 0 | 0 | 0 | 0 |
| H | 32 | 0 | 0 | 0 | 0 | 0 | 23 | 9 |
| O | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 4 |
| — | — | 10 | 7 | 4 | 12 | 89 | 4 | 92 |

— は棚卸しの後に入った断り（`after_tick.rs` など）か、文のない断りです。**読み方：** C は「壊れた状態だけ」の推測でしたが、Run に 8 行あります（うち2行は判定のみ）。E は「有効な状態で届く」の推測でしたが、Run で届いたのは 6 行で、か所の過半は一度も届きません。

### 表：Run で届いた断り（すべて）

kernel は `Unsupported` を作った行を含む Rust の関数です。受けた入口は多い順に2つです。回数は作られた回数です。

| 理由 | kernel（場所） | 受けた入口 | テスト | ゲート | 7日分 | 到達 | 棚卸し |
|---|---|---|--:|--:|--:|---|---|
| a free worker can take a request | `kind_exposure` `rules/staff.rs:243` | `rules.staff.assign_none` | 24,369 | 3,139 | 712 | **Run** | V |
| a qualified worker is free, so routes are read | `kind_wait` `rules/staff.rs:811` | `rules.staff.wait_snapshot`, `rules.metrics.snapshot_fields` | 38,258 | 2,313 | 4 | **Run** | V |
| someone gives up on the queue | `prepare_facility_queue` `rules/queues.rs:402` | `or_rule queues::prepare_facility_queue`, `rules.queues.prepare_facility_queue` | 192 | 167 | 4 | **Run** | V |
| a free worker can patrol | `assign_operations` `rules/staff.rs:1236` | `or_rule staff::assign_operations`, `rules.staff.assign_operations` | 27 | 0 | 2 | **Run** | V |
| the namespace has no row | `with_row` `core/world_core.rs:932` | `state.psychological_distress.validate`, `state.body_state.rows_valid` | 2,231 | 65 | 1 | **Run** | T |
| a request closed at once | `plan_request` `rules/support.rs:944` | `rules.proposals.apply_effect`, `or_rule proposals::apply_effect` | 135 | 2 | 1 | **Run** | V |
| the policy has no long-term state this kernel knows | `update_long_term` `rules/psychology.rs:362` | `or_rule psychology::update_long_term`, `rules.psychology.update_long_term` | 48,240 | 2,680 | 0 | **Run** | M |
| a Provider can be called | `accept` `rules/tick.rs:444` | `rules.tick.run_tick` | 2,442 | 1,800 | 0 | **Run** | HT |
| a value is not a str | `exact_str` `core/row.rs:501` | `rules.tick_validation.entity_rules`, `rules.staff_dynamics.canonical_config` | 2,081 | 1,781 | 0 | Run（判定のみ） | C |
| a trace is recorded | `update_psychology` `rules/psychology.rs:1095` | `rules.psychology.update_psychology` | 31,828 | 840 | 0 | **Run** | HT |
| a trace is recorded | `accept` `rules/tick.rs:440` | `rules.tick.run_tick` | 31,596 | 840 | 0 | **Run** | HT |
| the population state is the Python rule's | `population_dynamics` `rules/tick_validation.rs:1100` | `rules.tick_validation.population_dynamics` | 1,057 | 821 | 0 | **Run** | C |
| the sink is Python's, or the run keeps a trace | `record_outcome` `rules/decisions.rs:552` | `rules.decisions.record_outcome`, `or_rule decisions::record_outcome` | 47,627 | 696 | 0 | **Run** | HT |
| a recent receipt was not checked here | `plan_record` `rules/common_operations.rs:337` | `rules.common_operations.record_shared_operation`, `rules.care_receipts.append_transition` | 1,055 | 689 | 0 | **Run** | V |
| a declared namespace has no kernel that takes it | `check` `rules/after_tick.rs:523` | — | 1,651 | 646 | 0 | Run（判定のみ） | — |
| a declared namespace has no kernel that takes it | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 1,651 | 646 | 0 | **Run** | — |
| a trace records the contacts | `inputs` `rules/information.rs:190` | `rules.information.sample_contacts` | 17,024 | 480 | 0 | **Run** | HT |
| a trace needs the Python contact records | `update_social_propagation` `rules/social_propagation.rs:449` | `rules.social_propagation.update_social_propagation` | 16,560 | 480 | 0 | **Run** | HT |
| population_dynamics_v1: its kernel did not take it | `must_validate` `rules/after_tick.rs:207` | — | 390 | 348 | 0 | Run（判定のみ） | — |
| population_dynamics_v1: its kernel did not take it | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 390 | 348 | 0 | **Run** | — |
| no longer in a contact zone | `contact` `rules/information_events.rs:554` | `rules.information_events.contact` | 355 | 304 | 0 | **Run** | E |
| a staff member has a task | `relief` `rules/staff.rs:1263` | `or_rule staff::relief`, `rules.staff.relief` | 396 | 265 | 0 | **Run** | V |
| staff_operations_v1: its kernel did not take it | `must_validate` `rules/after_tick.rs:207` | — | 348 | 230 | 0 | Run（判定のみ） | — |
| staff_operations_v1: its kernel did not take it | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 348 | 230 | 0 | **Run** | — |
| the residents are busy: the Python rule defers | `queue_departures` `rules/population.rs:2462` | `rules.population.queue_departures` | 192 | 108 | 0 | **Run** | V |
| the world holds another movement state | `mirror_held` `rules/movement.rs:395` | `rules.movement.facility_approach_route`, `rules.movement.advance_movement` | 1,237 | 92 | 0 | **Run** | C |
| no floor plan: a request at once | `approach_route` `rules/travel.rs:834` | `rules.proposals.apply_effect`, `or_rule proposals::apply_effect` | 1,236 | 92 | 0 | **Run** | V |
| another kind of receipt | `record_shared_operation` `rules/common_operations.rs:176` | `rules.common_operations.record_shared_operation` | 134 | 69 | 0 | **Run** | C |
| a call raised EventSuppressed | `refused_by_python` `rules/effects.rs:75` | `rules.handlers.population` | 96 | 54 | 0 | **Run** | — |
| the shared operations are the Python rule's | `check` `rules/after_tick.rs:582` | — | 67 | 42 | 0 | Run（判定のみ） | — |
| the shared operations are the Python rule's | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 67 | 42 | 0 | **Run** | — |
| the people changed | `check` `rules/after_tick.rs:453` | — | 38 | 28 | 0 | Run（判定のみ） | — |
| the people changed | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 38 | 28 | 0 | **Run** | — |
| the person is busy: the Python rule defers | `plan_action` `rules/population_lifecycle.rs:797` | `rules.population_lifecycle.apply_presence_action` | 39 | 20 | 0 | **Run** | V |
| no active task | `complete_support_event` `rules/support.rs:331` | `rules.support.complete_support` | 37 | 19 | 0 | **Run** | E |
| the service's independent queue at once | `plan_resource` `rules/proposals.rs:203` | `or_rule proposals::apply_effect` | 12 | 12 | 0 | **Run** | V |
| a free worker may repair | `assign_operations` `rules/staff.rs:1172` | `or_rule staff::assign_operations`, `rules.staff.assign_operations` | 14 | 7 | 0 | **Run** | V |
| the Python rule refuses the change | `refuse` `rules/overlay.rs:1467` | `rules.overlay.validate_geometry_change`, `rules.overlay.apply_geometry_change` | 169 | 5 | 0 | **Run** | — |
| edge capacities look at every trip under way | `rows_of` `rules/travel.rs:97` | `rules.travel.route`, `or_rule proposals::apply_effect` | 99 | 3 | 0 | **Run** | V |
| the sender's belief changed | `contact` `rules/information_events.rs:551` | `rules.information_events.contact` | 74 | 2 | 0 | **Run** | C |
| the sample is stale | `proximity_on` `rules/handlers.rs:393` | `rules.handlers.proximity` | 6 | 2 | 0 | **Run** | E |
| no active task | `complete_event` `rules/service.rs:930` | `rules.service.complete` | 4 | 2 | 0 | **Run** | E |
| a marker's guard the Python rule decides | `check_state` `rules/effects.rs:1999` | `rules.effects.validate_state` | 3 | 2 | 0 | **Run** | C |
| a call raised StateInvariantError | `refused_by_python` `rules/effects.rs:75` | `rules.effects.geometry`, `rules.handlers.staff_unavailable` | 2 | 2 | 0 | **Run** | — |
| the provenance does not match the sender | `contact` `rules/information_events.rs:568` | `rules.information_events.contact` | 43 | 1 | 0 | **Run** | E |
| the presence state changed | `check` `rules/after_tick.rs:463` | — | 35 | 1 | 0 | Run（判定のみ） | — |
| the presence state changed | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 35 | 1 | 0 | **Run** | — |
| value is not a plain JSON value | `frozen` `records.rs:86` | — | 24 | 1 | 0 | Run（判定のみ） | C |
| the units are for the Python rule to judge | `facility_units` `rules/handlers.rs:510` | `rules.handlers.facility_recovery`, `rules.handlers.facility_outage` | 6 | 1 | 0 | **Run** | C |
| the modifier is not active | `facility_rule` `rules/effects.rs:731` | `rules.effects.facility` | 4 | 1 | 0 | **Run** | E |
| a body history actor is no longer here | `body_history` `rules/after_tick.rs:300` | — | 2 | 1 | 0 | Run（判定のみ） | — |
| a body history actor is no longer here | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 2 | 1 | 0 | **Run** | — |

**`a value is not a str` は断りではありません。** 込みの木のゲートの 1,781 回はすべて、Rust が値の型を確かめて捨てる判定です。どの入口にも届いていません（実測）。テストの 127 回だけが入口に届きます。

<details><summary>表：テストだけで届いた断り（525 行）</summary>

| 理由 | kernel（場所） | 受けた入口 | テスト | ゲート | 7日分 | 到達 | 棚卸し |
|---|---|---|--:|--:|--:|---|---|
| an urgent request may preempt a task | `assign_none` `rules/staff.rs:725` | `rules.staff.assign_none` | 203 | 0 | 0 | テスト・tick | V |
| the configs were written | `check` `rules/after_tick.rs:444` | — | 15 | 0 | 0 | テスト・tick | — |
| the configs were written | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 15 | 0 | 0 | テスト・tick | — |
| the modifier is not active | `environment_rule` `rules/effects.rs:847` | `rules.effects.environment` | 7 | 0 | 0 | テスト・tick | E |
| the configs had no stamp | `check` `rules/after_tick.rs:438` | — | 3 | 0 | 0 | テスト・tick | — |
| the configs had no stamp | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 3 | 0 | 0 | テスト・tick | — |
| the modifier was not applied or has expired | `geometry_rule` `rules/effects.rs:1165` | `rules.effects.geometry` | 2 | 0 | 0 | テスト・tick | E |
| the space did not start | `temporary_space_rule` `rules/effects.rs:1017` | `rules.effects.temporary_space` | 2 | 0 | 0 | テスト・tick | E |
| the exit of the same program was dropped | `plan_action` `rules/population_lifecycle.rs:821` | `rules.population_lifecycle.apply_presence_action` | 1 | 0 | 0 | テスト・tick | E |
| the return of the same program has passed | `plan_action` `rules/population_lifecycle.rs:794` | `rules.population_lifecycle.apply_presence_action` | 1 | 0 | 0 | テスト・tick | E |
| the guard isn't one the contract surely takes | `guard_of_row` `rules/effects.rs:546` | `rules.effects.should_apply_guard` | 3,449 | 0 | 0 | テスト・直接 | E |
| id_counters is not a plain dict | `of` `state/events.rs:1161` | `rules.effects.validate_state`, `rules.dispatch.run_phase` | 1,526 | 0 | 0 | テスト・直接 | C |
| a guard has no facility target | `guard_field` `rules/effects.rs:465` | `rules.effects.should_apply_guard` | 1,116 | 0 | 0 | テスト・直接 | E |
| an unknown reservation | `apply_planned` `rules/resources.rs:231` | `rules.resources.apply_operation`, `rules.handlers.resource_ledger` | 523 | 0 | 0 | テスト・直接 | E |
| a guard has no known staff target | `guard_field` `rules/effects.rs:526` | `rules.effects.should_apply_guard` | 372 | 0 | 0 | テスト・直接 | E |
| a guard has no ledger target | `guard_field` `rules/effects.rs:502` | `rules.effects.should_apply_guard` | 372 | 0 | 0 | テスト・直接 | E |
| the event counter is not an int a rule can add to | `of` `state/events.rs:1165` | `rules.effects.validate_state`, `state.shared_operations.validate_tick` | 323 | 0 | 0 | テスト・直接 | C |
| eligible contacts have no weight | `channel_stress` `rules/social_propagation.rs:274` | `rules.social_propagation.plan_of_values`, `rules.social_propagation.update_social_propagation` | 268 | 0 | 0 | テスト・直接 | C |
| a guard has no known zone target | `guard_field` `rules/effects.rs:496` | `rules.effects.should_apply_guard` | 248 | 0 | 0 | テスト・直接 | E |
| the world has no core | `core_of` `core/world_core.rs:1347` | `rules.facility_queues.queue_head_agent_ids`, `state.shared_operations.recent_passed` | 161 | 0 | 0 | テスト・直接 | C |
| the reservation ID was used | `apply_planned` `rules/resources.rs:216` | `rules.resources.apply_operation` | 87 | 0 | 0 | テスト・直接 | E |
| a value is not an int | `exact_int` `core/row.rs:485` | `rules.tick_validation.entity_rules`, `rules.staff_dynamics.canonical_config` | 86 | 0 | 0 | テスト・直接 | C |
| a program record the Python rule decides | `check_state` `rules/effects.rs:1915` | `rules.effects.validate_state` | 77 | 0 | 0 | テスト・直接 | C |
| a guard has no ledger target | `guard_field` `rules/effects.rs:504` | `rules.effects.should_apply_guard` | 62 | 0 | 0 | テスト・直接 | E |
| a guard has no staff target | `guard_field` `rules/effects.rs:520` | `rules.effects.should_apply_guard` | 62 | 0 | 0 | テスト・直接 | E |
| a guard has no zone target | `guard_field` `rules/effects.rs:488` | `rules.effects.should_apply_guard` | 62 | 0 | 0 | テスト・直接 | E |
| the loss exceeds the inventory | `apply_planned` `rules/resources.rs:208` | `rules.resources.apply_operation` | 55 | 0 | 0 | テスト・直接 | E |
| the entity store is tainted | `with_rows` `state/entities.rs:1484` | `rules.facility_queues.queue_head_agent_ids`, `state.entities.hunger_of_agents` | 54 | 0 | 0 | テスト・直接 | C |
| the reservation exceeds the inventory | `apply_planned` `rules/resources.rs:219` | `rules.resources.apply_operation`, `rules.handlers.resource_ledger` | 51 | 0 | 0 | テスト・直接 | E |
| the reservation is someone else's | `apply_planned` `rules/resources.rs:234` | `rules.resources.apply_operation`, `rules.handlers.resource_ledger` | 51 | 0 | 0 | テスト・直接 | E |
| movement_v1: its kernel did not take it | `must_validate` `rules/after_tick.rs:207` | — | 18 | 0 | 0 | テスト・直接 | — |
| movement_v1: its kernel did not take it | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 18 | 0 | 0 | テスト・直接 | — |
| value is not a plain JSON value | `read` `core/value.rs:81` | `rules.overlay.validate_spaces`, `rules.overlay.validate_overlay` | 18 | 0 | 0 | テスト・直接 | C |
| the model state is not the table, or a tree is detached | `check` `rules/after_tick.rs:425` | — | 17 | 0 | 0 | テスト・直接 | — |
| the model state is not the table, or a tree is detached | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 17 | 0 | 0 | テスト・直接 | — |
| the state or the warehouse is for the Python rule to judge | `apply_planned` `rules/resources.rs:189` | `rules.resources.apply_operation`, `rules.handlers.resource_ledger` | 17 | 0 | 0 | テスト・直接 | C |
| a stack's entry the Python rule decides | `check_state` `rules/effects.rs:2036` | `rules.effects.validate_state` | 10 | 0 | 0 | テスト・直接 | C |
| the maps of the world are not the ones the rows follow | `check` `rules/after_tick.rs:419` | — | 10 | 0 | 0 | テスト・直接 | — |
| the maps of the world are not the ones the rows follow | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 10 | 0 | 0 | テスト・直接 | — |
| a value is not a float | `exact_float` `core/row.rs:494` | `rules.staff_dynamics.canonical_config`, `rules.movement.route_with_movement_time` | 9 | 0 | 0 | テスト・直接 | T |
| a free worker can clean | `assign_operations` `rules/staff.rs:1163` | `rules.staff.assign_operations` | 8 | 0 | 0 | テスト・直接 | V |
| a receipt the model has to decide | `receipt` `rules/action_lifecycle.rs:357` | `rules.action_lifecycle.record_action_proposal` | 8 | 0 | 0 | テスト・直接 | M |
| not a stable ID | `valid_id` `rules/resources.rs:64` | `rules.resources.apply_operation` | 8 | 0 | 0 | テスト・直接 | C |
| the consume exceeds the reservation | `apply_planned` `rules/resources.rs:242` | `rules.resources.apply_operation` | 8 | 0 | 0 | テスト・直接 | E |
| the action does not match its type | `apply_event` `rules/social_interaction.rs:183` | `rules.social_interaction.apply` | 7 | 0 | 0 | テスト・直接 | E |
| a value is outside 0..1 | `unit_float` `rules/psychology.rs:52` | `rules.psychology.update_long_term`, `rules.psychology.apply_deltas` | 6 | 0 | 0 | テスト・直接 | C |
| the modifier is already active | `environment_rule` `rules/effects.rs:886` | `rules.effects.environment` | 6 | 0 | 0 | テスト・直接 | E |
| the units are for the Python rule to judge | `facility_units` `rules/handlers.rs:497` | `rules.handlers.facility_outage` | 6 | 0 | 0 | テスト・直接 | C |
| a stack's baseline the Python rule decides | `check_state` `rules/effects.rs:2023` | `rules.effects.validate_state` | 5 | 0 | 0 | テスト・直接 | C |
| a value is not a float | `unit_float` `rules/psychology.rs:48` | `rules.psychology.update_long_term`, `rules.psychology.apply_deltas` | 5 | 0 | 0 | テスト・直接 | T |
| a quantity that is not positive | `apply_operation` `rules/resources.rs:114` | `rules.resources.apply_operation`, `rules.handlers.resource_ledger` | 4 | 0 | 0 | テスト・直接 | E |
| a rate is not a float | `exact_rate` `rules/staff.rs:1013` | `rules.staff.update_operations` | 4 | 0 | 0 | テスト・直接 | C |
| a value is not a plain list | `exact_list` `core/row.rs:513` | `rules.staff_dynamics.validate_override`, `rules.agent_dynamics.create_state` | 4 | 0 | 0 | テスト・直接 | C |
| an unknown resource | `apply_operation` `rules/resources.rs:109` | `rules.resources.apply_operation`, `rules.handlers.resource_ledger` | 4 | 0 | 0 | テスト・直接 | E |
| not one or two participants | `capture_context` `rules/social_dialogue.rs:240` | `rules.social_dialogue.capture_context` | 4 | 0 | 0 | テスト・直接 | E |
| the person is not admitted | `plan_action` `rules/population_lifecycle.rs:791` | `rules.population_lifecycle.apply_presence_action` | 4 | 0 | 0 | テスト・直接 | E |
| the receipt's minute is not the world's | `record_shared_operation` `rules/common_operations.rs:189` | `rules.common_operations.record_shared_operation` | 4 | 0 | 0 | テスト・直接 | C |
| a queue trip must replan, in the serial order of the walk | `plan_journey` `rules/movement.rs:959` | `rules.movement.advance_movement` | 3 | 0 | 0 | テスト・直接 | V |
| a value is not a float | `exact_float` `rules/psychology.rs:435` | `rules.psychology.update_psychology` | 3 | 0 | 0 | テスト・直接 | T |
| the context is for the Python rule to judge | `apply_event` `rules/social_interaction.rs:191` | `rules.social_interaction.apply` | 3 | 0 | 0 | テスト・直接 | C |
| the facility is for the Python rule to judge | `facility_units` `rules/handlers.rs:501` | `rules.handlers.facility_outage` | 3 | 0 | 0 | テスト・直接 | C |
| the payload has other fields | `facility_units` `rules/handlers.rs:491` | `rules.handlers.facility_outage` | 3 | 0 | 0 | テスト・直接 | C |
| the request ID was used before | `plan_record` `rules/common_operations.rs:355` | `rules.care_receipts.append_transition`, `rules.common_operations.record_shared_operation` | 3 | 0 | 0 | テスト・直接 | E |
| the task's ID is a reservation's | `serve_one` `rules/service.rs:653` | `rules.service.serve_head` | 3 | 0 | 0 | テスト・直接 | C |
| the transition is out of order | `plan_record` `rules/common_operations.rs:344` | `rules.common_operations.record_shared_operation`, `rules.care_receipts.append_transition` | 3 | 0 | 0 | テスト・直接 | E |
| working_units is not an int | `facility_rule` `rules/effects.rs:715` | `rules.effects.facility` | 3 | 0 | 0 | テスト・直接 | C |
| a call raised ValidationError | `refused_by_python` `rules/effects.rs:75` | `rules.effects.environment`, `rules.effects.structure` | 2 | 0 | 0 | テスト・直接 | — |
| a distance is not a float | `action_of` `rules/body_state.rs:835` | `rules.body_state.record_social_event` | 2 | 0 | 0 | テスト・直接 | C |
| a free worker can reach out | `assign_operations` `rules/staff.rs:1218` | `rules.staff.assign_operations` | 2 | 0 | 0 | テスト・直接 | V |
| a marker of another program | `check_state` `rules/effects.rs:1981` | `rules.effects.validate_state` | 2 | 0 | 0 | テスト・直接 | C |
| a marker's values the Python rule decides | `check_state` `rules/effects.rs:1994` | `rules.effects.validate_state` | 2 | 0 | 0 | テスト・直接 | C |
| a participant is not in the world | `read_capture` `rules/social_dialogue.rs:205` | `rules.social_dialogue.capture_context` | 2 | 0 | 0 | テスト・直接 | E |
| a program's assumptions the Python rule decides | `check_state` `rules/effects.rs:1932` | `rules.effects.validate_state` | 2 | 0 | 0 | テスト・直接 | C |
| a relationship has no trust | `record_relationship` `rules/body_state.rs:425` | `rules.body_state.record_social_event` | 2 | 0 | 0 | テスト・直接 | C |
| a value is not a str | `required_str` `rules/effects.rs:99` | `rules.effects.facility` | 2 | 0 | 0 | テスト・直接 | C |
| a value is not an Identifier | `identifier` `rules/population.rs:175` | `rules.population.queue_arrivals` | 2 | 0 | 0 | テスト・直接 | C |
| a value the receipt's contract is for the Python rule to judge | `plan_transition` `rules/care_receipts.rs:121` | `rules.care_receipts.append_transition` | 2 | 0 | 0 | テスト・直接 | C |
| an ID is not a str | `id` `rules/body_state.rs:196` | `rules.body_state.record_social_event` | 2 | 0 | 0 | テスト・直接 | C |
| an unknown operation | `apply_operation` `rules/resources.rs:95` | `rules.resources.apply_operation` | 2 | 0 | 0 | テスト・直接 | C |
| an unknown person | `apply_planned` `rules/resources.rs:175` | `rules.resources.apply_operation` | 2 | 0 | 0 | テスト・直接 | E |
| an unknown zone | `queue_departures` `rules/population.rs:2420` | `rules.population.queue_departures` | 2 | 0 | 0 | テスト・直接 | E |
| an unknown zone | `plan_arrivals` `rules/population.rs:1473` | `rules.population.queue_arrivals` | 2 | 0 | 0 | テスト・直接 | E |
| no proximity model | `proximity_on` `rules/handlers.rs:315` | `rules.handlers.proximity` | 2 | 0 | 0 | テスト・直接 | V |
| no retained absent person | `plan_action` `rules/population_lifecycle.rs:824` | `rules.population_lifecycle.apply_presence_action` | 2 | 0 | 0 | テスト・直接 | E |
| someone is not admitted | `plan_action` `rules/population_lifecycle.rs:886` | `rules.population_lifecycle.apply_presence_action` | 2 | 0 | 0 | テスト・直接 | E |
| the 100-staff bound | `add_structure` `rules/effects.rs:1644` | `rules.effects.structure` | 2 | 0 | 0 | テスト・直接 | E |
| the alternate zones are invalid | `plan_arrivals` `rules/population.rs:1483` | `rules.population.queue_arrivals` | 2 | 0 | 0 | テスト・直接 | C |
| the config lacks a table | `zone_override` `rules/staff.rs:998` | `rules.staff.update_operations` | 2 | 0 | 0 | テスト・直接 | C |
| the cooperation is for the Python rule to judge | `other_typed_event` `rules/social_interaction.rs:468` | `rules.social_interaction.apply` | 2 | 0 | 0 | テスト・直接 | C |
| the count is out of range | `plan_arrivals` `rules/population.rs:1515` | `rules.population.queue_arrivals` | 2 | 0 | 0 | テスト・直接 | E |
| the delta has body values and the person no body record | `apply_deltas` `rules/psychology.rs:1369` | `rules.psychology.apply_deltas` | 2 | 0 | 0 | テスト・直接 | C |
| the duration is out of bounds | `apply_event` `rules/social_interaction.rs:187` | `rules.social_interaction.apply` | 2 | 0 | 0 | テスト・直接 | E |
| the event's effect is not scheduled | `facility_rule` `rules/effects.rs:709` | `rules.effects.facility` | 2 | 0 | 0 | テスト・直接 | C |
| the household size is out of range | `plan_arrivals` `rules/population.rs:1519` | `rules.population.queue_arrivals` | 2 | 0 | 0 | テスト・直接 | E |
| the mobility is not a member, or is the Python rule's to check | `plan_arrivals` `rules/population.rs:1523` | `rules.population.queue_arrivals` | 2 | 0 | 0 | テスト・直接 | C |
| the modifier violates capacity or occupancy | `facility_rule` `rules/effects.rs:718` | `rules.effects.facility` | 2 | 0 | 0 | テスト・直接 | E |
| the observed state is long | `decision_bytes` `rules/decisions.rs:481` | `rules.decisions.record_outcome` | 2 | 0 | 0 | テスト・直接 | C |
| the payload has other fields | `contact` `rules/information_events.rs:492` | `rules.information_events.contact` | 2 | 0 | 0 | テスト・直接 | C |
| the payload has other fields | `apply_operation` `rules/resources.rs:102` | `rules.resources.apply_operation` | 2 | 0 | 0 | テスト・直接 | C |
| the payload has other fields | `apply_event` `rules/social_interaction.rs:172` | `rules.social_interaction.apply` | 2 | 0 | 0 | テスト・直接 | C |
| the person is not admitted | `plan_action` `rules/population_lifecycle.rs:854` | `rules.population_lifecycle.apply_presence_action` | 2 | 0 | 0 | テスト・直接 | E |
| the plan read: ValidationError: 1 validation error for DynamicArrivalPersonContext
  Value error, dynamic arrival context override requires a care support kind [type=value_error, input_value={'member_index': 9}, input_type=dict]
    For further information visit https://errors.pydantic.dev/2.11/v/value_error | `planning` `rules/population.rs:66` | `rules.population.queue_arrivals` | 2 | 0 | 0 | テスト・直接 | — |
| the policy is not one this kernel knows | `model_of` `rules/psychology.rs:428` | `rules.psychology.update_psychology` | 2 | 0 | 0 | テスト・直接 | M |
| the record is of another row of the event | `is_applied` `rules/action_lifecycle.rs:605` | `rules.action_lifecycle.record_applied_action_completion` | 2 | 0 | 0 | テスト・直接 | C |
| the reservation is the service's | `ledger_event_of` `rules/handlers.rs:455` | `rules.handlers.resource_ledger` | 2 | 0 | 0 | テスト・直接 | C |
| the retry limit is reached | `plan_retry` `rules/population.rs:2040` | `rules.population.schedule_population_retry` | 2 | 0 | 0 | テスト・直接 | E |
| the route is unknown | `plan_arrivals` `rules/population.rs:1505` | `rules.population.queue_arrivals` | 2 | 0 | 0 | テスト・直接 | E |
| working units that aren't the stack's | `check_state` `rules/effects.rs:2041` | `rules.effects.validate_state` | 2 | 0 | 0 | テスト・直接 | C |
| a delta is at another minute | `update_social_propagation` `rules/social_propagation.rs:487` | `rules.social_propagation.update_social_propagation` | 1 | 0 | 0 | テスト・直接 | C |
| a delta is not of this minute | `apply_deltas` `rules/psychology.rs:1316` | `rules.psychology.apply_deltas` | 1 | 0 | 0 | テスト・直接 | C |
| a derived event lacks a known parent | `check_state` `rules/effects.rs:2008` | `rules.effects.validate_state` | 1 | 0 | 0 | テスト・直接 | C |
| a journey has no plain duration | `start_movement` `rules/movement.rs:1289` | `rules.movement.start_movement` | 1 | 0 | 0 | テスト・直接 | C |
| a marker's fields the Python rule decides | `check_state` `rules/effects.rs:1969` | `rules.effects.validate_state` | 1 | 0 | 0 | テスト・直接 | C |
| a marker's phase the Python rule decides | `check_state` `rules/effects.rs:1985` | `rules.effects.validate_state` | 1 | 0 | 0 | テスト・直接 | C |
| a model this kernel does not know | `inputs` `rules/information.rs:183` | `rules.information.sample_contacts` | 1 | 0 | 0 | テスト・直接 | M |
| a neighbor is not a str | `neighbors_of` `rules/social_propagation.rs:400` | `rules.social_propagation.update_social_propagation` | 1 | 0 | 0 | テスト・直接 | C |
| a policy this kernel does not know | `settings_of` `rules/social_propagation.rs:356` | `rules.social_propagation.update_social_propagation` | 1 | 0 | 0 | テスト・直接 | M |
| a slowdown is not a plain value | `movement_model` `rules/movement.rs:1470` | `rules.movement.route_with_movement_time` | 1 | 0 | 0 | テスト・直接 | C |
| a support trip without its IDs | `plan_travel` `rules/travel.rs:418` | `rules.travel.schedule_travel` | 1 | 0 | 0 | テスト・直接 | C |
| a zone is not a str | `correction_actors` `rules/body_state.rs:740` | `rules.body_state.record_social_event` | 1 | 0 | 0 | テスト・直接 | C |
| an unknown care status | `status_of` `rules/care_receipts.rs:82` | `rules.care_receipts.append_transition` | 1 | 0 | 0 | テスト・直接 | C |
| an unknown facility | `facility_rule` `rules/effects.rs:693` | `rules.effects.facility` | 1 | 0 | 0 | テスト・直接 | E |
| another movement schema | `movement_model` `rules/movement.rs:1431` | `rules.movement.route_with_movement_time` | 1 | 0 | 0 | テスト・直接 | C |
| another route policy | `rows_of` `rules/travel.rs:102` | `rules.travel.route` | 1 | 0 | 0 | テスト・直接 | M |
| committed is not a bool | `record_action_proposal` `rules/action_lifecycle.rs:539` | `rules.action_lifecycle.record_action_proposal` | 1 | 0 | 0 | テスト・直接 | C |
| committed is not a bool | `record_outcome` `rules/decisions.rs:533` | `rules.decisions.record_outcome` | 1 | 0 | 0 | テスト・直接 | C |
| metric snapshots must have strictly increasing minutes | `capture` `rules/metrics.rs:360` | `rules.metrics.capture` | 1 | 0 | 0 | テスト・直接 | — |
| no free floor-plan point | `place_facility` `rules/overlay.rs:1287` | `rules.overlay.place_installed_facility` | 1 | 0 | 0 | テスト・直接 | E |
| no such service point | `enqueue` `rules/facility_queues.rs:523` | `rules.facility_queues.enqueue` | 1 | 0 | 0 | テスト・直接 | C |
| not a float | `exact_float` `contacts.rs:26` | `rules.social_propagation.update_social_propagation` | 1 | 0 | 0 | テスト・直接 | C |
| not a tuple | `str_tuple` `rules/movement.rs:1233` | `rules.movement.start_movement` | 1 | 0 | 0 | テスト・直接 | C |
| someone gives up on the queue | `prepare_independent_facility_queue` `rules/queues.rs:495` | `rules.queues.prepare_independent_facility_queue` | 1 | 0 | 0 | テスト・直接 | V |
| the Python rule raises | `call_failed` `rules/world_view.rs:425` | `rules.world_view.view_after_movement` | 1 | 0 | 0 | テスト・直接 | C |
| the admission is not available | `admit_to_temporary_room` `rules/overlay.rs:923` | `rules.overlay.admit_to_temporary_room` | 1 | 0 | 0 | テスト・直接 | E |
| the count is out of range | `queue_departures` `rules/population.rs:2424` | `rules.population.queue_departures` | 1 | 0 | 0 | テスト・直接 | E |
| the counter after this sequence does not fit an i64 | `schedule` `state/events.rs:1203` | `state.events.schedule_row` | 1 | 0 | 0 | テスト・直接 | — |
| the deltas are not a tuple | `update_social_propagation` `rules/social_propagation.rs:461` | `rules.social_propagation.update_social_propagation` | 1 | 0 | 0 | テスト・直接 | C |
| the deltas are not in id order | `update_social_propagation` `rules/social_propagation.rs:512` | `rules.social_propagation.update_social_propagation` | 1 | 0 | 0 | テスト・直接 | C |
| the effect does not reference its approved program | `approved_marked_effect` `rules/effects.rs:1217` | `rules.effects.structure` | 1 | 0 | 0 | テスト・直接 | E |
| the event has no row | `apply` `rules/travel_completion.rs:650` | `rules.travel_completion.apply` | 1 | 0 | 0 | テスト・直接 | C |
| the event's identity is taken | `serve_one` `rules/service.rs:738` | `rules.service.serve_head` | 1 | 0 | 0 | テスト・直接 | E |
| the ledger came late | `capture` `rules/metrics.rs:368` | `rules.metrics.capture` | 1 | 0 | 0 | テスト・直接 | — |
| the metric minute is not the world's | `capture` `rules/metrics.rs:345` | `rules.metrics.capture` | 1 | 0 | 0 | テスト・直接 | — |
| the minute is not the view's | `update_psychology` `rules/psychology.rs:1101` | `rules.psychology.update_psychology` | 1 | 0 | 0 | テスト・直接 | C |
| the model is not enabled | `read` `rules/information.rs:116` | `rules.information.sample_contacts` | 1 | 0 | 0 | テスト・直接 | M |
| the modifier is already active | `facility_rule` `rules/effects.rs:722` | `rules.effects.facility` | 1 | 0 | 0 | テスト・直接 | E |
| the movement config has other fields | `movement_model` `rules/movement.rs:1473` | `rules.movement.route_with_movement_time` | 1 | 0 | 0 | テスト・直接 | C |
| the neighbors of a zone are not a tuple or a list | `neighbors_of` `rules/social_propagation.rs:410` | `rules.social_propagation.update_social_propagation` | 1 | 0 | 0 | テスト・直接 | C |
| the order is not for this minute | `staff_task_order` `rules/effects.rs:1227` | `rules.effects.structure` | 1 | 0 | 0 | テスト・直接 | E |
| the outcome's timing is for Python to refuse | `record_outcome` `rules/decisions.rs:539` | `rules.decisions.record_outcome` | 1 | 0 | 0 | テスト・直接 | E |
| the payload has other fields | `facility_rule` `rules/effects.rs:685` | `rules.effects.facility` | 1 | 0 | 0 | テスト・直接 | C |
| the payload has other fields | `add_structure` `rules/effects.rs:1624` | `rules.effects.structure` | 1 | 0 | 0 | テスト・直接 | C |
| the queue is not installed or the request's ID is in use | `enqueue` `rules/facility_queues.rs:527` | `rules.facility_queues.enqueue` | 1 | 0 | 0 | テスト・直接 | C |
| the repair's preconditions no longer match | `facility_repair` `rules/effects.rs:1499` | `rules.effects.structure` | 1 | 0 | 0 | テスト・直接 | E |
| the reservation ID is a queued task's | `ledger_event_of` `rules/handlers.rs:463` | `rules.handlers.resource_ledger` | 1 | 0 | 0 | テスト・直接 | C |
| the restoration does not match an active outage | `water_restoration` `rules/handlers.rs:702` | `rules.handlers.water_restoration` | 1 | 0 | 0 | テスト・直接 | E |
| the retry is past the scenario | `plan_retry` `rules/population.rs:2029` | `rules.population.schedule_population_retry` | 1 | 0 | 0 | テスト・直接 | E |
| the route starts elsewhere | `plan_travel` `rules/travel.rs:337` | `rules.travel.schedule_travel` | 1 | 0 | 0 | テスト・直接 | E |
| the space limit is reached | `register_space` `rules/overlay.rs:271` | `rules.overlay.register_temporary_space` | 1 | 0 | 0 | テスト・直接 | E |
| the space's ID already exists | `register_space` `rules/overlay.rs:268` | `rules.overlay.register_temporary_space` | 1 | 0 | 0 | テスト・直接 | E |
| the speed table has other fields | `movement_model` `rules/movement.rs:1447` | `rules.movement.route_with_movement_time` | 1 | 0 | 0 | テスト・直接 | C |
| the tree does not have the row's record | `remember_decision` `rules/agent_dynamics.rs:55` | `rules.agent_dynamics.record_proposal` | 1 | 0 | 0 | テスト・直接 | C |
| the tree has no such path | `write_through` `core/world_core.rs:755` | `rules.agent_dynamics.learn_facility` | 1 | 0 | 0 | テスト・直接 | C |
| the view and the deltas have different people | `update_social_propagation` `rules/social_propagation.rs:468` | `rules.social_propagation.update_social_propagation` | 1 | 0 | 0 | テスト・直接 | C |
| the view is at another minute | `update_social_propagation` `rules/social_propagation.rs:458` | `rules.social_propagation.update_social_propagation` | 1 | 0 | 0 | テスト・直接 | C |
| the world is at another minute | `inputs` `rules/information.rs:203` | `rules.information.sample_contacts` | 1 | 0 | 0 | テスト・直接 | C |
| two deltas for one person | `apply_deltas` `rules/psychology.rs:1353` | `rules.psychology.apply_deltas` | 1 | 0 | 0 | テスト・直接 | C |
| two deltas for one person | `update_social_propagation` `rules/social_propagation.rs:516` | `rules.social_propagation.update_social_propagation` | 1 | 0 | 0 | テスト・直接 | C |
| a value is not a plain dict | `exact_dict` `core/row.rs:508` | `rules.overlay.effective_total_zone_capacity`, `rules.overlay.capacity_metrics` | 5,485 | 0 | 0 | 示せず | C |
| the namespace is not owned | `mirror_of` `core/world_core.rs:1357` | `rules.staff_dynamics.on_shift`, `rules.movement.queue_slot_position` | 4,389 | 0 | 0 | 示せず | C |
| the namespace has another row | `with_row` `core/world_core.rs:936` | `state.body_state.rows_valid`, `state.agent_dynamics.validate` | 1,711 | 0 | 0 | 示せず | C |
| the population state's header is other | `validate_population_dynamics_state` `rules/population.rs:2628` | `rules.population.validate_population_dynamics_state` | 798 | 0 | 0 | 示せず | C |
| value is not a plain JSON value | `value` `lib.rs:134` | — | 780 | 0 | 0 | 示せず | C |
| model_state is another kind of mapping | `held_mirror` `state/facility_queues.rs:69` | `rules.facility_queues.facility_has_independent_queues`, `rules.toilet.facilities_to_serve` | 379 | 0 | 0 | 示せず | C |
| a pending arrival lacks a field | `check_pending_items` `rules/population.rs:2697` | `rules.population.validate_population_dynamics_state` | 319 | 0 | 0 | 示せず | C |
| the namespace has another row | `with_row_tree` `core/world_core.rs:957` | `rules.movement.start_movement`, `rules.movement.finish_movement` | 164 | 0 | 0 | 示せず | C |
| a context disagrees with its profile | `validate_population_dynamics_state` `rules/population.rs:2665` | `rules.population.validate_population_dynamics_state` | 161 | 0 | 0 | 示せず | C |
| a pending arrival has another shape | `check_pending_items` `rules/population.rs:2711` | `rules.population.validate_population_dynamics_state` | 161 | 0 | 0 | 示せず | C |
| a pending arrival is invalid | `check_pending_items` `rules/population.rs:2717` | `rules.population.validate_population_dynamics_state` | 161 | 0 | 0 | 示せず | C |
| an age is out of range | `traits_are_plain` `rules/population.rs:2538` | `rules.population.validate_population_dynamics_state` | 161 | 0 | 0 | 示せず | C |
| the event queue is not in Rust | `of` `state/events.rs:1154` | `rules.effects.validate_state`, `state.shared_operations.validate_tick` | 161 | 0 | 0 | 示せず | C |
| an active agent has no profile | `validate_population_dynamics_state` `rules/population.rs:2643` | `rules.population.validate_population_dynamics_state` | 160 | 0 | 0 | 示せず | C |
| an initial ID repeats | `validate_population_dynamics_state` `rules/population.rs:2636` | `rules.population.validate_population_dynamics_state` | 160 | 0 | 0 | 示せず | C |
| the population state has other fields | `validate_population_dynamics_state` `rules/population.rs:2617` | `rules.population.validate_population_dynamics_state` | 160 | 0 | 0 | 示せず | C |
| the population state's parts are something else | `validate_population_dynamics_state` `rules/population.rs:2625` | `rules.population.validate_population_dynamics_state` | 160 | 0 | 0 | 示せず | C |
| the contexts are something else | `validate_population_dynamics_state` `rules/population.rs:2650` | `rules.population.validate_population_dynamics_state` | 158 | 0 | 0 | 示せず | C |
| the open actions are not the store | `record_outcome` `rules/decisions.rs:545` | `rules.decisions.record_outcome` | 148 | 0 | 0 | 示せず | C |
| an agent's value is the Python rule's | `entity_rules_watching` `rules/tick_validation.rs:359` | `rules.tick_validation.entity_rules` | 102 | 0 | 0 | 示せず | C |
| a need is the Python rule's | `entity_rules_watching` `rules/tick_validation.rs:351` | `rules.tick_validation.entity_rules` | 95 | 0 | 0 | 示せず | C |
| active_actions is not the store | `complete_for_event` `rules/action_lifecycle.rs:651` | — | 95 | 0 | 0 | 示せず | C |
| an agent's value is the Python rule's | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 56 | 0 | 0 | 示せず | — |
| an ID list is another kind of list | `ids` `rules/tick_validation.rs:66` | `rules.tick_validation.entity_rules` | 43 | 0 | 0 | 示せず | C |
| a tree of the rule is not a plain dict | `tree` `rules/travel_completion.rs:107` | `rules.travel_completion.apply` | 41 | 0 | 0 | 示せず | C |
| agent_dynamics_v1: its kernel did not take it | `must_validate` `rules/after_tick.rs:207` | — | 41 | 0 | 0 | 示せず | — |
| agent_dynamics_v1: its kernel did not take it | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 41 | 0 | 0 | 示せず | — |
| a state is another kind of dict | `dict_or_nothing` `rules/world_view.rs:306` | `rules.information.sample_contacts` | 40 | 0 | 0 | 示せず | C |
| model_state is not readable | `body_mirror` `rules/psychology.rs:1252` | `rules.psychology.apply_deltas` | 40 | 0 | 0 | 示せず | C |
| the configs are not a plain dict | `configs_of` `rules/world_view.rs:1029` | `rules.world_view.view_after_movement` | 40 | 0 | 0 | 示せず | C |
| the expressiveness is not a dict | `update_social_propagation` `rules/social_propagation.rs:472` | `rules.social_propagation.update_social_propagation` | 40 | 0 | 0 | 示せず | C |
| a value is not a str | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 39 | 0 | 0 | 示せず | — |
| the context is for the Python rule to judge | `validate_context` `rules/social_dialogue.rs:160` | `rules.social_dialogue.validate_context` | 39 | 0 | 0 | 示せず | C |
| an agent's needs are the Python rule's | `entity_rules_watching` `rules/tick_validation.rs:342` | `rules.tick_validation.entity_rules` | 37 | 0 | 0 | 示せず | C |
| an input has no such attribute | `attribute` `core/row.rs:472` | `rules.tick_validation.entity_rules` | 35 | 0 | 0 | 示せず | C |
| the rows of the body state are the Python rule's | `check` `rules/after_tick.rs:543` | — | 32 | 0 | 0 | 示せず | — |
| the rows of the body state are the Python rule's | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 32 | 0 | 0 | 示せず | — |
| model_state is not readable | `body_state_of` `rules/social_network.rs:92` | `rules.social_dialogue.capture_context`, `rules.social_network.observe_event` | 28 | 0 | 0 | 示せず | C |
| an ID list is another kind of list | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 27 | 0 | 0 | 示せず | — |
| the namespace has no row | `with_row_tree` `core/world_core.rs:953` | `state.agent_dynamics.validate`, `rules.movement.validate` | 26 | 0 | 0 | 示せず | T |
| an agent's needs are the Python rule's | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 25 | 0 | 0 | 示せず | — |
| an activity is the Python rule's | `entity_rules_watching` `rules/tick_validation.rs:378` | `rules.tick_validation.entity_rules` | 23 | 0 | 0 | 示せず | C |
| the shifts of a person are not valid | `shifts_of` `rules/staff_dynamics.rs:424` | `rules.staff_dynamics.canonical_config`, `rules.staff_dynamics.validate_override` | 22 | 0 | 0 | 示せず | C |
| a request's agent or timing is the Python rule's | `entity_rules_watching` `rules/tick_validation.rs:835` | `rules.tick_validation.entity_rules` | 18 | 0 | 0 | 示せず | C |
| a value is not an int | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 16 | 0 | 0 | 示せず | — |
| an input has no such attribute | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 16 | 0 | 0 | 示せず | — |
| an unknown staff ID | `validate_override` `rules/staff_dynamics.rs:634` | `rules.staff_dynamics.validate_override` | 15 | 0 | 0 | 示せず | E |
| psychological_distress_v1: its kernel did not take it | `must_validate` `rules/after_tick.rs:207` | — | 15 | 0 | 0 | 示せず | — |
| psychological_distress_v1: its kernel did not take it | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 15 | 0 | 0 | 示せず | — |
| an ID is not stable | `stable_id` `records.rs:38` | — | 14 | 0 | 0 | 示せず | C |
| model_state is not readable | `apply_event` `rules/social_interaction.rs:198` | `rules.social_interaction.apply` | 14 | 0 | 0 | 示せず | C |
| information_spread_v1: its kernel did not take it | `must_validate` `rules/after_tick.rs:207` | — | 13 | 0 | 0 | 示せず | — |
| information_spread_v1: its kernel did not take it | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 13 | 0 | 0 | 示せず | — |
| the claims or beliefs do not parse | `plan` `rules/information.rs:389` | `rules.information.sample_contacts` | 13 | 0 | 0 | 示せず | C |
| an edge is the Python rule's | `entity_rules_watching` `rules/tick_validation.rs:304` | `rules.tick_validation.entity_rules` | 12 | 0 | 0 | 示せず | C |
| an infrastructure event without the model | `check` `rules/after_tick.rs:641` | — | 12 | 0 | 0 | 示せず | — |
| an infrastructure event without the model | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 12 | 0 | 0 | 示せず | — |
| the queue config is out of its bounds | `policy_of` `rules/queues.rs:249` | `rules.queues.prepare_facility_queue` | 12 | 0 | 0 | 示せず | C |
| the state is for the Python rule to judge | `metric_values` `rules/resources.rs:371` | `rules.resources.metric_values` | 12 | 0 | 0 | 示せず | C |
| an agent's zone is unknown | `entity_rules_watching` `rules/tick_validation.rs:363` | `rules.tick_validation.entity_rules` | 11 | 0 | 0 | 示せず | C |
| social_network_v1: its kernel did not take it | `must_validate` `rules/after_tick.rs:207` | — | 11 | 0 | 0 | 示せず | — |
| social_network_v1: its kernel did not take it | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 11 | 0 | 0 | 示せず | — |
| the operational events are the Python rule's | `operational_schema` `rules/after_tick.rs:343` | — | 11 | 0 | 0 | 示せず | — |
| the operational events are the Python rule's | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 11 | 0 | 0 | 示せず | — |
| a limit is outside its range | `canonical_config` `rules/staff_dynamics.rs:473` | `rules.staff_dynamics.canonical_config` | 10 | 0 | 0 | 示せず | C |
| a program's assumptions the Python rule decides | `check_state` `rules/effects.rs:1926` | `rules.effects.validate_state` | 10 | 0 | 0 | 示せず | C |
| temporary_spaces_v1: its kernel did not take it | `validate_or_rule` `rules/after_tick.rs:214` | — | 10 | 0 | 0 | 示せず | — |
| temporary_spaces_v1: its kernel did not take it | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 10 | 0 | 0 | 示せず | — |
| a facility moved or was added | `check` `rules/after_tick.rs:459` | — | 9 | 0 | 0 | 示せず | — |
| a facility moved or was added | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 9 | 0 | 0 | 示せず | — |
| a facility's zone or units are the Python rule's | `entity_rules_watching` `rules/tick_validation.rs:406` | `rules.tick_validation.entity_rules` | 9 | 0 | 0 | 示せず | C |
| a program's operators the Python rule decides | `check_state` `rules/effects.rs:1923` | `rules.effects.validate_state` | 9 | 0 | 0 | 示せず | C |
| an invalid staff ID | `validate_override` `rules/staff_dynamics.rs:630` | `rules.staff_dynamics.validate_override` | 9 | 0 | 0 | 示せず | C |
| staff_dynamics_v1: its kernel did not take it | `must_validate` `rules/after_tick.rs:207` | — | 9 | 0 | 0 | 示せず | — |
| staff_dynamics_v1: its kernel did not take it | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 9 | 0 | 0 | 示せず | — |
| NaN and infinity are left to json | `float` `lib.rs:145` | `rules.parity.value_json` | 8 | 0 | 0 | 示せず | C |
| a request's agent or timing is the Python rule's | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 8 | 0 | 0 | 示せず | — |
| a space's values are not what the Python rule passes | `validate_spaces` `rules/overlay.rs:396` | `rules.overlay.validate_spaces` | 8 | 0 | 0 | 示せず | C |
| the body config changed | `check` `rules/after_tick.rs:556` | — | 8 | 0 | 0 | 示せず | — |
| the body config changed | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 8 | 0 | 0 | 示せず | — |
| the recovery is outside 0..1 | `canonical_config` `rules/staff_dynamics.rs:506` | `rules.staff_dynamics.canonical_config` | 8 | 0 | 0 | 示せず | C |
| the zone environment is the Python rule's | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 8 | 0 | 0 | 示せず | — |
| a staff member is the Python rule's | `entity_rules_watching` `rules/tick_validation.rs:602` | `rules.tick_validation.entity_rules` | 7 | 0 | 0 | 示せず | C |
| an activity is the Python rule's | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 7 | 0 | 0 | 示せず | — |
| an agent's zone is unknown | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 7 | 0 | 0 | 示せず | — |
| geometry_overlay_v1: its kernel did not take it | `validate_or_rule` `rules/after_tick.rs:214` | — | 7 | 0 | 0 | 示せず | — |
| geometry_overlay_v1: its kernel did not take it | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 7 | 0 | 0 | 示せず | — |
| population_lifecycle_v1 has an invalid exact schema | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 7 | 0 | 0 | 示せず | — |
| the zone environment is the Python rule's | `environment_schema` `rules/after_tick.rs:319` | — | 7 | 0 | 0 | 示せず | — |
| a value is not an int | `plain_int` `records.rs:45` | — | 6 | 0 | 0 | 示せず | C |
| an edge key is not its ID | `entity_rules_watching` `rules/tick_validation.rs:295` | `rules.tick_validation.entity_rules` | 6 | 0 | 0 | 示せず | C |
| the number of staff IDs is out of range | `validate_override` `rules/staff_dynamics.rs:616` | `rules.staff_dynamics.validate_override` | 6 | 0 | 0 | 示せず | C |
| NaN and infinity are left to Python | `frozen` `records.rs:64` | — | 5 | 0 | 0 | 示せず | C |
| a pending return has other fields | `check_lifecycle` `rules/population_lifecycle.rs:1358` | `rules.population_lifecycle.validate_population_lifecycle_state` | 5 | 0 | 0 | 示せず | C |
| a required namespace is missing | `check` `rules/after_tick.rs:530` | — | 5 | 0 | 0 | 示せず | — |
| a required namespace is missing | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 5 | 0 | 0 | 示せず | — |
| a staff member's skills are not a tuple | `entity_rules_watching` `rules/tick_validation.rs:620` | `rules.tick_validation.entity_rules` | 5 | 0 | 0 | 示せず | C |
| an empty staff id | `staff_key` `rules/staff_dynamics.rs:403` | `rules.staff_dynamics.canonical_config` | 5 | 0 | 0 | 示せず | C |
| policy v1 has no shifts or recovery | `canonical_config` `rules/staff_dynamics.rs:510` | `rules.staff_dynamics.canonical_config` | 5 | 0 | 0 | 示せず | C |
| someone names a request that is not open | `entity_rules_watching` `rules/tick_validation.rs:931` | `rules.tick_validation.entity_rules` | 5 | 0 | 0 | 示せず | C |
| the lifecycle state's header is other | `check_lifecycle` `rules/population_lifecycle.rs:1325` | `rules.population_lifecycle.validate_population_lifecycle_state` | 5 | 0 | 0 | 示せず | C |
| the override exceeds the limit | `validate_override` `rules/staff_dynamics.rs:640` | `rules.staff_dynamics.validate_override` | 5 | 0 | 0 | 示せず | E |
| a list of the population state is the Python rule's | `required_list` `rules/tick_validation.rs:1057` | `rules.tick_validation.population_dynamics` | 4 | 0 | 0 | 示せず | C |
| a request's status is the Python rule's | `entity_rules_watching` `rules/tick_validation.rs:918` | `rules.tick_validation.entity_rules` | 4 | 0 | 0 | 示せず | C |
| a shift has other keys | `shift_of` `rules/staff_dynamics.rs:416` | `rules.staff_dynamics.canonical_config` | 4 | 0 | 0 | 示せず | C |
| a value is not a str | `str_of` `rules/overlay.rs:152` | `rules.overlay.validate_spaces` | 4 | 0 | 0 | 示せず | C |
| an ID is not a str | `stable_id` `records.rs:34` | — | 4 | 0 | 0 | 示せず | C |
| an event integer is negative | `scheduled_event_checked` `records.rs:123` | — | 4 | 0 | 0 | 示せず | C |
| an int is out of range | `plain_int` `records.rs:47` | — | 4 | 0 | 0 | 示せず | C |
| an operational event without its state | `check` `rules/after_tick.rs:641` | — | 4 | 0 | 0 | 示せず | — |
| an operational event without its state | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 4 | 0 | 0 | 示せず | — |
| not a dict | `scheduled_event_checked` `records.rs:129` | — | 4 | 0 | 0 | 示せず | C |
| points are not a list | `points_in_value` `rules/movement_geometry.rs:396` | `rules.overlay.validate_overlay` | 4 | 0 | 0 | 示せず | C |
| the lifecycle state's parts are something else | `check_lifecycle` `rules/population_lifecycle.rs:1322` | `rules.population_lifecycle.validate_population_lifecycle_state` | 4 | 0 | 0 | 示せず | C |
| the namespace does not pass validation | `plan_long_term` `rules/psychology.rs:247` | `rules.psychology.update_long_term` | 4 | 0 | 0 | 示せず | C |
| the overlay has another shape | `check_overlay` `rules/overlay.rs:2348` | `rules.overlay.validate_overlay` | 4 | 0 | 0 | 示せず | C |
| the person cannot queue now | `request` `rules/service.rs:1224` | `rules.service.request` | 4 | 0 | 0 | 示せず | E |
| the sleep debt or the sleep efficiency is out of bounds | `read_life` `rules/psychology.rs:944` | `rules.psychology.update_psychology` | 4 | 0 | 0 | 示せず | C |
| the state has another shape | `validate_spaces` `rules/overlay.rs:365` | `rules.overlay.validate_spaces` | 4 | 0 | 0 | 示せず | C |
| the world holds another ledger | `held_mirror` `state/resource_ledger.rs:390` | `rules.tick_validation.entity_rules`, `rules.resources.metric_values` | 4 | 0 | 0 | 示せず | C |
| a body history item is the Python rule's | `body_history` `rules/after_tick.rs:290` | — | 3 | 0 | 0 | 示せず | — |
| a body history item is the Python rule's | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 3 | 0 | 0 | 示せず | — |
| a facility key is not its ID | `entity_rules_watching` `rules/tick_validation.rs:396` | `rules.tick_validation.entity_rules` | 3 | 0 | 0 | 示せず | C |
| a facility's zone or units are the Python rule's | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 3 | 0 | 0 | 示せず | — |
| a limit is outside its range | `canonical_config` `rules/staff_dynamics.rs:483` | `rules.staff_dynamics.canonical_config` | 3 | 0 | 0 | 示せず | C |
| a request's wait reason is the Python rule's | `entity_rules_watching` `rules/tick_validation.rs:853` | `rules.tick_validation.entity_rules` | 3 | 0 | 0 | 示せず | C |
| an empty source | `canonical_config` `rules/staff_dynamics.rs:463` | `rules.staff_dynamics.canonical_config` | 3 | 0 | 0 | 示せず | C |
| another orientation source | `canonical_policy` `state/agent_dynamics.rs:93` | `rules.agent_dynamics.canonical_config` | 3 | 0 | 0 | 示せず | M |
| another policy | `canonical_policy` `state/agent_dynamics.rs:84` | `rules.agent_dynamics.canonical_config` | 3 | 0 | 0 | 示せず | M |
| another policy | `canonical_config` `rules/staff_dynamics.rs:451` | `rules.staff_dynamics.canonical_config` | 3 | 0 | 0 | 示せず | M |
| another schema | `canonical_config` `rules/staff_dynamics.rs:457` | `rules.staff_dynamics.canonical_config` | 3 | 0 | 0 | 示せず | C |
| object key is not a str | `value` `lib.rs:100` | — | 3 | 0 | 0 | 示せず | C |
| object key is not a str | `frozen_object` `records.rs:94` | — | 3 | 0 | 0 | 示せず | C |
| the config has other fields | `canonical_policy` `state/agent_dynamics.rs:71` | `rules.agent_dynamics.canonical_config` | 3 | 0 | 0 | 示せず | C |
| the namespace has no row | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick`, `state.resource_ledger.validate` | 3 | 0 | 0 | 示せず | — |
| the walk is not one this rule takes, or does not fit | `apply_event` `rules/travel_completion.rs:574` | `rules.travel_completion.apply` | 3 | 0 | 0 | 示せず | V |
| v1 has no orientation source | `canonical_policy` `state/agent_dynamics.rs:90` | `rules.agent_dynamics.canonical_config` | 3 | 0 | 0 | 示せず | C |
| WorldState v6 without a ledger | `check` `rules/after_tick.rs:587` | — | 2 | 0 | 0 | 示せず | — |
| WorldState v6 without a ledger | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 2 | 0 | 0 | 示せず | — |
| a contact of a person with themselves | `observe_event` `rules/social_network.rs:376` | `rules.social_network.observe_event` | 2 | 0 | 0 | 示せず | E |
| a journey's edge is unknown | `edge` `rules/movement.rs:128` | `rules.movement.advance_movement` | 2 | 0 | 0 | 示せず | C |
| a limit of another service | `policy_of` `rules/queues.rs:225` | `rules.queues.prepare_facility_queue` | 2 | 0 | 0 | 示せず | C |
| a member has no record | `plan` `rules/staff_dynamics.rs:225` | `rules.staff_dynamics.update` | 2 | 0 | 0 | 示せず | C |
| a need is the Python rule's | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 2 | 0 | 0 | 示せず | — |
| a new value is out of bounds | `update_psychology` `rules/psychology.rs:1223` | `rules.psychology.update_psychology` | 2 | 0 | 0 | 示せず | C |
| a part of the overlay is another value | `check_overlay` `rules/overlay.rs:2355` | `rules.overlay.validate_overlay` | 2 | 0 | 0 | 示せず | C |
| a pending request keeps an assignment | `entity_rules_watching` `rules/tick_validation.rs:908` | `rules.tick_validation.entity_rules` | 2 | 0 | 0 | 示せず | C |
| a person has no body record | `record` `rules/body_state.rs:353` | `rules.body_state.record_social_event` | 2 | 0 | 0 | 示せず | C |
| a person stands in a queue slot, which depends on the people ahead | `stationary` `rules/movement.rs:247` | `rules.movement.advance_movement` | 2 | 0 | 0 | 示せず | V |
| a queue reservation has no matching task | `plan_trip` `rules/queues.rs:579` | `rules.queues.progress_physical_queue_positions` | 2 | 0 | 0 | 示せず | E |
| a request's attempt is the Python rule's | `entity_rules_watching` `rules/tick_validation.rs:840` | `rules.tick_validation.entity_rules` | 2 | 0 | 0 | 示せず | C |
| a resource queue task is the Python rule's | `entity_rules_watching` `rules/tick_validation.rs:559` | `rules.tick_validation.entity_rules` | 2 | 0 | 0 | 示せず | C |
| a room is not what its contract takes | `check_overlay` `rules/overlay.rs:2457` | `rules.overlay.validate_overlay` | 2 | 0 | 0 | 示せず | C |
| a snapshot's value is out of range | `snapshot_is_plain` `rules/population_lifecycle.rs:1290` | `rules.population_lifecycle.validate_population_lifecycle_state` | 2 | 0 | 0 | 示せず | C |
| a staff key is not its ID | `entity_rules_watching` `rules/tick_validation.rs:595` | `rules.tick_validation.entity_rules` | 2 | 0 | 0 | 示せず | C |
| a staff member is the Python rule's | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 2 | 0 | 0 | 示せず | — |
| a staff member's skills are not a tuple | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 2 | 0 | 0 | 示せず | — |
| a staff status has no available | `is_available` `rules/staff_dynamics.rs:80` | `rules.staff_dynamics.update` | 2 | 0 | 0 | 示せず | C |
| a travel task needs a moving agent | `entity_rules_watching` `rules/tick_validation.rs:775` | `rules.tick_validation.entity_rules` | 2 | 0 | 0 | 示せず | C |
| a value is not a dict | `map_of` `rules/overlay.rs:131` | `rules.overlay.validate_overlay` | 2 | 0 | 0 | 示せず | C |
| a view value is not a float | `exact_float` `rules/world_view.rs:260` | `rules.psychology.update_psychology`, `rules.world_view.view_after_movement` | 2 | 0 | 0 | 示せず | C |
| an ID has a character that JSON escapes | `plain_id` `rules/social_network.rs:54` | `rules.social_network.observe_event` | 2 | 0 | 0 | 示せず | T |
| an active agent has no profile | `population_dynamics` `rules/tick_validation.rs:1114` | `rules.tick_validation.population_dynamics` | 2 | 0 | 0 | 示せず | C |
| an affect value is for the Python rule to judge | `affect_of` `rules/social_dialogue.rs:348` | `rules.social_dialogue.capture_context` | 2 | 0 | 0 | 示せず | — |
| an agent's initial needs are the Python rule's | `entity_rules_watching` `rules/tick_validation.rs:338` | `rules.tick_validation.entity_rules` | 2 | 0 | 0 | 示せず | C |
| an archived tie has other fields | `check_lifecycle` `rules/population_lifecycle.rs:1419` | `rules.population_lifecycle.validate_population_lifecycle_state` | 2 | 0 | 0 | 示せず | C |
| an assigned request's staff or task is unknown | `entity_rules_watching` `rules/tick_validation.rs:863` | `rules.tick_validation.entity_rules` | 2 | 0 | 0 | 示せず | C |
| an event without a source | `of` `rules/population_lifecycle.rs:72` | `rules.population_lifecycle.apply_presence_action` | 2 | 0 | 0 | 示せず | C |
| an int is too large | `exact_int` `core/row.rs:487` | `rules.staff_dynamics.update`, `rules.staff_dynamics.on_shift` | 2 | 0 | 0 | 示せず | C |
| an unscheduled event without the derived mark | `check_state` `rules/effects.rs:2003` | `rules.effects.validate_state` | 2 | 0 | 0 | 示せず | C |
| another schema | `canonical_policy` `state/agent_dynamics.rs:76` | `rules.agent_dynamics.canonical_config` | 2 | 0 | 0 | 示せず | C |
| another schema version | `policy_of` `rules/queues.rs:212` | `rules.queues.prepare_facility_queue` | 2 | 0 | 0 | 示せず | C |
| no agents: the Python rule divides by zero | `kpi_values` `rules/metrics.rs:240` | `rules.metrics.kpi_values` | 2 | 0 | 0 | 示せず | C |
| no such task or person | `apply_event` `rules/travel_completion.rs:473` | `rules.travel_completion.apply` | 2 | 0 | 0 | 示せず | C |
| operational_events_v1 has no staff_status | `staff_status` `rules/staff_dynamics.rs:62` | `rules.staff_dynamics.update` | 2 | 0 | 0 | 示せず | C |
| resource_ledger_v1: its kernel did not take it | `must_validate` `rules/after_tick.rs:207` | — | 2 | 0 | 0 | 示せず | — |
| resource_ledger_v1: its kernel did not take it | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 2 | 0 | 0 | 示せず | — |
| shared operations without their contract | `check` `rules/after_tick.rs:578` | — | 2 | 0 | 0 | 示せず | — |
| shared operations without their contract | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 2 | 0 | 0 | 示せず | — |
| someone is missing from the facilities | `entity_rules_watching` `rules/tick_validation.rs:476` | `rules.tick_validation.entity_rules` | 2 | 0 | 0 | 示せず | C |
| someone names a request that is not open | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 2 | 0 | 0 | 示せず | — |
| the config has no shift windows | `on_shift` `rules/staff_dynamics.rs:88` | `rules.staff_dynamics.on_shift` | 2 | 0 | 0 | 示せず | C |
| the config has other fields | `canonical_config` `rules/staff_dynamics.rs:442` | `rules.staff_dynamics.canonical_config` | 2 | 0 | 0 | 示せず | C |
| the event already has a receipt | `plan_record` `rules/common_operations.rs:340` | `rules.common_operations.record_shared_operation` | 2 | 0 | 0 | 示せず | E |
| the lifecycle state has other fields | `check_lifecycle` `rules/population_lifecycle.rs:1308` | `rules.population_lifecycle.validate_population_lifecycle_state` | 2 | 0 | 0 | 示せず | C |
| the log already has this receipt's identity | `receipt` `rules/action_lifecycle.rs:387` | `rules.action_lifecycle.record_action_proposal` | 2 | 0 | 0 | 示せず | E |
| the payload lacks an ID | `required` `rules/social_network.rs:348` | `rules.social_network.observe_event` | 2 | 0 | 0 | 示せず | C |
| the person cannot queue now | `queue_up` `rules/toilet.rs:122` | `rules.toilet.request` | 2 | 0 | 0 | 示せず | E |
| the person has a toilet request | `queue_up` `rules/toilet.rs:126` | `rules.toilet.request` | 2 | 0 | 0 | 示せず | E |
| the queue config has another field | `policy_of` `rules/queues.rs:242` | `rules.queues.prepare_facility_queue` | 2 | 0 | 0 | 示せず | C |
| the relationships are near their bound | `apply` `rules/social_network.rs:197` | `rules.social_network.observe_event` | 2 | 0 | 0 | 示せず | E |
| the service's state does not match | `complete_event` `rules/service.rs:956` | `rules.service.complete` | 2 | 0 | 0 | 示せず | E |
| the walk does not match its task | `apply_event` `rules/travel_completion.rs:529` | `rules.travel_completion.apply` | 2 | 0 | 0 | 示せず | E |
| the world's events are not in Rust | `of` `state/events.rs:1150` | `rules.effects.validate_state`, `state.events.schedule_row` | 2 | 0 | 0 | 示せず | C |
| a body history item lacks a field | `body_history` `rules/after_tick.rs:278` | — | 1 | 0 | 0 | 示せず | — |
| a body history item lacks a field | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 1 | 0 | 0 | 示せず | — |
| a body history item's actors are the Python rule's | `body_history` `rules/after_tick.rs:293` | — | 1 | 0 | 0 | 示せず | — |
| a body history item's actors are the Python rule's | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 1 | 0 | 0 | 示せず | — |
| a body value does not fit a float | `body_value` `rules/action_lifecycle.rs:219` | `rules.action_lifecycle.record_action_proposal` | 1 | 0 | 0 | 示せず | C |
| a call raised AttributeError | `refused_by_python` `rules/effects.rs:75` | `rules.travel_completion.apply` | 1 | 0 | 0 | 示せず | — |
| a contact field is negative | `plan` `rules/social_propagation.rs:106` | `rules.social_propagation.update_social_propagation` | 1 | 0 | 0 | 示せず | C |
| a cooldown is negative | `plan` `rules/social_propagation.rs:109` | `rules.social_propagation.update_social_propagation` | 1 | 0 | 0 | 示せず | C |
| a facility has duplicate entries | `entity_rules_watching` `rules/tick_validation.rs:427` | `rules.tick_validation.entity_rules` | 1 | 0 | 0 | 示せず | C |
| a facility key is not its ID | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 1 | 0 | 0 | 示せず | — |
| a facility kind is not ASCII | `is_toilet` `rules/metrics.rs:33` | `rules.metrics.snapshot_fields` | 1 | 0 | 0 | 示せず | C |
| a facility kind that casefold may change | `service_of` `rules/queues.rs:137` | `rules.queues.prepare_facility_queue` | 1 | 0 | 0 | 示せず | C |
| a group id is empty | `read_view_people` `rules/psychology.rs:973` | `rules.psychology.update_psychology` | 1 | 0 | 0 | 示せず | C |
| a guard compares a large int with a float | `numeric_order` `rules/effects.rs:361` | `rules.effects.should_apply_guard` | 1 | 0 | 0 | 示せず | C |
| a history record has another shape | `check_overlay` `rules/overlay.rs:2381` | `rules.overlay.validate_overlay` | 1 | 0 | 0 | 示せず | C |
| a legacy queue checkpoint | `check` `rules/after_tick.rs:537` | — | 1 | 0 | 0 | 示せず | — |
| a legacy queue checkpoint | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 1 | 0 | 0 | 示せず | — |
| a monitor value is negative or not finite | `plan_monitor` `rules/psychology.rs:160` | `rules.psychology.update_long_term` | 1 | 0 | 0 | 示せず | — |
| a pending request keeps an assignment | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 1 | 0 | 0 | 示せず | — |
| a pending request needs one timeout | `entity_rules_watching` `rules/tick_validation.rs:912` | `rules.tick_validation.entity_rules` | 1 | 0 | 0 | 示せず | C |
| a pending request reports its writes | `assign_none` `rules/staff.rs:709` | `rules.staff.assign_none` | 1 | 0 | 0 | 示せず | V |
| a person is in an unknown zone | `read_rows` `rules/world_view.rs:765` | `rules.world_view.view_after_movement` | 1 | 0 | 0 | 示せず | C |
| a person stands in the queue twice | `prepare_facility_queue` `rules/queues.rs:385` | `rules.queues.prepare_facility_queue` | 1 | 0 | 0 | 示せず | C |
| a point is not two floats | `point_in_value` `rules/movement_geometry.rs:386` | `rules.overlay.validate_overlay` | 1 | 0 | 0 | 示せず | C |
| a position is not a usable point | `position_of` `rules/overlay.rs:735` | `rules.proposals.apply_effect` | 1 | 0 | 0 | 示せず | C |
| a presence transition has another field | `transition_is_plain` `rules/population.rs:407` | `rules.population.admit_pending_arrivals` | 1 | 0 | 0 | 示せず | C |
| a proposal has invalid decision timing | `(macro or helper)` `/home/water7/.rustup/toolchains/stable-x86_64-unknown-linux-gnu/lib/rustlib/src/rust/library/core/src/ops/function.rs:250` | `rules.behavior.propose_due` | 1 | 0 | 0 | 示せず | — |
| a request's attempt is the Python rule's | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 1 | 0 | 0 | 示せず | — |
| a request's wait reason is the Python rule's | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 1 | 0 | 0 | 示せず | — |
| a reservation has another shape | `check_overlay` `rules/overlay.rs:2597` | `rules.overlay.validate_overlay` | 1 | 0 | 0 | 示せず | C |
| a reservation is not what the Python rule passes | `check_overlay` `rules/overlay.rs:2614` | `rules.overlay.validate_overlay` | 1 | 0 | 0 | 示せず | C |
| a resource queue is the Python rule's | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 1 | 0 | 0 | 示せず | — |
| a resource queue is the Python rule's | `entity_rules_watching` `rules/tick_validation.rs:460` | — | 1 | 0 | 0 | 示せず | C |
| a resource task is the Python rule's | `entity_rules_watching` `rules/tick_validation.rs:555` | `rules.tick_validation.entity_rules` | 1 | 0 | 0 | 示せず | C |
| a resource task's references are the Python rule's | `entity_rules_watching` `rules/tick_validation.rs:552` | `rules.tick_validation.entity_rules` | 1 | 0 | 0 | 示せず | C |
| a room in an unknown zone | `check_overlay` `rules/overlay.rs:2460` | `rules.overlay.validate_overlay` | 1 | 0 | 0 | 示せず | C |
| a room is not what the Python rule passes | `check_overlay` `rules/overlay.rs:2463` | `rules.overlay.validate_overlay` | 1 | 0 | 0 | 示せず | C |
| a room references an unknown route | `check_overlay` `rules/overlay.rs:2475` | `rules.overlay.validate_overlay` | 1 | 0 | 0 | 示せず | C |
| a room's active is not a bool | `check_overlay` `rules/overlay.rs:2430` | `rules.overlay.validate_overlay` | 1 | 0 | 0 | 示せず | C |
| a route's overlay has another shape | `check_overlay` `rules/overlay.rs:2543` | `rules.overlay.validate_overlay` | 1 | 0 | 0 | 示せず | C |
| a space has another shape | `validate_spaces` `rules/overlay.rs:374` | `rules.overlay.validate_spaces` | 1 | 0 | 0 | 示せず | C |
| a space has another shape | `validate_spaces` `rules/overlay.rs:380` | `rules.overlay.validate_spaces` | 1 | 0 | 0 | 示せず | C |
| a space's source is another value | `validate_spaces` `rules/overlay.rs:401` | `rules.overlay.validate_spaces` | 1 | 0 | 0 | 示せず | C |
| a space's status is another value | `validate_spaces` `rules/overlay.rs:404` | `rules.overlay.validate_spaces` | 1 | 0 | 0 | 示せず | C |
| a staff member's work is the Python rule's | `entity_rules_watching` `rules/tick_validation.rs:614` | `rules.tick_validation.entity_rules` | 1 | 0 | 0 | 示せず | C |
| a staff queue is the Python rule's | `entity_rules_watching` `rules/tick_validation.rs:606` | `rules.tick_validation.entity_rules` | 1 | 0 | 0 | 示せず | C |
| a state change does not match the world | `state_changes` `rules/common_operations.rs:157` | `rules.common_operations.record_shared_operation` | 1 | 0 | 0 | 示せず | E |
| a state is not a plain dict | `required_dict` `rules/world_view.rs:315` | `rules.world_view.view_after_movement` | 1 | 0 | 0 | 示せず | C |
| a toilet queue task is the Python rule's | `entity_rules_watching` `rules/tick_validation.rs:577` | `rules.tick_validation.entity_rules` | 1 | 0 | 0 | 示せず | C |
| a toilet task's references are the Python rule's | `entity_rules_watching` `rules/tick_validation.rs:570` | `rules.tick_validation.entity_rules` | 1 | 0 | 0 | 示せず | C |
| a travel event's target differs | `entity_rules_watching` `rules/tick_validation.rs:796` | `rules.tick_validation.entity_rules` | 1 | 0 | 0 | 示せず | C |
| a travel task is the Python rule's | `entity_rules_watching` `rules/tick_validation.rs:547` | `rules.tick_validation.entity_rules` | 1 | 0 | 0 | 示せず | C |
| a travel task's references are the Python rule's | `entity_rules_watching` `rules/tick_validation.rs:544` | `rules.tick_validation.entity_rules` | 1 | 0 | 0 | 示せず | C |
| a trust is outside 0..1 | `update_psychology` `rules/psychology.rs:1163` | `rules.psychology.update_psychology` | 1 | 0 | 0 | 示せず | C |
| a value is outside 0..1 | `people` `rules/psychology.rs:97` | `rules.psychology.update_long_term` | 1 | 0 | 0 | 示せず | C |
| a value leaves what this kernel holds | `plan` `rules/staff_dynamics.rs:233` | `rules.staff_dynamics.update` | 1 | 0 | 0 | 示せず | C |
| a view value is not a str | `exact_text` `rules/world_view.rs:274` | `rules.world_view.view_after_movement` | 1 | 0 | 0 | 示せず | C |
| a view value is not an int | `exact_i64` `rules/world_view.rs:267` | `rules.psychology.update_psychology` | 1 | 0 | 0 | 示せず | C |
| a zone environment event without the policy | `check` `rules/after_tick.rs:641` | — | 1 | 0 | 0 | 示せず | — |
| a zone environment event without the policy | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 1 | 0 | 0 | 示せず | — |
| an ID counter is negative | `entity_rules_watching` `rules/tick_validation.rs:974` | `rules.tick_validation.entity_rules` | 1 | 0 | 0 | 示せず | C |
| an active action does not fit its row | `into_rows` `rules/action_lifecycle.rs:135` | `rules.action_lifecycle.record_action_proposal` | 1 | 0 | 0 | 示せず | C |
| an admission has another shape | `check_overlay` `rules/overlay.rs:2622` | `rules.overlay.validate_overlay` | 1 | 0 | 0 | 示せず | C |
| an affect model this kernel doesn't know | `models_of` `rules/body_state.rs:681` | `rules.body_state.record_social_event` | 1 | 0 | 0 | 示せず | M |
| an agent's initial needs are the Python rule's | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 1 | 0 | 0 | 示せず | — |
| an assigned request lacks one matching staff task | `entity_rules_watching` `rules/tick_validation.rs:871` | `rules.tick_validation.entity_rules` | 1 | 0 | 0 | 示せず | C |
| an assigned request must seek help or travel to staff | `entity_rules_watching` `rules/tick_validation.rs:892` | `rules.tick_validation.entity_rules` | 1 | 0 | 0 | 示せず | C |
| an assigned request's staff or task is unknown | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 1 | 0 | 0 | 示せず | — |
| an exponent is out of range | `(macro or helper)` `/home/water7/.rustup/toolchains/stable-x86_64-unknown-linux-gnu/lib/rustlib/src/rust/library/core/src/ops/function.rs:250` | `rules.behavior.propose_due` | 1 | 0 | 0 | 示せず | — |
| an initial facility is not a stable ID | `awareness_of` `rules/agent_dynamics.rs:159` | `rules.agent_dynamics.create_state` | 1 | 0 | 0 | 示せず | C |
| an initial facility repeats | `awareness_of` `rules/agent_dynamics.rs:165` | `rules.agent_dynamics.create_state` | 1 | 0 | 0 | 示せず | C |
| an unknown shipment | `delivery_delay_on` `rules/handlers.rs:572` | `rules.handlers.delivery_delay` | 1 | 0 | 0 | 示せず | E |
| another awareness schema | `awareness_of` `rules/agent_dynamics.rs:148` | `rules.agent_dynamics.create_state` | 1 | 0 | 0 | 示せず | C |
| another awareness source | `awareness_of` `rules/agent_dynamics.rs:179` | `rules.agent_dynamics.create_state` | 1 | 0 | 0 | 示せず | M |
| another awareness source type | `awareness_of` `rules/agent_dynamics.rs:174` | `rules.agent_dynamics.create_state` | 1 | 0 | 0 | 示せず | C |
| another fatigue formula | `fatigue_formula` `rules/staff_dynamics.rs:120` | `rules.staff_dynamics.update` | 1 | 0 | 0 | 示せず | M |
| another schema version | `update_social_propagation` `rules/social_propagation.rs:565` | `rules.social_propagation.update_social_propagation` | 1 | 0 | 0 | 示せず | C |
| another staff operations formula | `assign_operations` `rules/staff.rs:1128` | `rules.staff.assign_operations` | 1 | 0 | 0 | 示せず | M |
| discovery is not a bool | `awareness_of` `rules/agent_dynamics.rs:170` | `rules.agent_dynamics.create_state` | 1 | 0 | 0 | 示せず | C |
| infrastructure state without its model | `check` `rules/after_tick.rs:610` | — | 1 | 0 | 0 | 示せず | — |
| infrastructure state without its model | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 1 | 0 | 0 | 示せず | — |
| left i64 | `sum_nums` `rules/parity.rs:107` | `rules.parity.sum_nums` | 1 | 0 | 0 | 示せず | C |
| no active task | `complete_event` `rules/toilet.rs:610` | `rules.toilet.complete` | 1 | 0 | 0 | 示せず | E |
| no such agent | `add_agent_stress` `state/entities.rs:1531` | `state.entities.add_agent_stress` | 1 | 0 | 0 | 示せず | C |
| no such person | `person_of` `rules/behavior.rs:1002` | `rules.behavior.observed_state` | 1 | 0 | 0 | 示せず | E |
| not a WorldState v6 | `check` `rules/after_tick.rs:432` | — | 1 | 0 | 0 | 示せず | — |
| not a WorldState v6 | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 1 | 0 | 0 | 示せず | — |
| not a queue service's kind | `service_of` `rules/queues.rs:146` | `rules.queues.prepare_facility_queue` | 1 | 0 | 0 | 示せず | C |
| not the service's quantity | `complete_event` `rules/service.rs:919` | `rules.service.complete` | 1 | 0 | 0 | 示せず | C |
| object key is not a str | `read` `core/value.rs:69` | `rules.parity.value_round_trip` | 1 | 0 | 0 | 示せず | C |
| operational events without their state | `check` `rules/after_tick.rs:604` | — | 1 | 0 | 0 | 示せず | — |
| operational events without their state | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 1 | 0 | 0 | 示せず | — |
| population_lifecycle_v1 pending return is invalid | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 1 | 0 | 0 | 示せず | — |
| resource_service_v1: its kernel did not take it | `must_validate` `rules/after_tick.rs:207` | — | 1 | 0 | 0 | 示せず | — |
| resource_service_v1: its kernel did not take it | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 1 | 0 | 0 | 示せず | — |
| someone in a toilet has no task | `entity_rules_watching` `rules/tick_validation.rs:636` | `rules.tick_validation.entity_rules` | 1 | 0 | 0 | 示せず | C |
| someone in a toilet queue has no task | `entity_rules_watching` `rules/tick_validation.rs:631` | `rules.tick_validation.entity_rules` | 1 | 0 | 0 | 示せず | C |
| someone moving has no travel task | `entity_rules_watching` `rules/tick_validation.rs:808` | `rules.tick_validation.entity_rules` | 1 | 0 | 0 | 示せず | C |
| someone's open request is the Python rule's | `entity_rules_watching` `rules/tick_validation.rs:926` | `rules.tick_validation.entity_rules` | 1 | 0 | 0 | 示せず | C |
| the 200-facility bound | `add_structure` `rules/effects.rs:1659` | `rules.effects.structure` | 1 | 0 | 0 | 示せず | E |
| the active reservations differ from the active tasks | `entity_rules_watching` `rules/tick_validation.rs:767` | `rules.tick_validation.entity_rules` | 1 | 0 | 0 | 示せず | C |
| the awareness config has other fields | `awareness_of` `rules/agent_dynamics.rs:182` | `rules.agent_dynamics.create_state` | 1 | 0 | 0 | 示せず | C |
| the body state is for the Python rule | `complete_support_event` `rules/support.rs:390` | `rules.support.complete_support` | 1 | 0 | 0 | 示せず | T |
| the completion does not match its task | `complete_support_event` `rules/support.rs:351` | `rules.support.complete_support` | 1 | 0 | 0 | 示せず | E |
| the delay does not move a not-yet-due delivery forward | `delivery_delay_on` `rules/handlers.rs:596` | `rules.handlers.delivery_delay` | 1 | 0 | 0 | 示せず | C |
| the delta has body values and the world no body records | `apply_deltas` `rules/psychology.rs:1364` | `rules.psychology.apply_deltas` | 1 | 0 | 0 | 示せず | C |
| the event counter is not past the sequences | `entity_rules_watching` `rules/tick_validation.rs:987` | `rules.tick_validation.entity_rules` | 1 | 0 | 0 | 示せず | C |
| the failures exceed the installed units | `capacities_of` `rules/handlers.rs:1572` | `rules.handlers.infrastructure` | 1 | 0 | 0 | 示せず | E |
| the field holds another dict | `field_mirror_of` `core/world_core.rs:1370` | `rules.psychology.update_long_term` | 1 | 0 | 0 | 示せず | C |
| the initial agents repeat an ID | `population_dynamics` `rules/tick_validation.rs:1108` | `rules.tick_validation.population_dynamics` | 1 | 0 | 0 | 示せず | C |
| the inventory is the Python rule's | `entity_rules_watching` `rules/tick_validation.rs:967` | `rules.tick_validation.entity_rules` | 1 | 0 | 0 | 示せず | C |
| the ledger was removed | `capture` `rules/metrics.rs:366` | `rules.metrics.capture` | 1 | 0 | 0 | 示せず | — |
| the minute is not an int | `update_long_term` `rules/psychology.rs:365` | `rules.psychology.update_long_term` | 1 | 0 | 0 | 示せず | C |
| the minute is not the world's | `assign_none` `rules/staff.rs:661` | `rules.staff.assign_none` | 1 | 0 | 0 | 示せず | C |
| the namespace is not at the previous minute | `plan_long_term` `rules/psychology.rs:250` | `rules.psychology.update_long_term` | 1 | 0 | 0 | 示せず | C |
| the new values do not pass validation | `plan_long_term` `rules/psychology.rs:302` | `rules.psychology.update_long_term` | 1 | 0 | 0 | 示せず | C |
| the payload has other fields | `delivery_delay_on` `rules/handlers.rs:563` | `rules.handlers.delivery_delay` | 1 | 0 | 0 | 示せず | C |
| the presence's people are not a dict | `write_household` `rules/decisions.rs:409` | `rules.decisions.record_outcome` | 1 | 0 | 0 | 示せず | C |
| the request is another person's | `timeout_event` `rules/support.rs:94` | `rules.support.timeout` | 1 | 0 | 0 | 示せず | E |
| the request's identity changed | `plan_record` `rules/common_operations.rs:351` | `rules.common_operations.record_shared_operation` | 1 | 0 | 0 | 示せず | E |
| the requests are not the ones held | `check` `rules/after_tick.rs:491` | — | 1 | 0 | 0 | 示せず | — |
| the requests are not the ones held | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 1 | 0 | 0 | 示せず | — |
| the required sleep is out of bounds | `read_life` `rules/psychology.rs:910` | `rules.psychology.update_psychology` | 1 | 0 | 0 | 示せず | C |
| the reservation does not match | `complete_event` `rules/service.rs:975` | `rules.service.complete` | 1 | 0 | 0 | 示せず | E |
| the reservations are still to be made | `plan_bed` `rules/overlay.rs:982` | `rules.overlay.reserve_sleep_bed` | 1 | 0 | 0 | 示せず | V |
| the restoration is for the Python rule to judge | `staff_restoration` `rules/handlers.rs:859` | `rules.handlers.staff_restoration` | 1 | 0 | 0 | 示せず | C |
| the service's state does not match | `complete_event` `rules/toilet.rs:640` | `rules.toilet.complete` | 1 | 0 | 0 | 示せず | E |
| the task does not match | `complete_event` `rules/service.rs:940` | `rules.service.complete` | 1 | 0 | 0 | 示せず | E |
| the view has no hash of a state | `due_outcomes` `rules/behavior.rs:874` | `rules.behavior.propose_due` | 1 | 0 | 0 | 示せず | C |
| the walk has no live reservation | `check_sleep_bed` `rules/travel_completion.rs:378` | `rules.travel_completion.apply` | 1 | 0 | 0 | 示せず | C |
| the world and the deltas have different people | `people` `rules/psychology.rs:90` | `rules.psychology.update_long_term` | 1 | 0 | 0 | 示せず | C |
| the zone environment is the Python rule's | `environment_schema` `rules/after_tick.rs:311` | — | 1 | 0 | 0 | 示せず | — |
| the zones changed | `check` `rules/after_tick.rs:456` | — | 1 | 0 | 0 | 示せず | — |
| the zones changed | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 1 | 0 | 0 | 示せず | — |
| two deltas for one person | `people` `rules/psychology.rs:108` | `rules.psychology.update_long_term` | 1 | 0 | 0 | 示せず | C |
| values are not finite | `fsum` `contacts.rs:19` | — | 1 | 0 | 0 | 示せず | C |
| a step of the controller is patched | `accept` `rules/tick.rs:450` | `rules.tick.run_tick` | 2,648 | 0 | 0 | 印 | H |
| a rule of the check is patched | `check` `rules/after_tick.rs:405` | — | 1,316 | 0 | 0 | 印 | — |
| a rule of the check is patched | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 1,316 | 0 | 0 | 印 | — |
| a benchmark's shadow is installed | `accept` `rules/tick.rs:428` | `rules.tick.run_tick` | 1,050 | 0 | 0 | 印 | H |
| a benchmark's shadow is installed | `after_tick` `rules/after_tick.rs:711` | — | 1,045 | 0 | 0 | 印 | — |
| a benchmark's shadow is installed | `after_tick` `rules/after_tick.rs:726` | `rules.after_tick.after_tick` | 1,045 | 0 | 0 | 印 | — |
| the proposal is a Python object | `record_outcome` `rules/decisions.rs:529` | `rules.decisions.record_outcome`, `or_rule decisions::record_outcome` | 750 | 0 | 0 | 印 | H |
| the view is Python's | `snapshot_of` `rules/behavior.rs:663` | `rules.decisions.observed_row`, `rules.decisions.propose_rows` | 334 | 0 | 0 | 印 | H |
| a rule a step calls is patched | `accept` `rules/tick.rs:458` | `rules.tick.run_tick` | 265 | 0 | 0 | 印 | H |
| the RNG is not a CounterRNG | `due_outcomes` `rules/behavior.rs:862` | `or_rule decisions::propose_rows`, `rules.decisions.propose_rows` | 241 | 0 | 0 | 印 | H |
| the RNG is not a CounterRNG | `update_social_propagation` `rules/social_propagation.rs:453` | `rules.social_propagation.update_social_propagation`, `or_rule social_propagation::update_social_propagation` | 241 | 0 | 0 | 印 | H |
| the random source is not a CounterRNG | `inputs` `rules/information.rs:186` | `rules.information.sample_contacts`, `or_rule information::sample_contacts` | 241 | 0 | 0 | 印 | H |
| the prior view is Python's | `view_for_proposals` `rules/world_view.rs:1824` | `rules.world_view.view_for_proposals`, `or_rule world_view::view_for_proposals` | 146 | 0 | 0 | 印 | H |
| the world's whole validation is Python's | `request` `rules/toilet.rs:193` | `rules.toilet.request` | 72 | 0 | 0 | 印 | H |
| the step validates the state | `assign_none` `rules/staff.rs:656` | `rules.staff.assign_none` | 46 | 0 | 0 | 印 | H |
| a step of this controller is patched | `accept` `rules/tick.rs:453` | `rules.tick.run_tick` | 44 | 0 | 0 | 印 | H |
| the world's whole validation is Python's | `request` `rules/service.rs:1166` | `rules.service.request` | 40 | 0 | 0 | 印 | H |
| the RNG is not a CounterRNG | `start_one` `rules/toilet.rs:319` | `rules.toilet.start_head`, `or_rule toilet::start_head` | 33 | 0 | 0 | 印 | H |
| the manager's request is another function | `arrive` `rules/travel_completion.rs:289` | `rules.travel_completion.after_arrival` | 5 | 0 | 0 | 印 | H |
| the world's whole validation is Python's | `schedule_travel` `rules/travel.rs:618` | `rules.travel.schedule_travel` | 3 | 0 | 0 | 印 | H |
| the policy is another class | `due_outcomes` `rules/behavior.rs:859` | `rules.behavior.propose_due`, `rules.decisions.propose_rows` | 2 | 0 | 0 | 印 | H |
| a delta of another class | `update_social_propagation` `rules/social_propagation.rs:477` | `rules.social_propagation.update_social_propagation` | 1 | 0 | 0 | 印 | H |
| another kind of dispatcher | `run_inner` `rules/dispatch.rs:1076` | `rules.dispatch.run_phase` | 1 | 0 | 0 | 印 | H |
| the controller is bound to another world | `accept` `rules/tick.rs:435` | `rules.tick.run_tick` | 1 | 0 | 0 | 印 | H |
| the decisions are another class | `due_outcomes` `rules/behavior.rs:846` | `rules.behavior.propose_due` | 1 | 0 | 0 | 印 | H |
| the observed state is a Python object | `record_outcome` `rules/decisions.rs:556` | `rules.decisions.record_outcome` | 1 | 0 | 0 | 印 | H |
| the policy's scorer is another class | `due_outcomes` `rules/behavior.rs:855` | `rules.behavior.propose_due` | 1 | 0 | 0 | 印 | H |

</details>

### `or_rule!` と `__wrapped__` の回数（込み）

| 受けたもの | テスト | ゲート | 7日分 |
|---|--:|--:|--:|
| `__wrapped__ prepare_facility_queue` | 239 | 167 | 4 |
| `or_rule queues::prepare_facility_queue` | 239 | 167 | 4 |
| `__wrapped__ assign_staff_operations` | 56 | 7 | 2 |
| `or_rule staff::assign_operations` | 56 | 7 | 2 |
| `or_rule proposals::apply_effect` | 522 | 187 | 1 |
| `or_rule social_propagation::update_social_propagation` | 642 | 60 | 1 |
| `or_rule psychology::update_long_term` | 32,343 | 2,320 | 0 |
| `or_rule metrics::capture` | 2,770 | 323 | 0 |
| `__wrapped__ schedule_staff_operation_relief` | 418 | 265 | 0 |
| `or_rule staff::relief` | 418 | 265 | 0 |
| `or_rule decisions::record_outcome` | 287 | 0 | 0 |
| `or_rule decisions::propose_rows` | 145 | 0 | 0 |
| `or_rule information::sample_contacts` | 145 | 0 | 0 |
| `or_rule kernel` | 46 | 0 | 0 |
| `__wrapped__ progress_physical_queue_positions` | 34 | 0 | 0 |
| `or_rule decisions::observed_row` | 32 | 0 | 0 |
| `__wrapped__ advance_movement` | 25 | 0 | 0 |
| `__wrapped__ assign_pending` | 25 | 0 | 0 |
| `__wrapped__ update_staff_operations` | 25 | 0 | 0 |
| `or_rule movement::advance_movement` | 25 | 0 | 0 |
| `or_rule psychology::apply_deltas` | 25 | 0 | 0 |
| `or_rule staff::assign_pending` | 25 | 0 | 0 |
| `or_rule staff::update_operations` | 25 | 0 | 0 |
| `or_rule world_view::view_after_movement` | 25 | 0 | 0 |
| `or_rule world_view::view_for_proposals` | 25 | 0 | 0 |
| `__wrapped__ _start_head` | 16 | 0 | 0 |
| `or_rule toilet::start_head` | 16 | 0 | 0 |
| `__wrapped__ progress_physical_queue_head_to_service` | 12 | 0 | 0 |
| `__wrapped__ _serve_head` | 6 | 0 | 0 |
| `or_rule service::serve_head` | 6 | 0 | 0 |

**書いた後の `__wrapped__` 2か所（`population.rs`、`population_lifecycle.rs`）は、全テスト、ゲート、7日分で0回です（実測）。** Python を消すと壊れる場所ですが、通る場面が1つもありません。L1 で閉じる前に、通すテストを作ります。`__wrapped__` の残りはすべて driver の `rule_of` で、`or_rule!` と同じ回数です。

### 一度も届かないか所（1,360）

**全部の一覧は [step4-refusal-sites.md](step4-refusal-sites.md) です。** 手順4に効く読み方は3つです。

- **E 181 か所：** 有効な状態で文を出す枝で、Rust の断りにも Python の文にも、どの実行も届きません。比較の道（6節、8節の L8）は何も比べられません。
- **V 30 か所：** Python に任せている有効な枝で、Rust に移すときに比べる相手がありません。
- **C 1,011 か所：** 1つの出口にまとめる予定で、1か所ずつのテストは要りません（出口のテストは要ります）。

ファイルごと：

| Rust file | 届かないか所 | 届いたか所 |
|---|--:|--:|
| `rules/overlay.rs` | 163 | 27 |
| `rules/population.rs` | 121 | 25 |
| `rules/effects.rs` | 89 | 42 |
| `rules/handlers.rs` | 85 | 14 |
| `rules/service.rs` | 76 | 9 |
| `rules/population_lifecycle.rs` | 63 | 14 |
| `rules/tick_validation.rs` | 61 | 50 |
| `rules/movement.rs` | 59 | 10 |
| `rules/toilet.rs` | 53 | 6 |
| `rules/support.rs` | 42 | 5 |
| `rules/queues.rs` | 41 | 10 |
| `rules/information_events.rs` | 39 | 4 |
| `rules/psychology.rs` | 34 | 25 |
| `rules/world_view.rs` | 34 | 9 |
| `rules/social_interaction.rs` | 31 | 6 |
| `rules/facility_queues.rs` | 29 | 2 |
| `rules/staff.rs` | 28 | 14 |
| `rules/movement_geometry.rs` | 26 | 2 |
| `rules/travel.rs` | 25 | 6 |
| `rules/after_tick.rs` | 24 | 33 |
| `rules/proposals.rs` | 22 | 1 |
| `rules/body_state.rs` | 19 | 6 |
| `rules/common_operations.rs` | 19 | 8 |
| `rules/travel_completion.rs` | 19 | 7 |
| `rules/social_propagation.rs` | 18 | 17 |
| `rules/information.rs` | 15 | 6 |
| `rules/staff_operation_events.rs` | 14 | 0 |
| `state/events.rs` | 14 | 5 |
| `rules/behavior.rs` | 13 | 7 |
| `rules/care_receipts.rs` | 13 | 2 |
| そのほか 22 ファイル | 71 | 117 |

### handlers-rest で変わるもの（基準 → 込み）

| 理由 | ゲート 基準→込み | 7日分 基準→込み | テスト 基準→込み |
|---|--:|--:|--:|
| a blocked room is the Python rule's | 14 → 0 | 0 → 0 | 27 → 0 |
| a call raised EventSuppressed | 0 → 54 | 0 → 0 | 0 → 96 |
| a call raised StateInvariantError | 1 → 2 | 0 → 0 | 1 → 2 |
| a kind that a selected care formula decides | 16 → 0 | 0 → 0 | 26 → 0 |
| a marker's guard the Python rule decides | 1 → 2 | 0 → 0 | 2 → 3 |
| a value is not a str | 13 → 1,781 | 0 → 0 | 237 → 2,126 |
| an event without a typed interaction | 52 → 0 | 0 → 0 | 91 → 0 |
| another type of interaction | 112 → 0 | 0 → 0 | 116 → 0 |
| no longer in a contact zone | 0 → 304 | 0 → 0 | 0 → 355 |
| the provenance does not match the sender | 0 → 1 | 0 → 0 | 0 → 43 |
| the residents are busy: the Python rule defers | 54 → 108 | 0 → 0 | 96 → 192 |
| the sample is stale | 0 → 2 | 0 → 0 | 0 → 6 |
| the sender's belief changed | 0 → 2 | 0 → 0 | 0 → 74 |
| the units are for the Python rule to judge | 0 → 1 | 0 → 0 | 0 → 12 |
| the walk is not one this rule takes, or does not fit | 26 → 0 | 0 → 0 | 53 → 3 |

**読み方：** `assign_pending` の断りは同じ回数のまま、受けるところが `or_rule staff::assign_pending` から `rules.staff.assign_none` に変わります（基準の7日分 `or_rule` 720回 → 込み 8回）。`a call raised EventSuppressed` などは、Rust の規則が Python の部品を呼び、その例外を断りに替えるものです。

### 予算

`tick_budget.py`、`fork-b1` の 9600分から 480 tick、5回、同時。load average 約 9〜26。計測は切った状態です。**1.026**（1.046、1.074、0.968、1.026、0.995）。A/A 1.051（0.931、1.101、0.867、1.081、1.051）。上限 1.10 以内、状態は5回とも一致。**A/A の揺れの中で、これより細かくは言えません。** 7日分の断りは 0.07 回/tick で、増えたのはその途中のフラグの読み1回と `#[track_caller]` の隠れた引数です。計測をかけた7日分は Run のスレッド時間 79.9 秒、切った回は 79.2 秒でした（1回ずつ、同時）。

## 3. 消せないもの

### 分類の作り方

AST で呼び出しのグラフを作り、関数ごとに次の順で決めました（道具はこの文書の外です）。

| グループ | 意味 | 判定 |
|---|---|---|
| **a** | Rust の道が完全。Python は比較の基準か、型が壊れたときの受け手 | `kernel_first`、`SEGMENTS`、`DRIVEN_*` があり、kernel から届く断りが C・H・O・T だけ |
| **bE** | Rust は有効な状態を扱う。**失敗の文を出すためだけに**断る | 断りに E があり、V・M・HT がない |
| **bV** | 有効な状態の枝を Rust が断り、Python が処理する | 断りに V・M・HT がある |
| **c** | Rust の道がない | tick から届くのに kernel がない |
| **d** | tick の外（web、storage、region、llm、inspection、contracts、Scenario、世界の組み立て、保存、分岐）から届く | 外の入口から、kernel で止めずに辿って届く |
| **u** | 上のどこからも静的に届かない | ほとんどが動的に呼ばれる Policy の本体（下） |

kernel のない補助関数は、呼び手のうち一番厳しいグループに入れました。

**この表の限界（はっきり書きます）：**
- 断りの集計は Rust を正規表現で辿ったもので、**多めに出ます**。共通の部品の断りが、その kernel では起きなくても数えられます。bV は上限と読んでください。
- `.method()` の呼び出しは、同じ名前の定義が4つ以下のときだけ辿ります。`get`、`apply`、`update` などの一般的な名前は辿りません。
- **`u` の 2,335 行は `policies/` です。** `explain_update`、`deltas_without_traces`、`StressPropagationPolicy.update`、`InformationSpreadPolicy.update`、`UtilityBehaviorPolicy.propose` などで、`SEGMENTS` の kernel の後ろにある Python の本体です。動的に呼ばれるので静的に届きません。**実際は bV です。** trace つきの Run が今もこれを動かします（`a-psychology.md`、`b-social-propagation.md`、読んで確かめました）。
- `core/tick_validation.py` と `core/world.py` は外の入口に入れたので d ですが、`TickValidationMemo` は実際は c です。`domain/environment.py` の `zone_environment_view` も d に入りますが、tick の規則としては Rust の道がなく c です。
- 表は人の判断ではありません。**行数の桁と場所を見るものです。** 断りの実測は2節です。

### 全体（行数）

| 範囲 | a | bE | bV | c | d | u |
|---|--:|--:|--:|--:|--:|--:|
| **tip** 全体 | 1,406 | 2,294 | 8,954 | 4,674 | 30,741 | 2,677 |
| **2レーン込み** 全体 | 2,549 | 2,930 | 10,989 | 1,819 | 29,910 | 2,677 |
| 　`domain/` | 1,926 | 1,512 | 6,903 | 373 | 9,011 | 255 |
| 　`events/` | 244 | 1,286 | 2,522 | 141 | 3,331 | 87 |
| 　`policies/` | 138 | 0 | 829 | 399 | 2,829 | 2,335 |
| 　`experiments/phases`、`runner`、`metrics`、`operational_kpis` | 241 | 132 | 735 | 846 | 1,120 | 0 |
| 　`experiments/` の残り（Scenario、比較、分岐など） | 0 | 0 | 0 | 0 | 8,372 | 0 |
| 　`core/` | 0 | 0 | 0 | 60 | 5,247 | 0 |

**消す候補の桁：** 2レーン込みで a + bE + bV + c + u = 約 21,000 行（残す `TickEngine` などを少し含みます）。残る d は `domain`、`events`、`policies` で約 15,200 行、ただしその中で規則と呼べるものは下の「d の中身」のとおり一部です。

### ファイルごと（2レーン込み。tick に関わらないファイルは省略。全部は別紙）

| file | a | bE | bV | c | d | u | tip: bV+bE / c |
|---|--:|--:|--:|--:|--:|--:|--:|
| `core/engine.py` | 0 | 0 | 0 | 60 | 98 | 0 | 0 / 60 |
| `core/tick_validation.py` | 0 | 0 | 0 | 0 | 316 | 0 | 0 / 0 |
| `core/world.py` | 0 | 0 | 0 | 0 | 2100 | 0 | 0 / 0 |
| `domain/action_lifecycle.py` | 0 | 0 | 364 | 0 | 115 | 0 | 364 / 0 |
| `domain/agent_dynamics.py` | 49 | 0 | 96 | 0 | 106 | 0 | 96 / 0 |
| `domain/body_state.py` | 0 | 0 | 393 | 0 | 479 | 2 | 393 / 0 |
| `domain/care_receipts.py` | 106 | 0 | 242 | 63 | 140 | 0 | 137 / 273 |
| `domain/common_operations.py` | 0 | 0 | 201 | 0 | 450 | 0 | 201 / 0 |
| `domain/distress_monitor.py` | 0 | 0 | 65 | 0 | 18 | 43 | 65 / 0 |
| `domain/environment.py` | 0 | 0 | 0 | 0 | 583 | 0 | 0 / 0 |
| `domain/facilities.py` | 71 | 145 | 256 | 9 | 85 | 6 | 276 / 9 |
| `domain/facility_awareness.py` | 0 | 0 | 0 | 0 | 38 | 0 | 0 / 0 |
| `domain/facility_queues.py` | 204 | 28 | 22 | 0 | 234 | 0 | 32 / 115 |
| `domain/geometry_overlay.py` | 166 | 0 | 859 | 0 | 571 | 0 | 221 / 132 |
| `domain/information.py` | 0 | 0 | 30 | 0 | 294 | 0 | 0 / 30 |
| `domain/infrastructure.py` | 0 | 0 | 17 | 0 | 659 | 0 | 17 / 0 |
| `domain/layouts.py` | 0 | 0 | 0 | 0 | 137 | 0 | 0 / 0 |
| `domain/models.py` | 0 | 0 | 0 | 8 | 757 | 0 | 0 / 8 |
| `domain/movement.py` | 200 | 0 | 1131 | 40 | 675 | 19 | 1132 / 66 |
| `domain/operational.py` | 0 | 0 | 14 | 0 | 493 | 0 | 14 / 0 |
| `domain/population.py` | 208 | 152 | 842 | 0 | 135 | 0 | 994 / 0 |
| `domain/population_lifecycle.py` | 21 | 157 | 1022 | 77 | 132 | 10 | 1179 / 77 |
| `domain/psychological_state.py` | 0 | 0 | 0 | 0 | 106 | 88 | 0 / 0 |
| `domain/queueing.py` | 0 | 0 | 336 | 0 | 21 | 0 | 277 / 0 |
| `domain/resource_service.py` | 232 | 333 | 311 | 105 | 115 | 0 | 411 / 313 |
| `domain/resources.py` | 27 | 142 | 0 | 0 | 517 | 0 | 142 / 0 |
| `domain/simulator_state.py` | 0 | 0 | 0 | 0 | 561 | 4 | 0 / 0 |
| `domain/social_dialogue.py` | 43 | 36 | 0 | 0 | 148 | 0 | 36 / 0 |
| `domain/social_network.py` | 0 | 157 | 11 | 37 | 199 | 3 | 168 / 37 |
| `domain/staff.py` | 274 | 28 | 51 | 0 | 298 | 80 | 353 / 0 |
| `domain/staff_dynamics.py` | 41 | 43 | 177 | 0 | 128 | 0 | 220 / 0 |
| `domain/staff_operations.py` | 247 | 158 | 463 | 34 | 572 | 0 | 463 / 418 |
| `domain/temporary_spaces.py` | 37 | 133 | 0 | 0 | 124 | 0 | 73 / 0 |
| `domain/zones.py` | 0 | 0 | 0 | 0 | 120 | 0 | 0 / 0 |
| `events/effects.py` | 14 | 677 | 468 | 4 | 2090 | 2 | 1145 / 4 |
| `events/handlers.py` | 230 | 609 | 2034 | 103 | 358 | 5 | 1119 / 1838 |
| `events/models.py` | 0 | 0 | 0 | 2 | 219 | 0 | 0 / 2 |
| `events/queue.py` | 0 | 0 | 20 | 32 | 646 | 80 | 20 / 32 |
| `experiments/metrics.py` | 0 | 0 | 109 | 0 | 194 | 0 | 109 / 0 |
| `experiments/operational_kpis.py` | 70 | 0 | 0 | 0 | 45 | 0 | 0 / 0 |
| `experiments/phases/arbitrate.py` | 37 | 0 | 4 | 30 | 29 | 0 | 0 / 53 |
| `experiments/phases/dispatch.py` | 4 | 0 | 77 | 160 | 60 | 0 | 77 / 160 |
| `experiments/phases/driver.py` | 0 | 0 | 0 | 0 | 112 | 0 | 0 / 0 |
| `experiments/phases/metrics.py` | 3 | 0 | 0 | 3 | 12 | 0 | 0 / 3 |
| `experiments/phases/propose.py` | 36 | 132 | 450 | 295 | 92 | 0 | 582 / 295 |
| `experiments/phases/sample.py` | 3 | 0 | 41 | 83 | 18 | 0 | 41 / 83 |
| `experiments/phases/segments.py` | 0 | 0 | 0 | 0 | 65 | 0 | 0 / 0 |
| `experiments/phases/update.py` | 66 | 0 | 54 | 275 | 39 | 0 | 54 / 275 |
| `experiments/runner.py` | 22 | 0 | 0 | 0 | 449 | 0 | 0 / 0 |
| `policies/affect.py` | 0 | 0 | 74 | 0 | 82 | 0 | 74 / 0 |
| `policies/agents_social_manifest.py` | 0 | 0 | 0 | 0 | 487 | 0 | 0 / 0 |
| `policies/alternatives.py` | 0 | 0 | 0 | 0 | 31 | 103 | 0 / 0 |
| `policies/behavior.py` | 8 | 0 | 0 | 12 | 367 | 496 | 0 / 12 |
| `policies/information.py` | 0 | 0 | 51 | 26 | 63 | 341 | 51 / 26 |
| `policies/proximity.py` | 0 | 0 | 0 | 0 | 56 | 83 | 0 / 0 |
| `policies/psychology.py` | 79 | 0 | 45 | 313 | 220 | 858 | 45 / 313 |
| `policies/queueing.py` | 0 | 0 | 20 | 0 | 113 | 0 | 20 / 0 |
| `policies/registry.py` | 0 | 0 | 87 | 14 | 754 | 40 | 95 / 6 |
| `policies/relationships.py` | 0 | 0 | 73 | 0 | 42 | 0 | 73 / 0 |
| `policies/social.py` | 0 | 0 | 53 | 0 | 192 | 391 | 53 / 0 |
| `policies/space.py` | 0 | 0 | 0 | 0 | 158 | 0 | 0 / 0 |
| `policies/world_view.py` | 51 | 0 | 426 | 34 | 240 | 16 | 426 / 34 |
省略したのは `experiments/{scenario,initial_scenario,comparison,timeline,branch,validation,profiles,...}`（すべて d）、`core/{checkpoint,checkpoint_cache,state_mirror,save_validation,...}`（すべて d）です。

### 断りの理由（文で付けた分類）

L0 の前に文を読んで付けた分類です。到達は**推測で、上の実測が優先します。** 記号はほかの節でも使います。

| 記号 | 意味 | 到達 | か所 |
|---|---|---|--:|
| **C** | 型、行の範囲、壊れた状態、検証が落ちる | **壊れた状態だけ**（テストと、`dict.__setitem__` で表を飛ばしたとき）。ただし「壊れた」の判定は文からの推測 | 1,305 |
| **E** | 状態は有効で、規則が拒否か抑止をする | **有効な状態で届く** | 267 |
| **V** | 有効な状態の枝を Python に任せる | **有効な状態で届く** | 53 |
| **T** | 有効だが変わった入力。共通の変更で閉じる | **有効な状態で届く** | 26 |
| **M** | 選べるモデルかもしれない ID を知らない | **ほとんど届かない**（下） | 27 |
| **HT** | trace、Provider | **有効な Run で届く** | 6 |
| **H** | テストの偽物、patch、shadow、`validate_state=True` | 手順4で印ごと消える | 32 |
| **O** | 古い保存の形（WorldState v5、情報の schema 1、列の旧方式） | 旧モデルを閲覧のみにすると tick では届かない | 4 |

| Rust file | C | E | V | T | M | H | HT | O |
|---|--:|--:|--:|--:|--:|--:|--:|--:|
| `rules/population.rs` | 111 | 22 | 8 | 3 | 0 | 0 | 0 | 0 |
| `rules/overlay.rs` | 118 | 10 | 7 | 2 | 0 | 0 | 0 | 0 |
| `rules/effects.rs` | 76 | 55 | 0 | 0 | 0 | 0 | 0 | 0 |
| `rules/tick_validation.rs` | 109 | 0 | 0 | 0 | 0 | 0 | 0 | 2 |
| `rules/handlers.rs` | 62 | 35 | 2 | 0 | 0 | 0 | 0 | 0 |
| `rules/service.rs` | 65 | 18 | 0 | 0 | 0 | 1 | 0 | 0 |
| `rules/population_lifecycle.rs` | 46 | 26 | 3 | 2 | 0 | 0 | 0 | 0 |
| `rules/movement.rs` | 57 | 4 | 4 | 0 | 3 | 0 | 0 | 0 |
| `rules/toilet.rs` | 47 | 10 | 0 | 0 | 0 | 2 | 0 | 0 |
| `rules/psychology.rs` | 47 | 0 | 0 | 3 | 4 | 2 | 1 | 0 |
| `rules/queues.rs` | 43 | 2 | 4 | 0 | 2 | 0 | 0 | 0 |
| `rules/support.rs` | 36 | 7 | 1 | 2 | 1 | 0 | 0 | 0 |
| `rules/information_events.rs` | 28 | 12 | 0 | 1 | 1 | 0 | 0 | 1 |
| `rules/staff.rs` | 30 | 1 | 9 | 0 | 1 | 1 | 0 | 0 |
| `rules/world_view.rs` | 40 | 0 | 0 | 0 | 0 | 2 | 0 | 0 |
| `rules/social_interaction.rs` | 27 | 8 | 1 | 1 | 0 | 0 | 0 | 0 |
| `rules/social_propagation.rs` | 28 | 0 | 0 | 0 | 1 | 2 | 1 | 0 |
| `rules/facility_queues.rs` | 27 | 3 | 1 | 0 | 0 | 0 | 0 | 0 |
| `rules/travel.rs` | 20 | 5 | 2 | 1 | 2 | 1 | 0 | 0 |
| `rules/common_operations.rs` | 17 | 9 | 1 | 0 | 0 | 0 | 0 | 0 |
| `rules/movement_geometry.rs` | 27 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| `rules/staff_dynamics.rs` | 22 | 2 | 0 | 0 | 2 | 0 | 0 | 0 |
| `rules/travel_completion.rs` | 12 | 9 | 3 | 0 | 0 | 2 | 0 | 0 |
| `rules/body_state.rs` | 20 | 1 | 0 | 0 | 4 | 0 | 0 | 0 |

**M は思ったより閉じています（grep で確かめた）。** 選べるモデルを1つずつ Rust と突き合わせました。`psychology`、`social`、`information`、`behavior`、`affect`、`relationship`、`queue`、`staff_operations`、`staff_fatigue`、`care_outcome`、`body_dynamics` は、クラス名か ID で Rust に道があります。**例外は2つです：`zone_*` の8つの式と `DistanceSampledProximityPolicy` は、Rust に道がありません（次）。** `another movement policy` のような残りの M は、実測では Run で届きません（2節）。Scenario の契約が1つしか許さない値を見ているものが多いと思いますが、確かめていません。

**E は手順4の本体です。** 267 か所は文からは有効な状態で届くはずですが、実測で届いたのは一部です（2節）。今は「断る → Python が同じ条件を判定 → 文を出す」です。Python を消すなら、Rust が文を出します（6節）。

### Rust の道がないもの（2レーン込み。読んで確かめた範囲で補正）

実測の表には出ません。断らず、Python を直接呼ぶからです。

| 塊 | もの | 行 | 備考 |
|---|---|--:|---|
| 道の変更 | `UpdatePhase._replan_journeys`、`_replan_staff_support_trip` | 275 | ゲートで1回、7日分で0回。**テストが薄い** |
| 区域の環境 | `zone_environment_view`（`domain/environment.py`）、`ZoneEnvironmentFormula` の8つの式（`policies/registry.py`）、`_zone_environment` | 約 200 | **規則です。** kernel がこれを呼びます（`world_view.rs:406`）。式の意味を変えないので、移すだけです |
| 人の文脈 | `_person_entry`、`PersonContext`、`_misinformation_applies` | 少 | pydantic を通る解析 |
| 近接の抽選 | `DistanceSampledProximityPolicy.sample`、`_sample_proximity_contacts` | 111 | H2 はハンドラーだけ移す。抽選は Python |
| 情報の接触の予定 | `SamplePhase._schedule_information_contact` | 44 | |
| trace | `AgentPsychologyTrace.to_dict`（187）、`AgentPsychologyTrace` ほか | 約 300 | 決定 D1 |
| tick の検証 | `TickValidationMemo`、`validate_after_tick` | 316 + `core/world.py` の一部 | #346 の前は driver の一番大きい入口（571 µs/tick）。**今は変わらない tick を `after_tick.rs` が受け、Python の `begin`、`commit`、`requests_to_check`、`_steady_plan`、`validate` は Plan を作るときと断ったときに動きます** |
| dispatch の部品 | `_dispatch_one_event`、`_sync_movement_after_events`、`_finish_stopped_journey`、`_interrupt_trips_of_event`、`_refresh_space_after_geometry_change`、`_record_applied_outcomes` | 約 100 | `sleep_bed` の枝を含む |
| 提案の部品 | `_select_toilet`、`_select_resource_facility`、`_facility_queue_has_capacity`、`_physically_closed_edges`、`_facility_approach`、`_active_route_edge_loads`、`_record_route_capacity_wait`、`_agent_view_snapshot` | 約 230 | Rust に同じ計算の版があるものを含む（推測） |
| 職員の割り当て | `_record_assigned_support`、`_consider_due_complaints` | 53 | |
| 資源の部品 | `ResourceServiceManager._audit_event`、`_record_internal_event` ほか | 105 | H2 の `handlers.rs` がコールバックで呼ぶ |
| エンジン | `TickEngine.step`、`_run_phases`、`run` | 60 | `step` は Python のままでよい（残す） |

**LLM の境界は移しません：** `_prioritize_support_request`、`_consider_social_contact_event`、`consider_*`。Provider を呼ぶ手前なので、Rust からのコールバックのまま残します。**これは「規則のコールバック」の例外として残ってよいものです。**

**`domain/population_lifecycle.py` の `apply_population_presence_command`（47行）** は kernel の中身（`_apply_presence_action`）があるのに外側がコマンドの解析（pydantic）、写し、検証で、**コマンドの境界です。残してもよい**ものです。

### tick の外から使うもの

`domain`、`events`、`policies` の d は、関数とクラスで 11,347 行です。**うち 10,471 行は tick からも届きます**（a・b の kernel の中の Python 本体が消えたあとは減ります）。

| 種類 | 行 | 例 | どうするか |
|---|--:|---|---|
| `validate_*`、`canonical_*` | 4,393 | `validate_effect_program`（727）、`validate_resource_ledger`、`validate_body_state`、`validate_movement_runtime_state` | **残す**（決定 D3）。読み込みの完全検証と、介入の承認の境界 |
| dataclass、pydantic の契約 | 3,283 | `domain/models.py` の実体（591）、`events/queue.py`（493）、`EffectProgram`、`ActionProposal`、`WorldView` と `AgentView` | 残す。`_PythonEventQueue`、`_PythonEventLog` は消す |
| `create_*`、`initialize_*`、`add_*` | 554 | `initialize_simulator_extensions`、`create_movement_runtime_state` | 残す（世界の組み立て）。`create_population_*` は V の断りを閉じるのに使う |
| その他 | 3,117 | 下 | 下 |

**その他で、規則に近く、気を付けるもの（読んで確かめた）：**
- **`branch.py` の `_apply_to_world`：** `place_installed_facility`（kernel あり）、`staff_position_snapshots`（Python のみ）を直接呼びます。分岐のコマンドは Python に残るので、呼ばれる規則は kernel の薄い入口か Python で残します。
- **`validate_effect_program`：** `validate_staff_operation_order`、`shared_passage_edge_loads`、`validate_geometry_change` を呼びます。承認の前のプレビューで使います。
- **`inspection/frames.py`（`world_frame`）：** `care_history_*`、`current_household_id`、`resource_metric_values`、`support_wait_reason_codes`、`support_waited_minutes`、`effective_geometry` を呼びます。**`support_wait_reason_codes` は kernel が断るときの Python の規則でもあるので、残ります。**
- **`storage/history_index.py`、`index_writer.py`：** `body_state_v1` など5つの名前空間の木を読みます。
- **`experiments/metrics.py`、`comparison.py`：** `asdict(item.needs)` など。
- **`social_dialogue.py`：** `virtual_dialogue_example` は履歴を読むときだけです。

**kernel つきで外からも呼ばれる関数は42個です**。例：`canonical_*_config`、`create_*_state`、`capacity_metrics`、`current_household_id`、`operational_kpi_values`。**Python の名前は残し、本体を kernel の呼び出しだけにします。** ここは消せません。薄くなるだけです。

## 4. kernel から Python を呼ぶところ

2レーン込みで `getattr`・`call_method` は 785 か所、`setattr` は 94 か所、`.import(` は 106 か所です（grep）。

| 種類 | 例 | 木を切り離すと | 手順4で |
|---|---|---|---|
| **データの読み書き** | `.get`、`.items`、`.values`、`.pop`、`.append`、`.update`、`.clear`、`.setdefault`、`.remove`、`.sort`、`.insert`、`.to_dict`。実体の属性の `getattr`、`setattr` | 消える | 行だけで書けば消える。**ほとんどの回数はこれ。** ただし `rules/world_view.rs` 36、`effects.rs` 18、`support.rs` 11 か所が `model_state` を直接見ます |
| **世界の窓口** | `allocate_event_sequence`、`schedule_event`、`unsettled_support_requests`、`pending`、`find`、`contains_id` | 行の版にできる | 依頼の索引が Python の `_tick_memo` にあるのが残り |
| **オブジェクトを作る** | `AgentState`、`NeedState`、`ActiveTask`、`FacilityState`、`StaffState`、`SupportRequestState`、`ScheduledEvent`、`EventLogRecord`、`ToiletRequestReceipt`、`ResourceRequestReceipt`、`AgentStateDelta`、`PsychologyUpdateResult`、`ZoneRoute`、`Point2D`、`ZoneDemandView`、`with_outcome`、`with_stress` | 実体を残すなら残る | Python の読み手が残るので残してよい |
| **契約の検査（pydantic）** | `CareTransitionReceipt`、`FacilityQueueTransitionReceipt`、`NumericStateChange`、`StaffSupportContinuityReceipt`、`DynamicArrivalPersonContext`、`FacilityGeometry`、`SpatialConstraints`、`ScenarioInfrastructureSpec`、`ScenarioZoneEnvironmentConfig`、`InformationSpreadConfig`、`_PARAMETER_MODELS`、`_operation_record_adapter`、`parsed_facility_geometry`、`_parsed_config`、`canonical_body_config`、`canonical_proximity_contact_config`、`selected_models` | 残る | 設計で Python に残すもの。**毎 tick なら費用（`_parsed_config` 32回、`selected_models` 8回/tick）** |
| **写して差し替える** | `WorldState.from_dict`、`to_dict`、`validate`、`validate_simulator_extensions` | 残る | `effects.rs` と `handlers.rs` の介入。`core.fork()` は後でよい |
| **LLM の境界（残す）** | `_prioritize_support_request`、`consider_support_request`、`consider_complaint_response`、`_consider_social_contact_event`、`settle`、`record_support_request` | 残る | **残します** |
| **Python の外の口** | `capture_metric`、`_refresh_space` | 残る | sink は Python |
| **規則（消すなら Rust が要る）** | 下 | 残る | **これが手順4で閉じるもの** |

**規則のコールバック（2レーン込み。grep の結果を読んで分類）：**

- `rules/tick.rs`：`_replan_journeys`、`_record_assigned_support`、`_consider_due_complaints`、`_sample_proximity_contacts`、`_finalize_actions_at_end`、`assign_pending`、`arbitrate`（独立の列）。**`__wrapped__` 経由の Python の規則すべて**（`or_rule!`。約 30 か所）。
- `rules/dispatch.rs`：`_dispatch_one_event`（表にないイベント）、`_sync_movement_after_events`、`_finish_stopped_journey`、`_interrupt_trips_of_event`、`_admit_pending_returns`、`admit_pending_arrivals`、`learn_facility`、`record_applied_action_completion`、`record_applied_population_exit`、`ingest_applied_shared_operation_records`、`_record_request_interrupted`、`_record_staff_support_started`。
- `rules/handlers.rs`（H2）：`ResourceServiceManager` の `cancel`、`interrupt_for_staff`、`record_request_outcome`、`_audit_event`、`_record_internal_event` など11個。`apply_resource_operation`、`apply_arrival_approach_completion`、`queue_arrivals`、`queue_departures`、`apply_population_presence_command`、`admit_pending_returns`、`finish_movement`、`record_shared_operation`、`record_queue_transition`。**多くは kernel_first の Python の入口を呼び、その先で kernel が動く形です。Rust から Rust を直接呼べば消えます。**
- `rules/staff_operation_events.rs`（H2）：`staff_position_snapshots`、`staff_dynamics_is_available`、`staff_dynamics_on_shift`、`create_staff_operations_formula`、`admit_pending_arrivals`。
- `rules/staff.rs`：`interrupt_staff_operation`。`rules/support.rs`：`append_applied_care_transition`、`care_source_ids_for_agent`、`create_care_outcome_formula`、`assign_pending`。
- `rules/travel_completion.rs`：`agent_is_at_queue_slot`、`agent_is_at_queue_trip_destination`、`agent_is_at_facility_service_point`、`complete_presence_relocation`、`_after_arrival`。
- `rules/world_view.rs`：`_person_entry`、`_zone_environment`、`_misinformation_applies`、`applied_facility_event_ids`。
- `rules/social_propagation.rs`：`record_observed_contacts`。
- `rules/population.rs`、`population_lifecycle.rs`：**書いたあとの `__wrapped__` の呼び戻し2か所。** kernel が途中で断ったら Python の規則を呼びます（`m6-population-rules.md`）。**この2か所は Python を消すと壊れます。** 実測の回数は0です（2節）。L1 で、通すテストを書いて、Rust の中で閉じます。

**`Unsupported` の後ろの「Python の規則」が全部消えるとどうなるか：** `kernel_first` と `or_rule!` の受け先がなくなるので、この2つの仕組み自体を消して、Rust のエラーに変えます（6節）。

## 5. 木の切り離し

**今、本番で切り離している名前空間はありません（grep）。** `state_mirror.detach` を呼ぶのはテストだけです（`test_world_core.py`、`test_psychology_row.py`、`test_staff_dynamics_row.py`）。つまり、行を持つ19の名前空間（2レーン込み）のすべてに木があります。行のない V の木は `operational_events_v1`、`infrastructure_v1`、`zone_environment_v1`、`social_runtime_v1`、`support_priority_runtime_v1` です。

**読み手（文字列とキーの定数を grep。外だけ抜き出し）：**

| 名前空間 | tick の外の読み手 |
|---|---|
| `agent_dynamics_v1`、`social_network_v1`、`staff_dynamics_v1`、`movement_v1` | `inspection/frames.py`（`movement_v1` は `contracts/evidence.py` も） |
| `body_state_v1` | `inspection/frames.py`、`storage/history_index.py`、`index_writer.py` |
| `facility_queues_v1`、`staff_operations_v1` | `inspection/frames.py`、`storage/history_index.py`（`staff_operations_v1` は `index_writer.py` も） |
| `geometry_overlay_v1` | `inspection/frames.py`、`llm/natural_language.py` |
| `information_spread_v1` | `inspection/frames.py`、`traces.py`、`llm/runtime.py` |
| `population_dynamics_v1` | `inspection/frames.py`、`llm/natural_language.py`、`contracts/population.py`、`spatial.py` |
| `population_lifecycle_v1` | `storage/history_index.py`、`index_writer.py`、`llm/natural_language.py` |
| `population_presence_v1` | `inspection`、`storage`、`llm/natural_language.py`、`web/project_queries.py`、`contracts/runtime.py`、`presence.py` |
| `resource_ledger_v1`、`resource_service_v1` | `inspection/traces.py`、`llm/natural_language.py` |
| `shared_operations_v1` | `inspection/frames.py`、`contracts/operations.py` |
| `effect_program_v1` | `web/pdf_report.py` |
| `psychological_distress_v1`、`social_propagation_v1`、`temporary_spaces_v1` | 外になし（`core` と `experiments` の読みだけ。**切り離しの最初の候補**） |

**どれも世界全体に対して、さらに次が読みます（読んで確かめた）：**
- `core/world.py` の `to_dict`、`same_state`、`validate` と全部の `validate_*`
- `core/checkpoint_cache.py` の `_WORLD_TREE`、`_with_empty`。**`dict(model_state)` で読むので、切り離したものを毎回作り直します**（`v-saves.md` が記録）
- 残す kernel 自身（上の `model_state` の直読み）

**切り離しに要るもの：**
1. `_with_empty` と `core/world.py` の丸ごとの読みを、`owned_row_bytes` の行の bytes に替える（入口はもうあります）。
2. `_DetachingTable` だと kernel が断る問題を直す（`b-social-propagation.md`。`_table_holds_row_of` はもう `_DETACHED` を受けているので、確かめるところから）。
3. `inspection/frames.py` は1回の読みで再作成を許す。frame は保存ごとに作るので、**再作成が保存の間隔ごとに起きます。** 費用は測っていません。
4. `validate_*` を残すなら、読むたびに再作成か、行から木を作ることになります。

**実体（dataclass）：** `world.agents` 等の参照は、tick の外で次の数です（grep）。

| | web | inspection | llm | `experiments/*.py` | region、storage |
|---|--:|--:|--:|--:|--:|
| `world.agents` | 5 | 8 | 11 | 7 | 0 |
| `world.staff` | 2 | 1 | 3 | 8 | 0 |
| `world.facilities` | 3 | 1 | 2 | 9 | 0 |
| `world.support_requests` | 0 | 2 | 2 | 0 | 0 |
| `world.active_tasks` | 0 | 0 | 0 | 3 | 0 |

使うのは、frame の描画、指標と比較、`natural_language` と `llm/runtime` の文脈、`branch` のコマンド、Scenario からの世界の組み立てです。**どれも読むだけか、コマンドの境界です。**

**提案：切り離しは手順4の必須にしません。** 利用者の決定にある完了の定義は「Python の規則を消す」で、切り離しは速さの最適化です（`b-social-propagation.md` も、切り離しで減るのは毎 tick の書き込み）。**並列に要るのは、並列の計算の段階が行だけを読むことです。** ミラーは `apply` の段階で GIL を取って書けばよいので、木があっても並列にできます（推測。測っていません）。切り離しは最後の別レーンにします。

## 6. エラーの文

| | 数 | 出どころ |
|---|--:|---|
| `src` の全 raise | 3,290 | AST（2レーン込み） |
| `domain`、`events`、`policies`、`experiments`、`core` | 1,824 | |
| **消すコードの中（a + bE + bV + c + u）** | **685** | 文字列 584、f-string 86、式 12、そのほか 3 |
| 残るコードの中（d） | 1,139 | |
| `pytest.raises(match=)` | **459 か所、321 テスト関数**（AST、tip）。grep では 433 | |

**`match=` の正規表現を、raise の文と実際に突き合わせました（tip のテスト × 2レーン込みの src）：**

| 当たる先 | 数 | どうなるか |
|---|--:|---|
| 残る d の文 | 163 | そのまま |
| `web`、`storage` など外の文 | 93 | そのまま |
| 消すコードだけ | 35 | **文を Rust に移す** |
| 消すコードと残るコードの両方 | 66 | 確認する。残る側で通るかもしれない |
| どの raise にも当たらない | 79 | ほとんどが kernel の断りの文（`tainted`、`no row`、`two deltas`）。**kernel の直接のテストで、断りの文が変わると直す** |
| 文字列でない | 23 | 読む |

設計書の「raise 1,373、match 328」は当時の数で、数える範囲も違います。

### バイトまで同じでないといけないもの

**読んで確かめました：**

1. **`EventSuppressed` と `EventApplicationError` の文は保存されます。** `dispatch_one` が `detail = str(exc)` にして EventLog の記録に書きます（`events/handlers.py:157-162` 付近）。ハッシュに入り、画面と根拠に出ます。消すコードの中で `EventApplicationError` 302 か所、`EventSuppressed` 49 か所、計 351 か所です。**今の文のまま移します。** 手順4でハッシュは変わりますが、旧 Run と新 Run で同じ出来事の文が違うと比較と根拠の表示が読みにくいからです。**f-string の float も `python_float_repr` で同じ書き方にします。**
2. **Web の API は `str(exc)` を返します。** `web/server.py:215-223,310,404-409`。公開の形ではありませんが、利用者に見えます。
3. **文で分岐するコードは3か所だけで、どれも残るコードです：** `web/server.py:217,404`（`unknown job_id`）、`:220`（`unknown branch_id`）、`llm/provider.py:317`（`usage_runtime_share_exhausted`）。**消すコードの文で分岐しているコードはありません**（grep の形に合わないものは見つけられません）。
4. **`known_stops` のゲートの Scenario** は `StateInvariantError` の文を結果として固定しています。バイト同じのままにします。

### 変えられないのではなく、変えてよいもの

- **`ValueError`、`StateInvariantError` のうち、壊れた状態でだけ出るもの（C の 1,305 か所の断りの先）。** 保存されず、利用者が普通は見ません。**1つずつ移すのはやめることを勧めます。** 工数の割に意味がありません。

### 移し方の提案

1. **Python が生きている間に移します。** `kernel_first` に比較の道を足します：Rust が文を出したら、Python の規則を写しの上で動かして文を比べます（設計にある「CI で文を比べる」）。Python を消した後では比べられません。これがレーンを Python の削除の前に置く理由です。
2. **`native/src/core/errors.rs` に種類ごとの関数を1つずつ：** `suppressed(text)`、`rejected(text)`、`invariant(text)`、`ledger(text)`。例外のクラスは Python の `events/models.py` などに残し、Rust は import して投げます（クラスは残すコードも使う）。
3. **E の 267 か所は `unsupported("...")` を置き換えます（届かない 181 か所は先にテスト。8節）。** 同じ条件の Python の文を対応させる表を作ります。**条件の順序に注意：** Python が最初に通る検査の文が出るので、Rust の順序が違うと別の文になります。比較の道はこれを見つけるためです。
4. **C の 1,305 か所は1つの出口にします（Run で届く 6 行を除きます。8節）。** `kernel_first` の後継が `Unsupported(reason)` を `StateInvariantError(f"{rule}: {reason}")` にします。**断りの文をそのまま内部エラーの文にします。** 英語で読める文になっています。`match=` の 79 + 必要な分を新しい文に直します。
5. **順序：** 文を移す → 比較が0件 → `kernel_first` を kernel だけにする → Python の本体を消す。

## 7. テスト

**数：** テスト関数は AST で 1,961 個（`m6-driver.md` の全テストは parametrize を含めて 4,121 件成功、`SHELTERSIM_NATIVE=0` では 2,079 件成功、2,048 件 skip）。下の表は関数の数で tip です。**helper や fixture の中の patch は数えていません。** 上が優先の排他的な分け方です。

| 群 | 関数 | 手順4で | 概数 |
|---|--:|---|---|
| `@pytest.mark.python_rules` | 18（20ファイルに印か名前） | **書き直す。** patch で規則の途中を変えて確かめるテスト。`test_save_validation` 7、`test_movement_row` 4、`test_tick_views` 4 が中心。状態を作って kernel に通す形に | 書き直し 10〜15、捨て 3〜8 |
| `__wrapped__` を使う | 67（さらに印との重複2）。`tests` に 100 か所、`benchmarks` に 45 か所 | **比較の片方が消える。** 「Python と Rust が同じ」のテストは、kernel の結果を固定の期待値と比べる形に。`test_effect_kernels` 14、`test_staff_dynamics_row` 12、`test_handler_rules` 10、`test_staff_rules` 7、`test_action_lifecycle_rows` 6 | 書き直し 約 50、捨て 約 15 |
| patch の対象が `sheltersim` | 73（重複を除く） | **ほとんどはそのまま。** LLM、storage、HTTP の patch が大半です（中身を読んだのは一部。lint により、移した規則への patch は上の印つきのはず） | 書き直し 10 未満 |
| c の関数を直接呼ぶ | 52 | c のレーンが kernel を足すなら、名前を残せばそのまま | そのまま 40、直し 10 |
| a、b の関数を Python の名前で呼ぶ | 108 | **薄い入口を残すならそのまま。** Python にしかない引数の形は直す | そのまま 90、直し 20 |
| d だけ | 876 | そのまま。**ただし結果の数値を固定したものは libm と RNG で直す（下）** | そのまま |
| その他（規則を直接呼ばない） | 767 | そのまま。同上 | そのまま |

**横断する群：**

| 群 | 数 | 手順4で |
|---|--:|---|
| **移植専用の29ファイル**（`*_row`、`*_rules`、`*_kernel`、`native`、`parity`、`first_difference`、`tracked`、`world_core`） | 683 関数。`match=` 116 か所 | **残す中心です。** Rust の唯一のテストになります。比較の半分を期待値に直す |
| **`match=` の文** | 上の 35 + 66 + 79 | 6節 |
| `native.available()` の skip、`SHELTERSIM_NATIVE` | テスト 40 ファイル。src 32 か所 | **消す。** Rust の有無で分ける必要がなくなる |
| 数値を固定（`== pytest.approx`、小数3桁以上の `==`） | 98 か所（grep） | **libm、RNG で一部が変わる。** どれが変わるかは動かして分かる。**変わった値は、計算の前後を見て1件ずつ確かめます。自動で書き換えない** |
| 16進のハッシュの文字列を持つテスト | 1 ファイル（`test_initial_scenario`）。**Scenario の作成は Python なので変わらない** | そのまま |
| 固定の期待値の fixture | `fixtures/gate-all-paths.json`（55 Scenario）、`fixtures/regression-contract.json`、`fixtures/demo_replay/`（48 MB）、7日分の `--expect` | **作り直す。** 決定 D4 |
| `test_native_parity.py` | | `exp`、`expm1`、`pow` の CPython との一致は**成り立たなくなる。** 固定のビットのベクトルに替える。`hypot`、`fsum`、`sum`、`round`、`max`、`min` は CPython との比較を残す |
| 消す道具 | `benchmarks/rule_shadow/`（5,877 行）、`tests/support/python_rules.py`、`test_python_rules_lint.py`、`tests/support/tracked_mirror*.py`（1,162 行）、`tracked_mirror_gate.py`、`first_difference.py --both`、ゲートの `python_kernels`、`--coverage` の「なし」の側 | **タグを打ってから消す** |
| 残す道具 | `first_difference.py`（2つのビルドの比較に使える。libm の変更の確認に使う）、`gate_all_paths.py`、`compare_behaviour.py`、`tick_budget.py` | |

**新しく要るテスト：** 一度も届かない E と V の断り（2節）。 閉じる V の枝それぞれ（`_replan_journeys` はゲートで1回しか通らない）。旧モデルの Run の閲覧と、続き・分岐のエラー。`--reversed-order` は今 driver を通らないので、driver を通す形に（`m6-driver.md`）。

## 8. レーン

**前提：** handlers-rest がマージされている。L0〜L8 は結果を変えません（Python と1バイト同じで、今の道具で比べます）。L9、L10 も変えません。L11 から結果が変わります。

### L1〜L8：棚卸しの見積もりから変わったこと（実測）

1. **7日分の断りはほぼ L2 です。** 724 回のうち 718 回が `a free worker can take a request`（712）、`a qualified worker is free`（4）、`a free worker can patrol`（2）です。残りは列を離れる 4、`namespace has no row` 1、`a request closed at once` 1。棚卸しの「`a pending request reports its writes` と `an urgent request may preempt a task` を含めて 2,951 / 712」は分かれました。**どちらも Run では0回で、テストだけです。**
2. **C の 6 行（判定の2行を除く）は壊れた状態ではありません。** `the population state is the Python rule's`（821）、`the world holds another movement state`（92）、`another kind of receipt`（69）などは有効な Run で届きます。L8 の「C は1つの出口」に入れると **Run が止まります。** L3、L4、L7 に移しました。
3. **E は 267 か所の作業ではありません。** Run で届いた 6 行とテストで届いた 56 行は比較でき、181 か所は比べる相手がありません。L8 はテストを書く仕事が増え、文を移す量は同じです。
4. **L7 は小さくなりました。** #346 の後の断りはゲートの 1,296 回の分岐（7日分0回）だけです。
5. **L3 の順序依存の枝は Run で一度も通りません。** `a queue trip must replan` はテスト 3 回、`a person stands in a queue slot` はテスト 2 回です。**棚卸しの「高」は順序ではなく、比べる相手がないことです。**
6. **「数えていない」だった人口の断りは0回でした。** `a new record of all people for one person`、`the population state is made on first use`、`the lifecycle is not installed`、`passage capacities read every trip`、`an action unknown here`、`the toilet's queue at once`、`the toilet's independent queue at once`、`the service's queue at once` はどこでも届きません。消せるとは言えないので、テストを書いてから移します。
7. **`a value is not a str` の 1,781 回は作業ではありません。** 判定です（L1 で例外を作らない形に）。
8. **HT と M は棚卸しのとおりでした。** HT の 6 行はすべて Run で届きます。M は `no long-term state` の 1 行だけが Run で届きます。

### 全体

| 順 | レーン | 中身 | 実測（ゲート / 7日分。テストだけのものは「テスト n」） | 依存 | 並列 | 危なさ |
|---|---|---|---|---|---|---|
| **L0** | 断りの実測 | このレーン | | handlers-rest | なし | 低（済み） |
| **L1** | すぐ閉じるもの | `no long-term state`（kernel も何もしない）。`namespace has no row`（Run の最初の tick の `update_social_propagation`。世界の組み立てで作る）。捨てる判定を例外にしない。**書いた後の `__wrapped__` 2か所は、通すテストを書いてから閉じる。** `handlers.rs` の kernel の直接呼び。`rules/population.rs` の読みを `overlay.rs` に寄せる。**int の係数（D6）は L11 に移しました。L1 では届くテストだけ足します** | 2,680 / 0、65 / 1、1,781 / 0、0 / 0 | L0 | L2〜L5 と | 低 |
| **L2** | 職員 | `assign_pending` の本体、`_support_route_wait_reasons`、`assign_staff_operations` の本体、`relief` の本体、`_preempt_background_staff`、`_record_assigned_support`、`_consider_due_complaints`、辺の容量。`rules/staff.rs` の `for (zone, group) in &people` は必ず並べる。**preempt をゲートに通す Scenario を足す** | `a free worker can take` 3,139 / 712、`a qualified worker` 2,313 / 4（`metrics::capture` の 323 もこれ）、`a staff member has a task` 265 / 0、repair 7 / 0、patrol 0 / 2、edge 3 / 0。テスト：preempt 203、clean 8、reach out 2、pending writes 1 | L0 | L3〜L5 と。`staff.rs` と `proposals.rs` の接点は L3 と調整 | **高。** 7日分のほとんど |
| **L3** | 提案と移動 | その場の `request` 3種、`_replan_journeys`、`_replan_staff_support_trip`、`advance_movement` の順序依存の枝、`sleep_bed`、`agent_is_at_*`、`the world holds another movement state`。**順序依存の枝は、比べるテストを先に足す。今の順序のまま移す** | `no floor plan` 92 / 0、`another movement state` 92 / 0、`service's independent queue` 12 / 0、`a request closed at once` 2 / 1。テスト：queue trip 3、queue slot 2、walk 3、bed 1。0回：`an action unknown here`、toilet と service の `at once` | L0 | L2 と（調整） | **高。** Run で通らない |
| **L4** | 列と資源と人口 | `_abandon_and_retry`、受領記録の再生と別の種類、`_defer_while_busy`、`ResourceServiceManager` の部品、`cancel`、`interrupt_for_staff`、`arbitrate` のループ。`_initial_dynamics` と初めて使うときの状態（**0回。テストから**） | `a recent receipt` 689 / 0、`gives up` 167 / 4、`residents are busy` 108 / 0、`another kind of receipt` 69 / 0、`person is busy` 20 / 0 | L0 | L2、L3、L5 と | 中 |
| **L5** | 読むだけの規則 | `zone_environment_view` と8つの式、`PersonContext`、`_misinformation_applies`、`DistanceSampledProximityPolicy.sample`、`_schedule_information_contact`。担当の方に見てもらう | **断らないので実測に出ません。** 中身は棚卸しのまま | L0 | ほかと | 中 |
| **L6** | trace と Provider | `TraceSink`（D1）、driver が Provider の router を受ける、`PhaseProfiler` と `tick_probe` を `phase_observer` に | `a Provider can be called` 1,800 / 0、trace の tick 840 / 0、`update_psychology` 840、`record_outcome` 696、接触 480、480 | L0 | ほかと | **高**（D1 で大きい） |
| **L7** | tick の検証の残り | `after_tick.rs` が断る枝だけ。`TickValidationMemo` と `validate` の丸ごとの移植は、断りが消えた後に必要な分を見ます。**tick の外の `validate_*` は残します（D3）** | `declared namespace` 646 / 0、`population_dynamics_v1` 348、`staff_operations_v1` 230、共有運用 42、`people changed` 28、ほか 2。`the population state is the Python rule's` 821 / 0 | L2〜L5（読む状態が変わる） | L8 と | 中 |
| **L8** | エラーの文 | 6節。比較の道、`errors.rs`、C の1つの出口。**先にテスト：一度も届かない E 181 か所を、状態を作って届かせる（または届かないと読んで示して消す）。** C の出口に入れてよいか、Run の 6 行を外したことをテストで止める | E のうち Run 6 行（`no longer in a contact zone` 304 / 0 など）、テスト 56 行、0回 181 か所。`a call raised *` 56 / 0 | L2〜L6（E の場所が動く） | L7 と | **高に上げました。** 量が大きく、比較が空振りするところが多い |
| **タグ** | **`rust-identical-final-YYYY-MM-DD`** | 下 |  | L0〜L8 | | |
| **L9** | **旧モデルの閲覧のみ** + 版の仕組み | 下。**版を上げる仕組みだけ入れて、まだ上げない** |  | タグ | L10 の前 | **最高** |
| **L10** | **Python を消す** | `kernel_first` と `or_rule!` を kernel だけに → 全テスト、ゲート、7日分で問題なし → `domain`、`events`、`policies`、`phases` の a、b、u を削除。`segments.py`、`_run_*_phase`、`Python*` のクラス、`SHELTERSIM_NATIVE`、`native.available()`、CI の「Rust なし」の回。**ハッシュは変わらない** |  | L9 | なし（直列） | 高。ただしハッシュが守る。**ファイルをまとめて PR に分ける** |
| **タグ** | **`rust-python-deleted-YYYY-MM-DD`** | | |  | | |
| **L11** | 版を上げる + libm + RNG | **ここで初めて結果が変わる。** 下 |  | L10 | なし | 中 |
| **L12** | 並列 | `compute`、`apply` に分ける。`propose_due`、心理、接触、長期の状態。`RAYON_NUM_THREADS` の変種を全部の Scenario に |  | L11 | なし | 中。**直列とハッシュが同じ** を条件にする |
| **L13** | 3つの OS | 9節。CI の変更 |  | L11（L12 と同時でよい） | L12 と | 中 |
| **L14**（任意） | 木の切り離し | 5節 |  | L10 | L12 と調整 | 低〜中 |

**一番危ないのは L9 です。** 読んで確かめたこと：

- `Checkpoint._legacy_queue_migration_required`（`core/checkpoint.py:184-213`）は、`policy:`、`simulator:`、`state:`、`operator:`、`model_suite` のどれかが違うと `CheckpointError("installed model implementation/state versions differ; explicit migration required")` を出します。`_world_document_for_restore` から呼ばれ、**すべての読み込みの経路（`load_restored_checkpoint`）を通ります。**
- つまり版を上げた瞬間、旧 Run は**閲覧もできず**エラーになります。`web/project_services.py:541-590` の `compatibility` の応答だけはこの失敗を受けて差分を返します。
- 閉じ方を間違えると、次がもっと悪いです：`TimelineStore.iter_worlds`（`experiments/timeline.py:494-534`）は、開始の Checkpoint のあと `OfflineSimulationController` と `TickEngine` で**今のモデルで tick を回します**。`web/replay.py`、`web/project_queries.py:117,338` が使います。**読み込みの検査だけゆるめると、旧 Run の保存の間の分が、新しいモデルで計算され直して黙って表示されます。** AGENTS.md の不変条件に反します。
- `_cache_frame` と `storage/frames.py` の frame のキャッシュは保存の時点の分だけあります。

**L9 の中身：**
1. `load_checkpoint` に「読むだけ」の口を足す。`Checkpoint` の文書と保存済みの frame を読む。版の違いでは失敗しない。**world を返すなら、tick に渡せない型か印にする。**
2. `iter_worlds`、`seek` が旧モデルの Run で保存の時点以外を求めたら、**はっきりしたエラーか、保存の時点に丸めて丸めたことを示す**（D2）。
3. `advance`、`fork`、`submit`、`apply_effect_program`、`web/demo.py:292,1024`、`storage/commands.py` の経路で版を確かめ、**「このRunは旧モデル（版 X）で計算されました。閲覧のみです」のエラーコード**を返す。
4. `model_signatures` に全体の版を1つ足す（`simulator_signatures` の `simulator:engine` のような1項目）。**Scenario ごとの署名を全部変えずに1か所で上げられる。** RNG の `algorithm_id` は別に記録する。
5. **旧モデルの Checkpoint を残したテストの fixture。** `test_checkpoint_versions.py` を足す。AGENTS.md の「古い Checkpoint を保持するか、テスト付きの移行」を満たす。
6. 列の旧方式の移行（`_legacy_queue_migration_required` の後半）、WorldState v5、情報の schema 1 を、**tick の経路から外す**（閲覧では読める）。

**L11 の中身：**
- `libm = "=x.y.z"`（`Cargo.lock` もコミット済み）。`mathx.rs` の `extern "C"` と `black_box` をやめる。`None` を返す場合（`math_1`、非正規化数）は Python がエラーを出していた道なので、Rust のエラーの文を決める。
- **`mathx.rs:463,467` の `mul_add` を `libm::fma` に**（`hypot` の中。下の9節）。
- `clippy.toml` の `reason` を直す。`test_native_parity.py` を固定のベクトルに。
- RNG：`events/handlers.py:462,667,796` 相当（Rust では `information_events.rs`）の `event_id`（連番を含む）をキーから外す。ラベルを `rng.rs` の1か所に。**`experiments/profiles.py`、`region/population.py` の抽選はそのまま**（Scenario が変わるのを避ける。D5）。
- `id_counters["event"]` の並列の問題は L12。
- **確かめ方：** libm だけ、RNG だけ、の2回に分けて `compare_behaviour.py`（100人、2日、10 seed）で KPI の分布の差を出す。**差が seed の揺れの中か**を担当の方に見てもらう。`first_difference.py` で `libm` の前後の最初の違いを見て、期待どおり `exp`、`expm1`、`pow` の下流であることを確かめる。

**削除の前に打つタグ：**
1. **`rust-identical-final-YYYY-MM-DD`：** L8 の後。Python と1バイト同じ最後の点。`first_difference.py --both` 全 Scenario、7日分、全部の shadow の結果を Issue に。
2. **`rust-python-deleted-YYYY-MM-DD`：** L10 の後。ハッシュは 1 と同じ。
3. **`rust-model-v2-YYYY-MM-DD`（名前は任意）：** L11 の後。新しい fixture の基準。

**「1つの変更」について：** 依頼には「手順4は1つの変更」とあります。**作業は上のように分け、`feature/rust-port` 上で L11 と L12 の間に使える状態を出さない（`main` に入れない）ことで、外から見て1つの変更にできます。** 版を上げるのは L11 の1回だけです。libm と RNG は分けて確かめ、同じ日にマージします（D7）。

## 9. 3つの OS の決定性

### libm を固定したあとに成り立つ必要があること

1. **`exp`、`expm1`、`pow` は crate の純 Rust のコード。** `libm` をバイナリに静的に入れる。**同じバージョン（`=`）と `Cargo.lock`。**
2. **LLVM が式を変えない。** Rust は既定で fast-math も FMA の融合もしません。`-C target-cpu=native` はありません（`native/.cargo` がないことを確かめた）。**x86_64（ubuntu、windows）と arm64（`macos-latest`）で同じ IEEE の演算です**（推測。L13 のベクトルで確かめる）。`libm` の `arch` 機能は `sqrt` など正確に丸まるものだけのはずですが、固定するときに crate を読んで確かめます。
3. **`f64::mul_add` が残っています（`mathx.rs:463,467`、`hypot`）。** ハードの FMA がない既定のターゲットでは、プラットフォームの `fma` を呼びます（glibc、UCRT、libSystem）。**正しく丸めていない実装があると OS で変わります。** `libm::fma` にします。`hypot` は約 15 か所から使います。**読んで見つけました。一番静かに残りそうな穴です。**
4. **`c_long` が OS で変わります（読んで確かめた）。** `mathx.rs:262` の `fits_c_long`（Windows は 32 ビット）が `sum_nums` の途中で分岐します。CPython に合わせたものです。**手順4では i64 に固定しないと、int の合計が 2^31 を超える `sum` で Windows だけ違います。** 実際に届くかは確かめていません。`world_core.rs:321` は ID の比較で、切り詰めの問題だけです。
5. **Python に残る float の計算。** libm に依存する Python のコードを AST で探しました。
   - **残るコードの中：** `inspection/frames.py:405-406`（`** 2`、ベッドからの距離の判定。frame は表示用ですが保存のキャッシュと比較に出る）、`contracts/spatial.py:181`（`** 0.5`、検証）、`storage/experiments.py:80`（校正の得点）、`policies/registry.py:194`（`StaffFatigueFormula.fatigue` の `math.exp`。Rust に同じものがあるので、Python の呼び手を確かめて消す）。
   - **`math.exp` 以外は `** 2` か `** 0.5` です。** `x * x` と `math.sqrt` に書き換えれば libm から外れます。**結果が変わり得るので L11 に入れます。**
   - **消すコードの中：** `policies/*` の `exp`、`expm1`、`geometry_overlay.py` の `**`。消えれば問題なし。
   - **`math.hypot`、`math.fsum`、`sum`、`sqrt`、`round`、`json.dumps` は libm に依存しません。** CPython の版にだけ依存します（D12）。ただし CPython の `math.hypot` は C の `fma` を使う可能性があります。残るのは検証と region の幾何です（推測）。
6. **Scenario の作成は Python のままなので、Scenario の JSON は今も3つの OS で同じはずです。** Windows と macOS で確かめたことはありません。

### `--determinism --no-python-kernels` を Windows、macOS で動かすのに要るもの

**今：** オプションはあります（`gate_all_paths.py:288`）。Windows と macOS では小さい1つの Scenario だけを `test_all_paths_gate.py:65` が動かします。**55 Scenario 全部を、Windows、macOS で動かしたことはありません**（`docs/rust-port.md` にも書いてあります）。`--expect` は Linux の CI だけです。

**手順4の後：**
- **`python_kernels` の変種と `--no-python-kernels` を消します。** 比べる先がないので、オプションごと要りません。
- **`--expect` を3つの OS で動かすことが本体です。** `--determinism` は同じ OS の中の決定性しか見ません。**3つの OS で同じことを示すのは、同じ `fixtures/gate-all-paths.json` に3つとも通ることです。**
- **`RAYON_NUM_THREADS` の変種（今は `only_with_saves`）を全部の Scenario に。** L12 の後は tick も並列です。macOS は3コア、Windows は4コアなので、`1` と `4` を明示します。
- **`--reversed-order` を driver を通す形に。** 今は `tick_probe` が入るので Python の経路です。`tick_probe` が消えるなら作り直します。
- **時間。** `--expect` は 16 コアで 86〜114 秒、`--determinism` は 532〜960 秒です（`docs/rust-port.md`）。GitHub の Windows と macOS は 3〜4 コアなので、**`--expect` で約 6〜10 分、`--determinism` で 40〜80 分**の見積もりです（単純な比例の推測）。**PR ごとには `--expect` を3つの OS。`--determinism` は手動か夜間。** 7日分は `long-regression.yml` の手動に3つの OS を足す。**今の `long-regression.yml` は Rust を入れていません**（読んで確かめた）。消した後は動かないので直します。
- **Windows の固有の問題（推測。動かして見る）：** `core/checkpoint.py:349` の `write_text` は改行を変換します。`storage/` の `os.replace` と共有違反（`retry_while_shared` はある）。一時ディレクトリの長いパスと 64 文字のハッシュのファイル名で `MAX_PATH`。`spawn` のプロセスに環境変数が渡ること。ネットワークを塞ぐ処理。
- **`macos-latest` は arm64 です。** ビルドは abi3 で通っています（CI が動いている）。
- **順序：** まず `--expect` を3つで動かす前に、**固定ベクトルのテストを3つの OS で**（`exp`、`expm1`、`pow`、`hypot`、`fsum`、`py_round`、`python_float_repr` のビット）。落ちたら Scenario を回す前に分かります。

### float の書式と hash の順序

**float の書式（読んで確かめた範囲）：**
- 保存の JSON は `ryu` と `python_float_repr`（`native/src/lib.rs`）。OS に依存しない純 Rust です。Python 側の `json.dumps` は CPython の `dtoa`。同じバイトであることは Linux で確かめ済みで、OS に依存する理由はありません（推測）。
- `mathx.rs:400` の `py_round` は `format!` と `parse`。Rust の正確な丸めで OS に依存しません。
- `behavior.rs:674` は Python の `__format__("g")` を呼びます。CPython の `dtoa` です。**Python の規則が消えても、ここが Python を呼ぶ1か所として残ります。** Rust に移すなら `%g` の実装が要ります。
- **f-string のエラーの文の float（6節）。** `format!("{}", f64)` は Python と書き方が違います（`1e16` など）。**`python_float_repr` を必ず通す。** clippy では止まらないので、テストで見ます。

**hash の順序：**
- **Rust の `HashMap`、`HashSet` を順に読むところを grep で探して、怪しいものを読みました。** `std` の `HashMap` は**プロセスごとに乱数**、`FxHashMap` は同じでも**crate の版で変わります。**
  - `rules/staff.rs:1205`：`for (zone, group) in &people`（`FxHashMap`）。**今は断るかどうかだけ**なので順序に影響しません。**L2 で本当に割り当てるようになると順序が結果に出ます。必ず並べる。**
  - `rules/population.rs:2263,2269`：`for message_id in &withdrawn`（`HashSet`）。dict から `pop` するだけで、順序は結果に出ません。
  - `rules/tick_validation.rs`、`state/information.rs`、`state/staff_operations.rs`：判定だけ。最初に見つけたものを返すと文が変わる場所はありますが、断りの文だけです（**C の出口を断りの文にすると、順序で文が変わります。** 並べるか、固定の文にする）。
  - `rules/world_view.rs:607`：取り出した後に並べ替えます。
  - **282 か所の候補を全部は読んでいません。** `clippy::iter_over_hash_type` を `disallowed` の確認に足すことを勧めます。全部の場所が機械的に出ます。
- **`rayon`：** 今は `checkpoint.rs`、`eventlog.rs` の解析だけで、`collect` で順序が保たれます。
- **Python の `set`：** `domain`、`events`、`policies` に作る場所が 467 か所あります（grep。`sorted` で包むものを含む）。`for x in {…}` の形で直接読むのは `core/checkpoint_cache.py:498` だけ見つかりました（中身は読んでいない）。規則が消えると減ります。`PYTHONHASHSEED` の変種を残します。
- **dict の順序：** `IndexMap` と `shift_remove`（clippy が止める）。**`--reversed-order` が driver を通らないのが今の穴です。**
- **実時間と環境：** `native/src` に `Instant`、`SystemTime`、`std::env`、`cfg(target_os)`、`f32` はありません（grep。`rules`、`state`、`core` 全部）。
- **文字列の順序：** Rust のバイト順は Python のコードポイント順と同じです。`casefold` は D11。

## 10. 確かめていないこと

**L0 で確かめていないこと：**

- **Windows と macOS では動かしていません。** 計測も全部の回が Linux（WSL2）です。C API は増やしていませんが、`Unsupported` の型の差し替え（`PyTypeInfo` の実装）は CI で初めて他の OS を通ります。
- **込みの木は公開されていません。** handlers-rest をこのブランチに試しにマージした `b3d59ac` です。衝突は `sitecustomize.py` と `docs/architecture.md` で、`docs/architecture.md` は片方を取りました。この木では計測ありのゲート、7日分、全テストだけを動かしています。`tracked_mirror_gate.py` はどちらの木でも動かしていません。
- **「テスト・tick」と「テスト・直接」は上限です。** テストの壊す書き込みはそのテスト自身のソースしか読みません。fixture や helper の中のものは見えません。「テスト・直接」は引数が壊れていても世界が有効なら入ります。「示せず」は、有効な状態から届かないという証明ではありません。
- **`validate` は `validate_event_history=False` です。** 履歴の監査は確認に入れていません。同じテスト、入口、理由の組では最初の1回だけ確認します。
- **棚卸しの記号との突き合わせは file と文です。** 同じ file に同じ文のか所があると、最も多い記号になります。
- **静的なか所は正規表現です。** `unsupported(`、`refuse(`、`Unsupported::new_err(` を探します。文を引数で受ける共通の部品（`unsupported(reason)`、`after_tick.rs` の作り直し）は1か所に数えます。マクロの本体は、文が別の行で届いていれば届いたことにします。
- **別の Python プロセスの中の回数：** `multiprocessing` の spawn と `atexit` で書きます。終了を待たず kill された worker の分は失います。テストの worker は1テストごとに書きます。テストの環境を替える子プロセス（`PYTHONPATH` を消すもの）は数えません。
- **`m6-validate.md` の断り 1,894 回のうち `resource_service_v1` の 598 回は、今は出ません。** 別の数え方（`after_tick.stats()`）でも 1,296 回で、計測と理由ごとに一致しました。消えた理由は、overlay のマージのあとで変わったと思いますが、追っていません。
- **`the namespace has no row` の名前空間は分かりません。** 文に名前がありません。

**棚卸しから引き継いだ未確認（今も同じ）：**

- 行数の a〜u の分類は AST とコールグラフで、2レーン込みの木のものです。bV は多めです。
- テストの群の「書き直し」「そのまま」の概数は、数を見た上での見積もりで、1件ずつ読んでいません。patch の 73 件は中身を一部しか読んでいません。
- `apply_population_presence_command` など、Python に残してよいと書いたものは棚卸しの判断で、依頼の範囲は利用者の確認が要ります。
- 3つの OS の節の「推測」と書いた所は、動かしていません。
