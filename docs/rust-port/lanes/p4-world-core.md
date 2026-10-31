# P4：`WorldCore` と、名前空間を Rust に移す手順

`world.__dict__["_core"]`（`WorldCore`）と、本番の追跡する入れ物（`core/state_mirror.py`）を入れました。**この作業では、どの名前空間も Rust に移していません。** 動作もハッシュも変わりません。以後の作業は、この文書の手順で名前空間を移し、`core/world.py` を編集しません。

## できたもの

| もの | 役目 |
|---|---|
| `core/state_mirror.py` | `TrackedDict`・`TrackedList`（書き込みを sink に伝える）、`NamespaceTable`（名前空間を持つときの `model_state`）、`OWNED_NAMESPACES`、`register_rust_validator`・`rust_validated`、`detach` |
| `native/src/core/world_core.rs` | `WorldCore`、名前空間ごとの `NamespaceMirror`（行・節点の表・taint）、`Row` トレイト、何でも入る `ValueRow` |
| `native/src/core/tracked.rs` | kernel が C API で読んでよい dict・list の型。素の型と、上の3つの型だけ |
| `native/src/state/sample_namespace.rs` | 名前空間のひな形。テストだけが使います。`ROW`（型付き）、`VALUE_ROW`（`V`） |
| `experiments/phases/segments.py` | 段階の手順を Rust に差し替える表 `SEGMENTS` |
| `benchmarks/tick_budget.py` | 作業ごとの tick の予算の確認（下） |

`SHELTERSIM_NATIVE=0` では `_core` を作らず、`model_state` は素の dict、kernel は呼ばれません。何も移していない間は、拡張があっても `model_state` は素の dict で、tick の中で動く追加の処理は検証の箇所の `rust_validated`（登録がなければ1回の判定だけ）と `WorldState.__setattr__` だけです。

## 仕組み

- **Python からの書き込み：** `d[k] = x` → `TrackedDict.__setitem__` → `sink.dict_set(d, 節点番号, k, x)`。Rust が値を変換して木に入れ（`PyDict_SetItem`）、古い節点を外し、経路を節点の表から求めて `Row::apply` に渡します。
- **Rust からの書き込み：** 行を変えてから `NamespaceMirror::write_through` で木に書きます。C API なので上書きしたメソッドは動かず、転送も起きません。
- **節点の置き場所は1つです。** 素の入れ物は入れるときに節点へ変換します。木から外れた節点（`pop` の戻り値、置き換えられた古い値）は `_sink` が `None` の普通の入れ物になり、そのまま入れ直せます。すでに木の中にある節点を別の場所へ入れると写しを作ります。P1 の調査のとおり、共有のあとに書くコードはなく、JSON の「写して差し替える」で共有は消えるので、結果を変えません。
- **合わない値（taint）：** `Row::apply` が `Misfit` を返すと行を捨てます。以後 kernel は `Unsupported` を返し、Python の規則が木の上で動くので、壊れた状態のテストは同じエラーで失敗します。文書全体が合わないときは、最初から taint された状態で木を持ちます。木は常に正しいので、`mirror.refit()` で行を作り直せます。写して差し替えると、新しい世界の `WorldCore` で読み直します。
- **切り離し：** `state_mirror.detach(world, name)` は木を捨てて行だけにします。`model_state` のキーは残り、値を読むと `_rebuild` で木を作り直します。その間 `model_state` は `_DetachingTable` で、kernel はこの型を断るので C API から読まれません。前から持っていた節点への書き込みは `DetachedNodeError` になり、読むと切り離す前の値が返ります。P1 の調査では、節点を持ち続けるものに `watch.rs`、`TickValidationMemo`、解析済みの設定があります。切り離す作業は、これらが切り離しのあとに読まないことを確かめてください。
- **世界の差し替え：** `WorldState.from_dict` は新しい `_core` を作り、`world.__dict__.clear()`・`update()` がそれを運びます。`WorldState.__deepcopy__` は中身を素の入れ物として写してから、新しい `_core` の持ち物にします。`world.model_state = {...}` は `WorldState.__setattr__` が受け、新しい文書を持ち直します。世界の pickle はもともとできません（`EventStore`）。
- **スレッド・プロセス：** 世界ごとの状態はすべて `_core` にあります。モジュールの大域変数は型と「何を移したか」の表だけで、worker のプロセスも同じ内容を import で持ちます。`PyRefMut` は表か行に触る短い間だけで、Python の呼び出しをまたぎません。kernel は Python を呼んだあと `core_of(world)`・`mirror_of(world, name)` で読み直します（`set_level_after` が例です）。

