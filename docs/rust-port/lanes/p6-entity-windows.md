# P6：世界の実体を Rust の行に持つ

`world.agents`、`facilities`、`staff`、`support_requests`、`active_tasks` の実体を、`native/src/state/entities.rs` の型付きの行に持ちました。**どの規則もまだ Rust に移していません。** 結果は変わりません。以後の作業は、この行を読む規則を書けます。

ファイル名は設計の「window」のままですが、**PyO3 の window にはしませんでした。** 理由は次の節です。

## 判断：window ではなく、Python のオブジェクトを写しにする

設計の案は、実体を PyO3 のクラス（行を覗く window）にするものでした。実物の window を試作して測り、**dataclass のまま `__dict__` を行の写しにして、書き込みだけを転送する形**にしました。

試作の計測（load average 約 21。20万回、ループ分を引く。9回の最小値）：

| 読み取り | dataclass | window（`Py<Store>` と行番号） | 中に行を持つ window（最良の場合） |
|---|---:|---:|---:|
| str | 8.3 ns | 20.6 ns（毎回作ると 21.9 ns） | |
| enum | 9.0 ns | 21.0 ns | 15.1 ns |
| float | 8.9 ns | 23.1 ns | 17.5 ns |
| int | | 25.0 ns | |
| `agent.needs.hunger` | 10.5 ns | 43.6 ns | |

| 書き込み（float） | 1回あたり |
|---|---:|
| dataclass | 11.1 ns |
| window | 21〜42 ns |
| 今の `SupportRequestState` の形（Python の `__setattr__`、転送なし） | 81 ns |
| Python の `__setattr__` から Rust へ転送（試作） | 137 ns |

実際の tick の回数（`fork-b1` の 7日の Run を 9600分から分岐、120 tick、`__getattribute__` と `__setattr__` で数えた）：読み取り 12,749回、書き込み 749回（`psychology.py` の人ごとの4つのニーズと、ストレス・信頼で約 600回）。設計の時点の数は読み取り 24,388回（うち `status` 10,397回）で、その後の作業で減っています。

| 形 | 計算（tick を約 5〜8 ms として） |
|---|---|
| window | 読み取り +12〜33 ns × 12,749〜24,388回 = 0.2〜0.4 ms（3〜8%）。読み取りが見積もりどおりでも、1つの作業の予算（+10%）の大半を使います |
| dataclass を写しにする | 読み取りの増加なし。書き込み 749回 × 約 100 ns = 約 0.08 ms |

**window でも予算を超えるとは言えません**（マイクロベンチは同じオブジェクトを読み続ける最良の場合で、実際はキャッシュのぶんだけ悪くなるはずですが、それは測っていません）。それでも次の理由で写しにしました。

- Python の読み取りが遅くなりません。規則が Rust に移るまで読み取りが残るので、ここは効き続けます。
- 値がそのまま残ります。`agent.stress = 1` の `1` は、合う型の行には入りません。window では、ずれた値を別に持つ仕組みが全項目に要ります。写しなら Python のオブジェクトが持ち続けるので、壊れた状態のテストのエラーの文も、JSON の byte 列も同じです。
- クラスが1つです。window だと `SHELTERSIM_NATIVE=0` の dataclass と、`__init__`・`__post_init__` のエラー、`__eq__`（タプルの比較は NaN を同一性で同じとみなす。読むたびに float を作る window では違う結果になります）、`repr`、`fields`、`pickle` を合わせて2つ持つことになります。
- Rust の規則は、どちらの形でも行を直接読めます。差は Rust が書くときで、写しは1回の `PyObject_GenericSetAttr` が増えます（下の「Rust から書く」）。読み手の Python がいなくなれば、名前空間の切り離しと同じように写しをやめられます。

## 仕組み

