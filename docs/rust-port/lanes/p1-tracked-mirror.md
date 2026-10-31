# P1：追跡する写しは、すべての書き込みを見ているか

Rust が `model_state` の持ち主になったあと、まだ移していない Python は、入れ子の dict と list を今までどおり読み書きします。設計では `TrackedDict(dict)`・`TrackedList(list)` が書き込みごとに Rust へ転送します。いちばん危ないのは、**転送されない書き込みが黙ってずれを生むこと**です。この作業では、それを純 Python の試作で探しました。

## 結論

- **転送の漏れは、3か所の書き方（下の表）のほかに見つかりませんでした。** 3か所を直したあと、全シナリオと全テストで、追跡する文書と転送から作り直した文書は一度もずれませんでした。
- **方式そのものを否定する結果は出ていません。** ただし、次の3つは設計を変える必要があります。
  1. **書き込みの件数だけでは足りない：** `resource_ledger_v1` は操作ごとに名前空間を丸ごと置き換えるので、7日分の転送量の 92.9% を占めます（1 tick あたり 65,540 値）。
  2. **`deepcopy(world)` は `__setattr__` を通らない：** `storage/projects.py` の `_copy_restored` が通る経路で、素の dict の世界ができます。`WorldCore` にはここの対応が要ります。
  3. **Python の別名と共有は実在する：** 設計の「共有なし」は、調べた範囲が狭かったためです（下の「共有」）。
- **Rust との間は試していません。** 試作の相手は Python の影の文書です。転送の手間、Rust から写しへの書き込み、Rust のカーネルが追跡する型を読む場合は、この結果の外です。

## 直した箇所

どれも、`record = a[k] = {...}` のあとの `record` への書き込みのように、**入れたあとも元の名前で書き換える**ものです。素の dict では同じ結果ですが、追跡する入れ物は入れるときに写すので、元の名前への書き込みは木に届きません。入れたあとに読み直すよう直しました。

| 箇所 | 書いていたもの | モデルのコードか |
|---|---|---|
| `domain/body_state.py` `_record_relationship` | 関係の記録の `trust` など。直さないと10分目でずれる | **はい**（体調の関係。式と係数は変えていません。モデル担当の方に見ていただく箇所） |
| `events/effects.py` `schedule_effect_program` | `program_record["scheduled_event_ids"]` | いいえ（介入の記録） |
| `events/effects.py` `_apply`（設備の稼働数の修飾） | `current["modifiers"]` | いいえ（介入の記録） |

調べて、直す必要がなかった候補：

| 箇所 | 理由 |
|---|---|
| `domain/population.py` `admit_pending_arrivals` の `remaining.append(item)` | `item` は木から読んだ節点で、`remaining` はローカルの素のリストです。入れたあとに書き換えません |
| `domain/population.py` `queue_arrivals` の `_record_waiting_presence(world, item)` | 呼ばれる側は読むだけです。実行時の確認でも見つかりません |
| `domain/psychological_state.py`、`domain/resources.py` の検証、`domain/staff.py` | ローカルの入れ物か、`StaffState` です |