## 転送の方式の判断

P3 の提案のうち、**Python の上書きから Rust の sink へ節点番号で転送する形**にしました。C のスロットを持つ dict の派生型（PEP 697）は使いません。

計測（load average 0.3、20万回、ループ分を引く。`d["level"] = x`、深さ3）：

| 書き込み | 1回あたり |
|---|---:|
| 素の dict | 8 ns |
| 木の外の `TrackedDict`（sink なし） | 60 ns |
| 1項目、型付きの行 | 125 ns（P3 の試作の経路タプルは 130〜821 ns、PEP 697 は 21〜168 ns） |
| 1項目、`V` の行 | 151 ns |
| 記録を丸ごと（3項目、変換と読み取りを Rust で） | 511 ns（型付き）、616 ns（`V`）（P3 の Python の変換は 2,160 ns 以上） |
| list への追加（小数） | 110 ns |
| list への追加（6項目の記録） | 1,129 ns（型付き）、963 ns（`V`） |

止める基準の 3 µs を十分に下回り、記録の変換を Rust に移したことで、P3 が次の費用に挙げた丸ごとの挿入も約4分の1になりました。転送を PEP 697 にして減る分は 1 tick あたり約 0.1 ms（書き込み 1,061回 × 約 100 ns）です。今の tick は約 5 ms なので約 2% に当たりますが、`d[k] = x` にしか効かず、確かめたのが Linux の Python 3.12 だけです。全 OS の CI の前に入れる理由にはなりません。予算が足りなくなった作業が、そのときに見直してください。

## 名前空間を移す手順

`sample_namespace.rs` を写して始めます。

1. **行を書く：** `native/src/state/<name>.rs` に行の型と `Row` の実装を書きます。ファイルを置くだけで `native.state("<name>")` に現れます（`lib.rs` は編集しません）。
   - 型付きの項目は厳密な型だけを受け取ります（`float` の項目に `1` は `Misfit`）。`bool` は `int` の派生型なので先に断ります。
   - `apply` は Python のコードを動かしません。途中で失敗するときは、行が捨てられるので、半端な変更は見えません。
   - `write_json` は `canonical_json` と同じ byte 列を書きます（キーは byte 順）。`to_py` は木と同じキーの順で作ります。
   - `shift_remove` を使い、`swap_remove` は禁止です。小数の計算は `core::mathx` を通します。
   - `register(m)` で `m.add("ROW", Py::new(py, RowType::new("<name>", load))?)` します。自由な形の名前空間は `load_value_row` をそのまま使えます。
2. **持ち主を宣言する：** `core/state_mirror.py` の `OWNED_NAMESPACES` に `"<namespace>_v1": ("<name>", "ROW")` を1行足します。名前空間を丸ごと書くコード（`model_state[ns] = {...}`）は、この変更で一緒に移します（`stats()["whole_writes"]` で数えられます）。同じ値の書き戻しは何もしないので、残して構いません。
3. **kernel を書く：** `native/src/rules/<name>.rs` に。入口は `mirror_of(world, NAME)?` と `NamespaceMirror::with_row::<Row, _>(&mirror, |row| ...)`、木へは `write_through` です。taint・不在の名前空間では `with_row` が `Unsupported` を返します。Python 側は `native.rules("<name>")` を呼び、`native.Unsupported` を受けて今のコードを実行します。
4. **段階の手順を差し替える（機械的にできるとき）：** `experiments/phases/segments.py` の `SEGMENTS` に `"_step_method": ("<name>", "kernel")` を足します。kernel は `(controller, *手順の引数)` を受け取り、何かを変える前に `Unsupported` を出せば Python の手順が動きます。trace があるときは `Unsupported` にしてください。
5. **検証を任せる：** 名前空間のモジュールで `register_rust_validator("<namespace>_v1", native.state("<name>").validate)` します。`kernel(world) is True` のときだけ Python の検証を省きます。エラーの文は持たず、false か `Unsupported` で Python の規則が同じ文を出します。検証の箇所は `core/world.py`・`domain/simulator_state.py`・`contracts/runtime.py` にすでにあります。`TickValidationMemo`・`SaveValidationMemo` を更新する検証を省くときは、その memo を読むコードも確かめてください。

   この登録で省けないもの：タスクの予約の照合（`core/world.py` の、`resource_ledger_v1`・`resource_service_v1` を読む人とタスクの確認）は、人とタスクの規則の一部です。この2つを切り離すと、ここで読むたびに作り直します。