- **Python のオブジェクト：** `domain/models.py` の dataclass のままです。`_store`（`EntityStore`）と `_slot` は `__slots__` にあるので、`__dict__`（`vars()`、pydantic、`asdict`）には入りません。読み取りは今までと同じインスタンスの属性です。
- **書き込み：** `_install_rows` が、拡張があるときだけ `__setattr__` を入れます。世界の外（`_store` が `None`）では普通の属性です。世界の中では `store.write(self, 種類, 行番号, 名前, 値)` が属性を入れてから行に入れます。プロパティ（`acute_distress`）は、その setter が `stress` を書くので行に届きます。項目ではない名前は行に関係しません。`del agent.stress` は store を taint します。
- **ID のリスト：** `FacilityState` の4つのリストと `StaffState.work_queue` は `TrackedList` です（`core/state_mirror.py` のものをそのまま使います）。どの書き込みでも、行のリストを読み直します。短いので十分です。
- **`IdMap(dict)`：** 読み取りは dict のままで、kernel も素の dict として読みます（`core/tracked.rs`）。`__setitem__`、`__delitem__`、`pop`、`popitem`、`setdefault`、`update`、`clear`、`|=` が実体を付け外しします。世界の外に出た実体は普通のオブジェクトとして動き続け、入れ直せます。キーの順は dict と同じで、`shift_remove` で保ちます。
- **丸ごとの代入：** `world.support_requests = {...}` は `WorldState.__setattr__` が受けて新しい `IdMap` にします。残る実体は行番号を変えず、残らない実体を外します。古い `IdMap` は素の dict として残ります。
- **写して差し替え：** `from_dict` は新しい実体と新しい store を作り、`world.__dict__.clear()`・`update()` が `_core` と `IdMap` を一緒に動かします。`deepcopy(world)` は素の入れ物に写してから持ち直します。`copy`・`deepcopy`・`pickle` で作った実体は世界の外です（`__getstate__` と、`TrackedList`・`IdMap` の `__reduce_ex__`）。世界が spawn の worker を越えることはありません（世界はもともと pickle できません）が、実体と `IdMap` の pickle はテストしています。
- **`to_dict()`：** `NeedState`、`StaffState`、`ActiveTask`、`ZoneState`、`ZoneEdgeState` に足し、`core/world.py` と `experiments/comparison.py` の `asdict` を置き換えました。結果は `asdict` と同じです（テストで比べます）。`asdict` も動きますが、`TrackedList` の写しを作るので遅いです。
- **参照の循環はありません：** store は実体も世界も持ちません。行は `Send + Sync` です（`assert_rows_are_shareable`）。Rust は実体を世界の `IdMap` から見つけます。

## 合わない値（taint）

型付きの項目は厳密な型だけを受け取ります（`float` に `1`、`int` に `True`、enum に同じ値の str はどれも合いません）。ほかに taint するもの：キーが str でない、クラスが違う（派生クラスも）、同じ実体が2か所にある（別の世界を含む）、世界の項目が mapping でない。

- taint すると store の行を全部捨てます。`with_rows` は `Unsupported` を返すので、Rust の規則は実行されません。Python の規則はオブジェクトを読むので、同じエラーの文になります。
- taint している間も、書き込みと出し入れはオブジェクトに届きます。`store.refit(world)` は全部を読み直し、合えば taint を解きます。`from_dict` の世界は新しい store なので、taint を持ち越しません。**今は誰も `refit` を呼びません**（今のところ呼ぶ規則がありません）。
- 世界から外れたものが、古い行番号で書くことはありません。taint の間に外れた実体やリストも、そのときに外します（テストがあります）。

## 次の作業へ

**Rust で読む：**

~~~rust
let store = entities_of(world)?; // コールバックのあとは読み直す。world は別の store に移っていることがある
let total = EntityStore::with_rows(&store, |rows| {
    let agent = rows.agents.get(id).ok_or_else(|| Unsupported::new_err("no such agent"))?;
    Ok(agent.needs.hunger)
})?;
~~~

`Table` の `iter()` は dict の順、`get` はキーで読みます。`with_rows` の中で Python を動かさないでください（`PyRefMut` を持っています）。

**Rust から書く：** `with_rows` の中で行を変え、外で `write_field(world, "agents", id, "stress", 値)` で属性に書きます。`__setattr__` を通らないので転送は戻りません。例は `add_agent_stress` です。

- **`SupportRequestState` を Rust から書くときは、`_write_listener` に自分で知らせてください。** `write_field` は知らせません。知らせないと、解決済みの要求の検証を省く判断（`core/tick_validation.py`）が誤ります。
- ID のリストを Rust から書くときは、`TrackedList` の中身を C API で書き（`raw_list_append` など）、行も変えます。`write_field` で新しいリストを入れてはいけません（節点になりません）。
- `agent.needs` は行の中の `NeedRow` です。ニーズの項目は `agent.needs` のオブジェクトに書きます。

