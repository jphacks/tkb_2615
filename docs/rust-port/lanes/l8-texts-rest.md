# L8b レーン：イベント以外のエラーの文

手順4で Python の規則を消すと、kernel が `Unsupported` を返して Python の規則に文を出させる道がなくなります。このレーンは、イベントの保存される文（`EventSuppressed`、`EventApplicationError`。L8a）以外のエラー（`ValueError`、`StateInvariantError`、契約のエラー）を、Python があるうちに Rust に移し、Python の規則と比べました。**結果は変わりません（ハッシュは同じ）。係数と式は変えていません。**

## 共通の出口（L8a と共有）

- **`native/src/core/errors.rs` は L8a のファイルです。** 始めたときに L8a のワークツリーにあった版をそのまま写し、後ろに L8b の節を足しました（`value_error`、`type_error`、`invariant`、`StateInvariantError` などの Python のクラス、`relayed`、`corrupt`、比較の `errors_switch`、`errors_compare`、`errors_take`）。名前は重なりません。**マージのときは L8a の最新の版に、この節を足し直してください。**
- `core/kernels.py` の `kernel_first` は、L8a の `_outcome_check`（`ValueError` のとき）と同じ形の `_error_check`（そのほかの移した文）と `_fell_back`（断りの後に Python の規則が出した例外を数える）を持ちます。`segments.py` も同じ2つを見ます。driver の `or_rule!` は Rust の中で比べます。**どれも道具が入れたときだけ動き、普段は例外の道で属性を1回読むだけです。**
- **比べ方：** 移した文を作ると、その場所（`#[track_caller]`）と文を覚えます。`SHELTERSIM_ERROR_COMPARE=DIR`（`benchmarks/rule_shadow/error_compare.py`）のとき、Python の規則に戻る入口は、kernel が移した文を出したら、同じ引数・同じ（変わっていない）状態で Python の規則も動かし、クラスと文が同じかを数えます。kernel の直接のテストは、その場で `__wrapped__` と比べます。
- **断りの棚卸しの追加：** `refusal_census.py` は、断りの後に Python の規則が出した例外（`python_raised`）と、`errors.rs` の文がどこで作られたか（`moved`）も数えます。これで「Python が文を出す断り」を測って選びました（下）。

## 選び方（実測）

このレーンの基準（`rust/l4-queues` の先頭）で、テスト・ゲート・7日分の棚卸しを取り、自分の範囲のファイルで「断りの後に Python の規則がイベント以外の例外を出した」か所を数えました（`speed/lane-l8-texts-rest/base/census.json`）。

| 到達 | か所 | 中身 |
|---|--:|---|
| Run（ゲート） | 1 | `overlay.rs` の `validate_geometry_change`（`the Python rule refuses the change`。52 か所の `refuse()` が1つの補助関数を通る）。ゲートで5回 |
| テスト（有効な世界） | 21 | `resources.rs` 14、`service.rs` 3、`staff.rs` 2、`toilet.rs` 1 ほか |
| テスト（壊れた状態・設定） | 34 | `population.rs` 11（状態の検証）、`staff_dynamics.rs` 14（設定）、`queues.rs` 5（設定）、`metrics.rs`、`movement.rs`、`toilet.rs` |

7日分ではどの断りも作られませんでした（0回）。

## 移したもの

| 規則 | 場所 | Python の raise | 移した | 比べたもの |
|---|---|--:|--:|---|
| `validate_geometry_change` と補助（`_validate_partition_polygon`、`_validate_room_polygon`、`_validate_facility_room_clearance`、`_require_service_staff_in_zone`） | `rules/overlay.rs` の `check_change` | 69 | **69**。契約の `model_validate` のエラーは同じ呼び出しのものをそのまま出します（`relayed`） | 新しい掃引のテスト `test_every_refusal_of_a_change_is_the_python_rule_s_error`（10 の世界 × 1,802 の変更。異なる文 52 種類で一致：Python の文の型 37、そのうち `staff_unavailable` は区域ごとに 12、契約のエラー 4。テストは 45 種類を下限にしています）、`test_each_change_…`、`test_a_wall_s_clearance_…`、比較の道 |
| `canonical_staff_dynamics_config` | `rules/staff_dynamics.rs` の `canonical_config` | 11 | **10**（Python の検査の順に書き直しました） | `test_the_config_kernel_gives_the_python_result…`（59 の設定）、比較の道 |
| `validate_staff_shift_windows_override` | 同 `validate_override` | 6 | **2**（数と 16 KiB） | `test_the_override_kernel_…`、`test_the_override_kernel_counts_the_json_limit_to_the_byte` |
| `NeedWeightedPhysicalQueuePolicy.__init__`、`StrictFIFOQueuePolicy.__init__`、`selected_models` のエラー | `rules/queues.rs` の `policy_of` | 7 | **7** と `selected_models` の中継 | `test_a_config_python_turns_down_raises_the_python_rule_s_error`（17 の設定） |

**合計：Python の raise 93 か所のうち 88 か所を Rust の文にしました。** 型が Python の受けるものかもしれない値（`schema_version: 1.0`、dict の派生型など）は今までどおり `Unsupported` です。そこで Python の規則が先の検査で別の文を出す場合も、Python が決めます。

**Rust の側で数えると：** 文を出すか所は 91（overlay 68、staff_dynamics 14、queues 9。中継を含む）。全テストで届いたのは 66 か所で、**66 か所すべてが Python の規則と比べるテストの中で届きます**（掃引、設定と override、列の設定、`test_each_change_…`、`test_a_wall_s_clearance_…`。`speed/lane-l8-texts-rest/after/census.json` の `moved` とテストの名前で突き合わせ）。届かない 25 か所は、overlay の足跡・障害物・壁・予約されたベッド・凹んだ区域の文と、窓や区間の型の組み合わせです。