6. **kernel が状態を読むとき：** `downcast_exact::<PyDict>` ではなく `as_readable_dict`・`as_readable_list` を使います。kernel の中で受け取った入れ物を `set_item` で書き換えてはいけません（転送を飛ばします）。写しか新しい入れ物に書きます。
7. **テスト：** `tests/integration/test_world_core.py` を手本に、転送、Rust からの書き込み、taint、`to_dict`・`from_dict`、写して差し替え、`deepcopy`、`same_state`、`canonical_json` を確かめます。全シナリオでは下の soak が使えます。
8. **確認：** `benchmarks/gate_all_paths.py --expect`、7日分のハッシュ、両モードの全テスト、ダイジェストの差分（`benchmarks/first_difference.py`）、予算の確認（次）。

## 予算の確認

~~~shell
SHELTERSIM_LLM_MODE=offline python benchmarks/tick_budget.py \
  --base 基準のツリー --store 保存済みのRunの保存先 --output 出力先 \
  --fork-minute 9600 --child-prefix 自分の接頭辞- --ticks 240 --rounds 3
~~~

- 基準のツリーは、`git archive 基準の commit | tar -x -C 別の場所` で作り、そこで `native/pylib` を build します（`native/README.md`）。今のツリーも同じように build してあること。
- 1回ごとに、基準を2つと作業のツリーを1つ**同時に**動かします。同じ分岐点から同じ分だけ進め、`TickEngine.step` のスレッドの CPU 時間を比べます。2つ目の基準が A/A の対照で、その比が比較そのものの誤差です。最後に世界の `state_digest` が3つとも一致することも確かめます。
- 判定は、作業のツリー ÷ 基準2つの平均の中央値が `--limit`（1.10）以下か。終了コードは 0 が範囲内、1 が超過、2 が実行の失敗か状態の不一致です。超えたら、マージの前に理由を報告します。
- 1回の作業で +10% まで、ある時点の写しの合計で +25% までです。A/A の揺れより小さい差は言えません。
- 保存済みの Run の長さを超えては進められません。9600分から分岐する 7日分の Run なら `--ticks` は 480 までです。
- 子の Branch は保存先に残ります（1つ数十 KB。Checkpoint は親と共有します）。ID は `--child-prefix` で始まります。
- 背景の索引のプロセスがこのスクリプトを読み直すので、入口は `if __name__ == "__main__":` の中です。

## soak：すべての名前空間を `ValueRow` で持つ

実際の規則・kernel・保存を、Rust の行と一緒に動かす確認です。`OWNED_NAMESPACES` を起動時に埋める `sitecustomize.py` を `PYTHONPATH` の先頭に置き、`TickEngine.step` のあとに一定の間隔で `mirror.verify()`（行と木の正準 JSON の比較）と `mirror.tainted` を確かめ、`gate_all_paths.py --expect` を実行しました。使った内容は本体に入れていません（本番には環境変数を増やしません）。

結果：26個の名前を持たせ（実在しない名前は空のまま）、ゲートの55シナリオすべてでハッシュが `fixtures/gate-all-paths.json` と一致し、taint もずれも0件でした（5分ごとに比較）。介入と分岐・再開の2シナリオは毎分比較して、同じく0件です。写して差し替える処理、Checkpoint の往復、人の出入りを、Rust の行と本物の kernel の組み合わせで通したのはこれが初めてです。**`ValueRow` なので、型付きの行の合わない値はここでは確かめていません。**