**そのほか：**

- 行に `write_json` はまだありません。保存を Rust に移す作業が足します（`canonical_json` と同じ byte 列）。今は保存も `_canonical_data` です（`tick_probe.world_parts` も）。
- 文字列は `Arc<str>` です。`core/interner.rs` の `Sym` にしていません。比べる回数が多い規則を移すときに、計測して決めてください。
- `AgentProfile` は凍結されているので、変わるのは `agent.profile = ...` のときだけです。`profile.provenance` の dict の中を直接書くコードは src にありません（書いても行に届きません）。`verify` は違いを見つけます。
- `dataclasses.replace(agent)` は `needs` を共有するので、世界に入れると2か所になって taint します。src にはありません。
- `TrackedList` に変わるので、`facility.queue_agent_ids` を直接イベントの payload に入れると `scheduled_event_checked` が Python の処理に戻ります（P4 の注意と同じ）。今のコードは `list(...)` で写しています。
- 新しい実体は `__setattr__` を通るので作るのが遅くなりました（`ActiveTask` が 434 ns → 1,808 ns）。tick で 2.4 個です。復元では約 1 µs × 実体の数が増えます。

## 計測

世界の中の実体（実物のクラス。load average 約 8。20万回、ループ分を引く）：

| 操作 | 1回あたり |
|---|---:|
| 読み取り enum、float（世界の中でも外でも同じ） | 4〜5 ns |
| `agent.needs.hunger` | 14 ns |
| 書き込み float | 83 ns（世界の外は 99 ns。世界の外は `object.__setattr__` を Python から呼ぶぶん遅い） |
| 書き込み `agent.needs.hunger` | 152 ns |
| `IdMap` の `m[k]` / 素の dict | 15 / 13 ns |
| `IdMap` の `m.get(k)` / 素の dict | 27 / 5 ns |
| 実体の追加と削除 | 412 ns（組で） |

## 結果

2026-10-31、WSL2、16コア。ほかの作業が同時に動いていました（load average 15〜87）。

| 確認 | 結果 |
|---|---|
| tick の予算（`tick_budget.py`、9600分から480 tick、5回、同時実行、スレッドの CPU 時間） | 基準との比 **1.033**（1.033、1.017、1.048、1.046、1.017）。A/A は 0.992（0.987、1.009、1.079、0.984、0.992）。世界の `state_digest` は3つとも一致 |
| `gate_all_paths.py --expect` | 55シナリオ、問題0件。`SHELTERSIM_NATIVE=0` でも同じ |
| 7日の生成 Run（project の経路） | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。期待どおり |
| soak：ゲートの全 tick のあとに `verify` と `tainted` | 31,530 tick すべてで確認。ずれ0件、taint 0件 |

soak は、`TickEngine.step` を包む `sitecustomize.py` を `PYTHONPATH` の先頭に置いて動かしました（本体には入れていません）。taint が0件なので、**ゲートの範囲では、今の規則が書く値はすべて行に合います。** 規則が行を読まないので `Unsupported` は `with_rows` のテスト以外で起きません。

## 確かめていないこと

- **Windows と macOS では動かしていません。** 使った C API（`PyObject_GenericSetAttr`、`PyDict_Next`、`PyDict_GetItemWithError`、`PyList_Append`）はどれも stable ABI で、wheel は abi3-py312 で作れています。
- window の実際の tick の費用は測っていません。計算だけです。
- メモリの増加（行のぶん）は測っていません。
- soak は tick の終わりだけを比べます。tick の途中で行を読む規則を入れるときは、その時点でのずれはここでは見えません。別名の問題（`ids = []; facility.queue_agent_ids = ids; ids.append(x)`）は、行のずれではなく Python の結果の違いになるので、`verify` では見えません。見えるのはゲートのハッシュです。この形は P1 の lint の対象外で、ゲートで違いは出ませんでした。
- ゲートが通らない書き込みの経路は確かめていません。`vars(agent)[...] = ...` は行に届きません（src にはありません。`object.__setattr__` は `test_tracked_write_lint.py` が見ます）。