**比較の道（`tests-compare.log`、全テスト）：** 比べた 74 回のうち同じ 74、違い 0、Python が例外を出さなかった 0。27 か所（overlay 16、staff_dynamics 11）。**ゲート**の比較の数は下の「確認」にあります。

**感度：** 写しの木で3つの文を1文字ずつ変えると（`overlay.rs`、`queues.rs`、`staff_dynamics.rs` の各1つ）、比べるテスト 5 件が失敗し、比較の道も `differs` を3回数えて終了コード1でした（`speed/lane-l8-texts-rest/sens-tests.log`、`sens-cmp.txt`）。

## C の群（壊れた状態）

- **Run で届く C の 6 行（L0）：** `the population state is the Python rule's`、`the world holds another movement state`、`another kind of receipt`、`the sender's belief changed` はこの木のゲートと7日分で **0回**（閉じています）。**`a marker's guard the Python rule decides`（`effects.rs`、ゲート2回）と `the units are for the Python rule to judge`（`handlers.rs`、ゲート1回）はまだ届きます。** どちらも Python が文（`ValueError`、`EventApplicationError`）を出すので C ではなく E で、L8a のファイルです。
- **1つの出口：** `errors::corrupt(namespace, check)` を作りました（`StateInvariantError("{namespace}: corrupt state: {check}")`）。**このレーンでは、どのか所もこの出口に移していません（0 か所）。** 理由：
  1. 計画6節の4のとおり、出口は手順4で `kernel_first` と `or_rule!` の後継が、残った `Unsupported(reason)` をまとめて変える1か所が自然です。今か所ごとに変えても、Python が生きている間は比べる相手がなく（文が違うので）、届かないか所では何も確かめられません。
  2. 棚卸しで C のか所に届くテストは、多くが「Python の規則が受けて正常に返る」ものでした（`python_raised` が `caught` より少ない）。今 `StateInvariantError` にすると、それらの Run とテストが止まります。どの値が本当に壊れた状態かは、か所ごとに読まないと決められません。
- **`match=` で文を固定している C：** `population.rs` の `validate_population_dynamics_state` の文（`population_dynamics_v1 has an invalid exact schema` など）はテストの `match=` にありません。この検証は tick の外の `validate_*` で、決定 D3 で Python に残すので、移しません。`movement_v1 has an invalid exact schema` は `match=` がありますが Python の検証の文で、L7 の範囲です。

## 残したもの（手順4で要るもの）

| もの | 理由 |
|---|---|
| `validate_staff_shift_windows_override` の4つ（不正な ID、知らない ID、21 を超える区間、区間の形） | Python は 16 KiB の検査を先にしますが、Rust はそれを、ID と区間が正しいときしか数えられません。文を出す前に長さを JSON の書き方で数える必要があります |
| `canonical_staff_dynamics_config` の `shift_windows_by_staff must be an object` | 窓が dict でないとき Rust は手前で `Unsupported` |
| `resources.rs` の `ResourceLedgerError` 14 か所 | 断りの後に Python が例外を出さず正常に返る場合が多く（例：`an unknown reservation` は 523 回のうち例外は数回）、条件が Python と1対1でありません。読んで分けるのはこのレーンの残り時間に入りませんでした |
| `service.rs` 3、`staff.rs` 2、`toilet.rs` 2 の `ValueError`、`StateInvariantError` | 同上。有効な世界から届く（テスト・直接）ので次に移す候補です |
| `metrics.rs` の `ZeroDivisionError`、`movement.rs`、`staff.rs`、`staff_dynamics.rs` の `KeyError` | 壊れた状態の Python の例外で、C の出口の候補です |
| 一度も届かない 1,000 を超える `Unsupported`（`step4-refusal-sites.md`） | 手つかずです。到達させるテストは書いていません |

## 確認

`speed/lane-l8-texts-rest/checks.out`（コード `e492adf`）。負荷の高い機械で、ほかのレーンと同時に動かしました。

| 確認 | 結果 |
|---|---|
| ゲート `--expect` | 55 シナリオ、問題 0 |
| ゲート `--reversed-order --expect` | 55 シナリオ、問題 0 |
| 比較の道ありのゲート `--expect` | 55 シナリオ、問題 0。比較 5 回すべて同じ（overlay 2 か所） |
| 7日分 `--expect` | world `69aba6590f6b`、KPI `94c8cbb26041`、events も一致 |
| `first_difference.py --both`（driver の経路） | 55 シナリオすべて違いなし。220,892 段階で digest が同じ |
| 棚卸し（後） | 7日分の断り 0、ゲートの driver の断り 0。自分の範囲で Run に残る断りは `service.rs` の `no active task`（`EventSuppressed`、L8a）だけ |
| ruff、ruff format、clippy（`disallowed_methods`） | 問題なし |

- **ハッシュ：** 上の表のとおり変わりません。
- **全テスト：** Rust あり 4,654 件成功、1 件失敗（`test_refusal_census.py::test_a_run_of_the_driver_shows_its_fallbacks`。基準の木でも失敗します。生成の Run に driver の断りがもうないためで、#353 で直されます）、8 件 skip。`SHELTERSIM_NATIVE=0` 2,079 件成功、2,584 件 skip。
- `tracked_mirror_gate.py` は動かしていません（依頼のとおり）。
- **Linux（WSL2）だけで確かめました。** C API は増やしていません（`PyTypeInfo` を `Unsupported` と同じ形で `StateInvariantError` などに実装）。
