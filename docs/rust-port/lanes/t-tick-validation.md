# レーン T：tick の検証

M1 の最後のレーンです。Rust が持つ名前空間について、Python の木を読む古い確認をやめました。**毎 tick の検証は、ほとんど速くなっていません。** 時間の大半は、まだ Rust が持っていない名前空間と実体の規則にあるからです（下の「時間」）。

## やめた確認

| やめたもの | 代わりに証明するもの |
|---|---|
| `psychological_agents_valid`（`validation.rs`） | `state/psychological_distress.rs` の `validate`。行に入らない値は taint、入るが規則に合わない値は `false` で、Python の規則が木を読みます |
| `information_contacts_valid`（`validation.rs`）と、それが読む `_contact_key_is_good`、`_GOOD_CONTACT_KEYS` | `state/information.rs` の `validate` と同上 |
| `unit_values_valid`（`validation.rs`） | 同上 |

この3つは、kernel が断ったあとの Python の規則の中でだけ動いていました。正常な tick では動きません。やめたあとも Python の規則はすべて残っているので、受け入れる範囲とエラーの文は変わりません。taint した世界と `Session(tracked=True)` の回は、Python が全部を読むぶん少し遅くなります。

`watch.rs` は**何もやめていません。** 使っているのは `social_network_v1` と `body_state_v1` だけで、どちらもまだ Rust が持っていません（M3）。

`metric_state` と `social_propagation_v1` には、もともと毎 tick の検証の規則がありません。やめるものもありません。

## 足した確認

レーンの前に、次の書き込みが毎 tick の検証を通っていました（`psychological_distress_v1`、`information_spread_v1`、`staff_dynamics_v1` のすべて。レーンの最初に、テストで確かめました）。行が古いまま残り、kernel がそれを読んで受け入れていたからです。

- `world.__dict__["model_state"] = ...`
- `dict.__setitem__(world.model_state, 名前, ...)`、`dict.pop(world.model_state, 名前)`

`rust_validated`（`core/state_mirror.py` の `_rows_are_what_the_world_holds`）が、世界が今も行の木を持っているか、`IdMap` が行の従う辞書で同じ長さか（`EntityStore.follows`）を確かめます。違えば Python の規則が動きます。`world.__dict__["staff"] = ...` と `dict.pop(world.staff, ...)` は、今は他の Python の規則が同じ文で止めますが、それらが Rust に移ると kernel の古い行が見えるので、ここで一緒に閉じました。

## 書き込みの経路ごとの証明

`tests/integration/test_tick_validation_routes.py` が、次のすべてを確かめます。エラーの文は、kernel をすべて切った Python の規則の文と同じで、文字列でも固定しています。

| 経路 | 例 | 何が止めるか |
|---|---|---|
| 属性 | `agent.stress = 1.5` | 実体の規則（オブジェクトを読みます） |
| 実体の `__dict__`、`object.__setattr__` | `agent.__dict__["stress"] = 1.5` | 同上。**行には届きません** |
| `TrackedDict` | `記録["long_term_distress"] = 2.0`、`update` | 型が合わなければ taint、範囲外なら kernel の `false` |
| 名前空間の丸ごとの書き込み | `world.model_state[名前] = {...}` | `replace_root` の taint か kernel の `false` |
| `model_state` の丸ごとの書き込み | `world.model_state = {...}` | 同上 |
| 世界の `__dict__` | `world.__dict__["model_state"] = {...}` | `rust_validated`（今回） |
| `NamespaceTable` を通らない | `dict.__setitem__(world.model_state, 名前, ...)` | `rust_validated`（今回） |
| `IdMap` を通らない | `dict.pop(world.staff, キー)` | `rust_validated`（今回）と実体の規則 |

`test_native_kernels.py` にあった、やめた kernel の境界値のテスト（`daily_acute_sum` の 3.0000000001、`10**30`、壊れた接触のキーなど）は、同じ値を行の kernel に対して確かめる形で、同じファイルに移しました。

### 証明できないもの

**追跡する木の節点への `dict.__setitem__`（`list.__setitem__` も同じはずです。リストは確かめていません）は、行にも kernel にも届きません。** 毎 tick の検証も、保存のときの完全検証も通ります（完全検証も `rust_validated` を使うためです）。保存される byte 列は行のものなので、値は保存されませんが、Python の規則はその値を読みます。**このレーンで弱くなったのではなく、P5 以降の行の kernel の性質です。** テスト `test_a_write_past_a_tracked_dict_is_seen_only_by_the_python_rules` が今の振る舞いを固定しています。