## 結果

2026-10-31、WSL2、16コア。

| 確認 | 結果 |
|---|---|
| 何も移さない場合の tick（`tick_budget.py`、9600分から480 tick、5回、同時実行、スレッドの CPU 時間） | 基準との比 **1.000**（1.039、0.977、0.993、1.000、1.000）。A/A は 0.983（0.985、0.955、0.983、0.976、0.998）。A/A の揺れの範囲で、差は見えません。世界の `state_digest` は3つとも一致 |
| 参考：3つの名前空間を `ValueRow` で持ち、規則は移さない場合（`body_state_v1`、`movement_v1`、`social_network_v1`、240 tick、3回） | 基準との比 **1.170**（1.166、1.170、1.255）。A/A は 1.024。状態は一致。**1つの作業の予算 +10% を超えます。** 内訳は調べていません（下） |
| `benchmarks/tracked_mirror_gate.py`（55シナリオ × 4回、kernel が追跡する型を読む形で） | 220行、問題0件 |
| P1 の plugin の5件の失敗 | すべて解消 |

**次の作業へ：** 上の +17% は、写すだけで規則を移さないときの費用です。転送だけでは説明がつきません（書き込み約 970回 × 約 150 ns で約 0.15 ms、tick 約 5 ms の 3%）。ほかの候補は、インタープリターの dict 専用の高速化が効かないことによる読み取り（`d[k]` は 13 → 20 ns）と、節点を含む値を断る kernel（下）ですが、確かめていません。名前空間を移す作業は、規則も同じ変更で移して予算を測ってください。

## 次の作業が知っておくこと

- **追跡する型を読まない kernel：** `checkpoint.rs` の `index_row`・`record_json`（`records.rs`）・`scheduled_event_checked` は素の型だけを受け取ります。追跡する節点を含む値は `None` になり、Python の処理に戻ります。結果は変わりませんが、**イベントの payload や索引の行に節点を入れるコードがあると遅くなります**。P5 で移した名前空間を確かめてください。
- **`canonical_json` の動き：** 今までは dict・list の任意の派生型を読みましたが、追跡する型だけになりました。`Counter`、`defaultdict` などは `json` の処理に戻ります（同じ byte 列で、遅くなるだけ）。
- **`V::from_py` も同じ：** 任意の派生型は `Unsupported` です。
- **テストの影の文書（`support/tracked_mirror.py`）の節点は、外れても sink を持ち続けます。** 外れた節点への書き込みを見つけるためです。本番と違うのはここと、共有を写さないことです（共有への書き込みは `write_to_shared_node` として報告します）。
- **`ReverseReadDict`（順序の実行）は kernel が断ります。** 読み取りの逆順の実行では Python の処理に戻ります。
- **`dict.__setitem__(node, ...)` を `src/` に書くと転送を飛ばします。** `tests/unit/repo/test_tracked_write_lint.py` が見つけます。`core/state_mirror.py` だけが対象外です。

## 危ないところ

- **`node_number_is`** はリストの中を番号で探すので、リストの節点の中の節点への書き込みは O(リストの長さ) です。P1 の census では `population_dynamics_v1.arrivals[]`・`pending_arrivals[]` だけで、どちらも短いリストです。
- **別名：** 入れたあとに元の名前で書くと届きません（P1 の lint と `support/tracked_mirror.py` が見つけますが、通った経路だけです）。`d[k2] = d[k]` のあとで `d[k]` を消すと、`d[k]` の元の名前で書いた値は `d[k2]` に届きません（写しなので）。素の dict では届きます。今のコードにはありません（`write_to_shared_node` は0件）。
- **`__setattr__` を経由しない `model_state` の置き換え**（`world.__dict__["model_state"] = ...` と、同じ世界に別の世界の `__dict__` の一部だけを入れるコード）は `_core` とずれます。`src/` の差し替えはどれも `__dict__` の全部を入れ替えるので、`_core` も一緒に動きます。
- **`detach` は Python の呼び出しからしか使えません。** `model_state` の読み取りが残っている名前空間で使うと、毎回の作り直しで遅くなります。