7日分のハッシュは一致しました（world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`）。修正前の同じ commit と比べて、674 個の Checkpoint ファイルはすべて1バイトも違いません。

## 道具

| もの | 役目 |
|---|---|
| `tests/unit/repo/test_tracked_write_lint.py` | tick 側のコードの AST を読み、上の書き方、`dict.__setitem__` などの直接の呼び出し、状態のリストへの `heapq` を見つけると失敗します |
| `tests/support/tracked_mirror.py` | 試作の本体。`TrackedDict`・`TrackedList`、影の文書（`Mirror`）、比較と調査（`Session`・`Census`） |
| `tests/unit/repo/test_tracked_mirror.py` | すべての mutator（`__setitem__`、`__delitem__`、`pop`、`popitem`、`setdefault`、`update`、`clear`、`\|=`、`append`、`extend`、`insert`、`remove`、`sort`、`reverse`、スライス、`+=`、`*=`）、`copy`、`deepcopy`、pickle、C の側の書き込み（`dict.__setitem__`、`list.append`、`heapq`）、素の入れ物を返す処理（`json`、`deepcopy`、`sorted`、スライス）を1つずつ確かめます |
| `tests/integration/test_tracked_mirror.py` | 生成 Scenario の短い実行。ずれを起こすと、分・段階・名前空間が名指しされること |
| `benchmarks/tracked_mirror_gate.py` | シナリオごとに4回実行して比べます（下） |
| `tests/support/tracked_mirror_plugin.py` | 全テストを追跡する入れ物の上で動かす pytest の plugin |

~~~shell
SHELTERSIM_LLM_MODE=offline PYTHONPATH=src:native/pylib:tests python benchmarks/tracked_mirror_gate.py --output 出力先
SHELTERSIM_LLM_MODE=offline PYTHONPATH=src:native/pylib:tests python -m pytest -p support.tracked_mirror_plugin -n 8
~~~

### 確かめ方

1. **素の実行：** 段階の終わりごとに、エンジン・分・段階・名前空間ごとのダイジェストを記録し、census を取ります。
2. **追跡する実行：** 同じダイジェストに一致すること、追跡する文書と転送から作った影の文書が一致すること（段階の終わりごと）。一致しないと分・段階・名前空間を名指しして止まります。素の入れ物を入れたら元のオブジェクトを保持し、あとで変わったら、その入れた行を報告します。
3. **順序（物理）：** 段階の終わりごとに、すべての dict（`model_state`、`world.agents` など）の中身の順を逆にします。
4. **順序（読み取り）：** 追跡する dict の反復を、段階の途中でも常に逆順にします。

最後のハッシュは4回とも一致する必要があります。

## 結果

実行環境の負荷は高く、時間は比較に使えません。件数は 2026-10-31 のものです。

| 実行 | 中身 | 結果 |
|---|---|---|
| ゲートの55シナリオ × 4回（Rust の部品あり） | 追跡する実行で 31,530 tick、220,710 回の段階の終わりを、素の実行と影の文書の両方と比較 | すべて一致。素の実行のハッシュも `fixtures/gate-all-paths.json` と一致 |
| 同（`SHELTERSIM_NATIVE=0`） | 同じシナリオ | すべて一致 |
| 7日分、画面の経路、追跡する実行（比較は1時間に1回） | 影の文書との比較 174 回、書き込み 15,871,236 件 | world・events・KPI のハッシュが `--expect` と一致、ずれ0件 |
| 全テスト（plugin、`-n 8`） | 684 個のエンジン、段階の終わり 627,284 回、書き込み 17,105,806 件 | 1827 件成功、5 件失敗（下）、5 件 skip。ずれ0件 |

`generated_project`（100人・12時間）は、シナリオの中の待ち時間の上限（600秒）に比較の手間で届いたので、上限を外して単独でもう一度実行しました（`benchmarks/tracked_mirror_gate.py` は今は上限を外します）。

シナリオが通るもの：噂とメッセージ、すべての介入と EffectProgram、写して差し替える処理、床図の上書き、人の出入り、分岐と再開、Checkpoint の往復、すべての trace level、記録した応答の再生、選べるすべてのモデル。

**全テストを追跡する入れ物の上で動かした失敗は、次の5件だけで、どれもずれではありません：** 型が厳密に `dict`・`list` のときだけ受け付ける Rust の部品が断るためで、P4 の範囲です。

| テスト | 断る部品 |
|---|---|
| `tests/unit/core/test_checkpoint_cache.py::test_a_history_entry_changed_in_place_is_compared_again_by_the_restore_check` | `native.same_json_items`（`src/sheltersim/native.py` の `type(current) is not list`） |
| `tests/integration/test_save_validation.py::test_unchanged_old_entries_are_not_read_again` | 同上 |
| `tests/integration/test_native_kernels.py::test_the_information_kernel_gives_the_python_state` | `contacts.rs` の `exact_dict` など |
| `tests/integration/test_native_kernels.py::test_the_social_kernel_gives_the_python_result` | 同上 |
| `tests/integration/test_native_kernels.py::test_the_rust_checks_accept_the_state_of_a_real_run` | `validation.rs` の `is_exact_dict` |

Rust の部品は、受け取った入れ物をその場で書き換えません。`native/src` の `set_item` はどれも、新しい dict か `query_pressure.copy()` への書き込みです。P4 で厳密な型の確認をゆるめても、`PyDict_SetItem` が転送を飛ばす経路は今のところありません。新しいカーネルを足すときは、この点を守る必要があります。

## census

### 型

- **整数と小数が混ざる道はありません。** tuple も、文字列以外のキーも、`model_state`・`metric_state`・`model_configs`・`resources`・`id_counters` のどこにもありません（`rng_state` も）。全テストでは、文字列以外のキーは壊した状態を作るテストにだけ現れます。
- 数値の `None` との混在はあります。型付きの行では `Option` が要ります：`social_network_v1.relationships.*.trust`（float か None）、`last_contact_minute`、`body_state_v1.relationships.*.last_contact_minute`、`agent_dynamics_v1.agents.*.last_decision_minute`、`staff_operations_v1.staff.*` の `arrival_minute`・`due_minute`・`started_minute`・`travel_due_minute`。
- 型付きの行の中に、自由な形の値があります：`facility_queues_v1.queues.*.last_stall`（dict か None）、`movement_v1.positions.*.point`（dict か None）。

### 共有

同じオブジェクトが2か所から見える例は、設計の見立てと違って実在します。

| 場所1 | 場所2 |
|---|---|
| `staff_dynamics_v1.config.shift_windows_by_staff` | `model_configs.staff_dynamics.shift_windows_by_staff` |
| `staff_operations_v1.config.{effects_by_zone,task_duration_minutes,thresholds_by_zone}` | `model_configs.staff_operations.*` |
| `geometry_overlay_v1.{facility_points,partitions,temporary_rooms,route_modifiers}.*.source_ids` | `geometry_overlay_v1.history[].source_ids` |
| `population_dynamics_v1.profile_traits.*` | `population_dynamics_v1.{pending_arrivals,departures}[].traits` |

共有したあとに片方へ書く処理はありません（`write_to_shared_node` は0件）。JSON を通した「写して差し替える」処理で共有は消えるので、今のコードも共有に頼ることができません。Rust の行が別々に持っても結果は変わりません。

### 段階をまたいで持たれる別名

| 道 | 持ち主 | 意味 |
|---|---|---|
| `body_state_v1.relationships`、`social_network_v1.relationships`、その記録と `topic_ids` | gc から見えないもの（`watch.rs`）。`SHELTERSIM_NATIVE=0` では消える | P4 と検証の作業で扱う |
| 各履歴の項目（`history[]`、`audit[]`、`request_history[]`、`queue_stall_history[]`） | 別のリスト。`shared_operations_v1` は `TickValidationMemo`。ほかの持ち主は特定していません | 履歴の項目への書き込みは0件 |
| `model_configs.information_spread.correction_source_reliability_by_id` など | `InformationSpreadConfig`、`SocialPropagationParameters` の解析済みの値 | 読むだけ |
| `model_configs.facility_geometry`、`spatial_constraints` | 解析の memo のキー | 同一性で引く memo は、写しの節点の同一性が保たれる必要がある |

「切り離したら書き込みはエラー」にする設計では、これらの持ち主が切り離しのあとに読まないことを確かめる必要があります。切り離された節点への書き込みは、`src/` からは0件、テスト（`tests/integration/test_tick_validation.py`）からだけ5件でした。

### dict の順序

2つの順序の実行は、全シナリオでハッシュと段階ごとのダイジェストが一致しました。結果は dict の順序に依存しません。

ただし、`information_spread_v1` のキーの順は、Rust のカーネルと Python で違います（`SHELTERSIM_NATIVE=0` では違いが出ません）。今でも実行ごとの経路で順序が変わるので、順序に頼るコードは書けません。

### 書き込みの量（7日分、100人、画面の経路）

| 名前空間 | 書き込み（1 tick あたり） | 転送する値（1 tick あたり） | 値の割合 | 1回の値 |
|---|---:|---:|---:|---:|
| `resource_ledger_v1` | 0.8 | 65,540 | 92.9% | 82,323 |
| `body_state_v1` | 769.9 | 923 | 1.3% | 1.2 |
| `social_propagation_v1` | 1.0 | 912 | 1.3% | 912 |
| `movement_v1` | 116.3 | 899 | 1.3% | 7.7 |
| `information_spread_v1` | 1.0 | 809 | 1.1% | 809 |
| `psychological_distress_v1` | 1.0 | 704 | 1.0% | 704 |
| `metric_state` | 506.5 | 507 | 0.7% | 1.0 |
| `social_network_v1` | 61.7 | 67 | 0.1% | 1.1 |
| `agent_dynamics_v1` | 45.5 | 71 | 0.1% | 1.6 |
| `id_counters` | 36.6 | 37 | 0.1% | 1.0 |
| 合計（ほかを含む） | 1,574.5 | 70,532 | | |

値は、転送される入れ物と値の数です。`resource_ledger_v1` は 8,025 回の操作のたびに、監査の全記録を含む名前空間を置き換えます。

### リストの中の節点への書き込み

リストの添字は動くので、パスで転送するとその時点の添字を探す必要があります。これが起きるのは `population_dynamics_v1.arrivals[]` と `pending_arrivals[]` だけで、どちらも小さいリストです。型付きの行には関係しません。

## 設計の表の変更

| 名前空間 | 今の表 | 変更 |
|---|---|---|
| `resource_ledger_v1.audit` | 追記専用の bytes | **このままでは使えません。** 操作ごとに `{**current, "audit": list(current["audit"])}` を作って置き換えるので（`domain/resources.py`）、転送の中身は追記ではありません。置き換えをやめて確認のあとにその場で追記するか、受け取る側で「同じ節点の列に1項目を足したもの」と見分ける必要があります |
| `shared_operations_v1.history`、`body_state_v1.history` など | 追記専用 | 先頭を切り詰める操作（`del history[:-N]`）が要ります。`shared_operations_v1` は追記のたびに呼びます（多くは何も消さない） |
| `population_presence_v1.history` | V | 変更なし。`[*history, x][-20000:]` で毎回全体を置き換えるので、追記専用にはできません |
| `psychological_distress_v1`、`social_propagation_v1`、`information_spread_v1` | 型付きの行 | 型はそのままで構いません。ただし tick ごとに名前空間全体が1回の `set` で来ます。移植するまで、1 tick あたり数百値の転送です |
| `social_network_v1.relationships`、`body_state_v1.relationships`、`agent_dynamics_v1`、`staff_operations_v1` | 型付きの行 | 上の `Option` が要ります |
| `facility_queues_v1` | 型付きの行 | `last_stall` は V |
| `movement_v1` | 型付き（一部 V） | 変更なし。`positions.*` は `set` で1人分がまとめて来ます。`point` は V か `Option` |
| `metric_state` | 表にない | 小数だけなので、型付きの表にできます。転送の件数は `body_state_v1` に次いで多い |

## 試作と lint に見えないもの

**lint：** 関数を1つずつ、行の順に読むだけです。次は見えません（実行時の確認が、実行した経路についてだけ見つけます）。

- 引数で受け取った入れ物に、呼び出し元が入れたあとで書き込む関数
- 属性・グローバル・別の関数のクロージャに持った別名
- あとで入れられるローカルの入れ物を通した別名
- 入れる行より前の行で書き込むループ
- `setattr`、`operator.setitem`、`getattr(d, "update")`、Rust のコード

**試作：**

- 通った経路だけです。ゲートとテストが通らない処理の書き込みは確かめていません。
- Rust は相手にしていません。転送の時間、Rust から写しへの書き込み、型のずれでの taint、切り離し、窓口のクラスは試していません。Rust のカーネルは追跡する型を断って Python に任せるので、「カーネルが追跡する型を読む」組み合わせも動いていません。
- エンティティ（`AgentState` など）は対象外です。census で型だけを数えました。
- 比較は段階の終わりです。段階の途中でずれて戻るものは見えません。ダイジェストは JSON なので tuple とリスト、`1` と `"1"` のキーを区別しません（census と単体テストの厳密な比較が補います）。
- `deepcopy(world)` でできた世界は、次の段階の始めまで素の dict のままで、その間の書き込みは比べません（`plain_root_at_phase_start` として数えます）。
- 順序は逆順だけで、無作為な並べ替えは試していません。読み取りの逆順は Python からの反復だけに効き、Rust の `PyDict_Next` には効きません。
- 7日分の追跡する実行は、比較の間隔を空けています（上の表）。比較の間に起きたずれが次の比較までに消えれば見えません。
- `ProjectAPI` の別プロセスの worker は、`--route project` とゲートの経路では使われません（どちらも同じプロセスのスレッドです）。全テストの plugin は worker も追跡する形で起動します。

## 残る危険

- **`deepcopy(world)`：** `_copy_restored`（`storage/projects.py`）は保存済みの世界の写しを作ります。`__dict__` を直接作るので `WorldState.__setattr__` を通らず、追跡する入れ物の `__deepcopy__` は素の dict を返します。`WorldCore` を入れる P4 で、`WorldState.__deepcopy__` を定義する必要があります。
- **台帳の書き方：** 上の `resource_ledger_v1` を直さないと、P5 以降の転送の手間の計測が、これだけで決まってしまいます。直し方は状態の持ち方に関わるので、モデル担当の方と相談する内容です（式と係数には触れません）。
- **許可の一覧：** `test_tracked_write_lint.py` の一覧はファイル名で書いてあります。P2 でファイルが分かれると（`policies/world_view.py`、`experiments/phases/propose.py`）、同じ変更で直す必要があります。