**判断（コーディネーター）：今は閉じません。** `src/` にこの書き込みはなく、lint（`test_tracked_write_lint.py`）と `verify()` が見つけるからです。手順4で Python の木が規則の入力でなくなると、この穴はなくなります。

`IdMap` の中身を同じ長さのまま `dict.__setitem__` で入れ替えると、`follows` には見えません。実体の規則がオブジェクトを読むので、今はそこで止まります。

## 保存の確認（`owned_row_bytes`）

`world.__dict__["model_state"]` を差し替えた世界の保存が、古い行の byte 列を書いていました。テストで再現しました（書いた `long_term_distress` が 0.123 のとき、保存された値は 0.0）。世界は検証を通るので、何も止めず、状態が黙って失われます。`dict.__setitem__(world.model_state, 名前, ...)` と、`dict.pop(world.support_requests, キー)`（`IdMap` を通らない書き込み）でも同じでした。

`owned_row_bytes` は `rust_validated` と同じ確認（`_table_holds_row_of`、`EntityStore.follows`）を使うようにしました。合わないものは行の byte 列を使わず、Python のオブジェクトから作ります。費用は同一性と長さの比較だけです。`tests/integration/test_capture_rows.py` が確かめます。

**この確認で見えない、同じ種類の書き込み：**

- `agent.__dict__["stress"] = 0.5`（規則に合う値）は行に届かないので、保存は古い値を書きます。
- `dict.__setitem__(world.support_requests, キー, 別のオブジェクト)` は長さが変わらないので、`follows` に見えません。

1つ目は実際に確かめました（世界が 0.4321 を持つとき、保存された値は元の 0.821…。完全検証も復元の確認も通ります）。`test_a_write_to_the_dict_of_an_entity_does_not_reach_a_save` が今の振る舞いを固定しています。2つ目はコードを読んだ判断で、動かしていません。どちらも `src/` にはありません。毎回の保存で実体を全部読めば閉じますが、「保存あたり数回の比較」の費用に収まらないので、していません。

## 残したもの

| 残したもの | やめられない理由 | いつ |
|---|---|---|
| `world_agents_valid` と `core/world.py` の実体の規則（キーと ID、人、設備、列、職員、作業、支援の依頼） | `agent.__dict__` への書き込みは行に届きません。行で判定すると、この経路を検出できなくなります。さらに、作業と支援の依頼の規則はイベントの列と突き合わせます | Python のオブジェクトが行の写しでなくなるとき（M4、M5）。イベントは M2 の E |
| `RecordWatch`（`watch.rs`）、`body_relationships_valid`、`body_agents_valid`、`PairKeys`、`TickValidationMemo` の `body_watch` | `body_state_v1` を Rust が持っていません。`social_network_v1` の分は [m3-social-network.md](m3-social-network.md) でやめました | M3 |
| `agent_dynamics_valid` | `agent_dynamics_v1` | M2 の F |
| `movement_positions_valid` | レーン I で `movement_v1` は Rust の行になりましたが（`state/movement.rs`）、規則は Python のままで、この確認は今も木を読みます | 規則が移る M4 |
| `TickValidationMemo` の `population_*`・`geometry_overlay_v1` の正準 JSON、共有運用、予約 ID、支援の依頼の記憶 | それぞれの名前空間と実体を Rust が持っていません | M3〜M5 |

`agent.__dict__` の書き込みを dict watcher で taint に変える案も考えました。監視するには実体の `__dict__` を作る必要があり、作ると属性の読み取りが遅くなります（`NeedState` の読み取りで 18 ns → 38 ns。load average 約 77 の1回の値で、ループの分を含みます）。P6 の数えた約 12,700 回/tick を掛けると約 250 µs で、書き込みごとの callback も増えます。行で判定して減るのは、`world_agents_valid` の約 50 µs/tick と、イベントと突き合わせない規則の約 150 µs/tick なので、割に合いません。試作はしていません。

## 時間

どれも 2026-11-01、WSL2、16コア。ほかのレーンが同時に動いていました。基準はレーンの開始点 `abcff25` です。

| 毎 tick の検証（`validate_after_tick`） | 基準 | このレーン |
|---|---:|---:|
| 7日分の Run、同時に1回ずつ（load average 約 14〜60）、スレッドの CPU 時間の合計 | 16.17 秒（1.60 ms/tick） | 16.19 秒（1.61 ms/tick） |
| `fork-b1` の 9600分から480 tick、同時に3回 | 1,333、1,385、1,688 µs/tick（中央値 1,385） | 1,353、1,308、1,800 µs/tick（中央値 1,353） |

**差は揺れの中です。** やめた3つは正常な tick で動いていなかったので、速くなる理由がありません。`rust_validated` に足した確認は kernel 3回で合わせて 1 µs ほどの計算ですが、単独では測っていません。

9600分からの480 tick の内訳（負荷の低い回、µs/tick）。Rust が持つ名前空間の検証は全体の 3% です。

| 部分 | 費用 | やめられる時期 |
|---|---:|---|
| `TickValidationMemo.begin`（`population_*`、`geometry_overlay_v1` の正準 JSON ほか） | 約 190 | M3〜M5 |
| `validate_body_state` | 約 210 | M3 |
| `validate_shared_operations_state` | 約 125 | M5 |
| `validate_movement_runtime_state` | 約 85 | M2 の I |
| `validate_agent_dynamics_state` | 約 60 | M2 の F |
| `validate_social_network_state` | 約 55 | M3 |
| `validate_population_dynamics_state` | 約 55 | M3 |
| `TickValidationMemo.commit` | 約 45 | M3〜M5 |
| `world_agents_valid` | 約 40 | M4、M5 |
| `kernel.information_spread_v1` | 約 28 | Rust が持つ分 |
| `kernel.staff_dynamics_v1`、`kernel.psychological_distress_v1` | 約 3、2 | Rust が持つ分 |
| `core/world.py` の実体の規則（人の Rust の確認を除く）、イベント、台帳ほか | 残り | M2 の E、M4、M5 |

この表の「約」は、別々の回の値を丸めたものです。合計は tick の全体と一致しません。やめられる時期は設計の表からの見込みで、まだ確かめたものではありません。


## 確認

| 確認 | 結果 |
|---|---|
| `tick_budget.py`（9600分から480 tick、3回、同時、load average 約 40） | 基準との比 **0.932**（0.932、0.825、0.982）。A/A 1.056（0.980、1.109、1.056）。状態は3つとも一致。この回は最後の修正（名前空間がないときの分岐）の前の commit で、`fork-b1` の世界はこの分岐を通りません |
| `gate_all_paths.py --expect` | 55個すべて一致 |
| `--reversed-order` | 段階の終わりと段階の途中、どちらも55個すべて一致 |
| 7日分のハッシュ | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。`--expect` と一致 |
| `rule_shadow`（ゲート全体、`shadow_tick_validation.py`） | 心理 31,667回、情報 32,201回、職員 35,061回、すべて kernel が受け入れ、断りは0回。そのたびに Python の規則も通り、行と木も一致。比べた値 約 2,359万個、違い0件。壊した写しの確認は 308回で、すべて kernel が断りました |
| `rule_shadow`（7日分） | 心理 10,431回、情報 10,431回、職員 10,434回、断り0回。比べた値 約 1,199万個、違い0件。壊した写しの確認は 64回 |
| 道具の確認（コピーで kernel の範囲を 1.02 に広げ、`rust_validated` の新しい確認を外す） | 心理は Run が `long_term_distress` の文で止まりました（`sitecustomize.py` の検証の影）。情報と職員は「the kernel took a tree stored past the table」を報告 |
| テスト、kernel あり | 3006件通過、6件 skip |
| テスト、`SHELTERSIM_NATIVE=0` | 2002件通過、1010件 skip |
| `tracked_mirror_gate.py --jobs 12` | 220行、問題0件。段階の比較 662,130回 |
| `gate_all_paths.py --coverage`（kernel ありとなしの合計 / kernel ありだけ） | `domain/psychological_state.py` 87% / 25%、`domain/information.py` 72% / 38%、`domain/staff_dynamics.py` 74% / 19%、`core/tick_validation.py` 94% / 94% |
| `ruff`、`cargo clippy` | 問題なし |

レーンの最初の版は、`staff_dynamics_v1` がない世界で kernel に聞かなくなり、`test_staff_dynamics_row.py` の1件が失敗しました。本当にないときは kernel に任せ、残された行があるときだけ断る形に直しました。


## Linux でしか確かめていないこと

`EntityStore.follows` は `PyDict` の取得と長さの比較だけで、stable ABI の中です。Windows と macOS は CI で動きます。ここでは動かしていません。
