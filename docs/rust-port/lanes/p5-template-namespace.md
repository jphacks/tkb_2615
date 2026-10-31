# P5：型付きの行のひな形（`psychological_distress_v1` と `metric_state`）

Rust が持つ最初の本番の状態です。次の名前空間は、この文書の順に移します。P4 の [p4-world-core.md](p4-world-core.md) の仕組み（転送、`write_through`、taint、切り離し）は、そのまま使います。

## できたもの

| もの | 役目 |
|---|---|
| `native/src/core/row.rs` | `Field`（行の中の値ができること）、`Scalar`、`IdMap`、`load_row`、`map_set`、`state_row!` |
| `native/src/state/psychological_distress.rs` | `psychological_distress_v1` の行と検証（手本） |
| `native/src/state/metric_state.rs` | `metric_state`（`IdMap<f64>`。世界の項目で、`model_state` の中ではありません） |
| `native/src/rules/psychology.rs` | 更新の段階の最後の手順：苦痛の監視（`domain/distress_monitor.py`）と長期の苦痛（`update_psychological_state`） |
| `core/state_mirror.py` の `OWNED_FIELDS` | `model_state` の外の世界の項目を持つ表 |
| `benchmarks/rule_shadow/` | kernel と Python の規則を毎回比べる道具（下） |
| `tests/integration/test_psychology_row.py` | 行、taint、転送、任せる場合、Python の規則との一致 |

`native/src/state/sample_namespace.rs` も `state_row!` に書き直しました。P4 のテストがそのままマクロのテストになっています。

## 手順

### 1. 行を書く

`native/src/state/<name>.rs` に項目を並べます。

~~~rust
state_row! {
    #[derive(Clone, Copy)]
    pub(crate) struct AgentRecord {
        baseline_distress: f64,                      // Rust の規則が書かない項目
        long_term_distress: f64 => set_long_term_distress,  // 書く項目には setter の名前を付ける
        daily_observed_minutes: i64 => set_daily_observed_minutes,
    }
}

state_row! {
    pub(crate) struct DistressRow {
        schema_version: i64,
        last_updated_minute: i64 => set_last_updated_minute,
        agents: IdMap<AgentRecord>,
    }
}
~~~

- **項目は木と同じ順に並べます。** `to_py` がこの順で作ります。`write_json` はバイト順に並べ直すので、順序を気にしません。
- 使える型は `f64`、`i64`、`bool`、`Arc<str>`、`Option<T>`、`IdMap<T>`、`Vec<T>`、`V`（自由な形の部分）と、別の `state_row!` の構造体です。足りなければ `core/row.rs` に `Field` を実装します。
- マクロが作るもの：`from_py`（項目がちょうどこれだけで、それぞれ厳密な型のとき）、`to_py`、`write_json`、転送された書き込みの `apply`、setter。
- setter は `Scalar`（入れ物でない値）の項目にだけ付けられます。入れ物を入れる書き込みは節点の表を変えるので、`write_through` を使います（`rules/psychology.rs` の新しい人の記録が例です）。
- 根が辞書だけなら `pub(crate) type MetricRow = IdMap<f64>;` で足ります。
- 登録は1行です。`m.add("ROW", Py::new(py, RowType::new("<name>", load_row::<DistressRow>))?)?`

### 2. 持ち主を宣言する

`core/state_mirror.py` の `OWNED_NAMESPACES` に1行足します。`model_state` の外の項目は `OWNED_FIELDS` です。

`OWNED_FIELDS` の項目を丸ごと代入するコード（`world.metric_state = {...}`）は受け取りません。その項目は素の dict になり、kernel は `field_mirror_of` で `Unsupported` を返すので、Python の規則が動きます。結果は変わらず、遅くなるだけです。`src/` にそのようなコードはありません。

### 3. 規則を一緒に移す

**名前空間を丸ごと書くコードを残して移してはいけません。** 持ち主を宣言するだけでは、その書き込みが毎回の転送になります。

kernel は2段に分けます（`rules/psychology.rs`）。

1. **計画：** 入力を読み、Python の規則が見る値をすべて確かめ、新しい値を計算します。Python の規則が断る値、1つでも判断に迷う値があれば `Unsupported` を返します。ここまで何も変えません。Python の手順がそのあと最初から動き、同じ文で失敗します。
2. **書き込み：** `NamespaceMirror::with_row_tree` の中で setter を呼びます。ビットが同じ値は書きません。多くの項目は毎分変わらないので、木への書き込みがほとんどなくなります。

ほかの決まり：

- 小数の計算は Python の式と同じ順で、`max`・`min` は `mathx::py_max`・`py_min` です。
- Python が整数の割り算をしていても、両方が小さい整数なら `f64` の割り算で同じ値です（CPython の `long_true_divide` も正しく丸めます）。大きい値は別です。
- `with_row_tree` の中では Python のコードを動かしません。`Tree::set` は C API で、スカラーだけを入れます。
- kernel の入力の型を、素の dict と決めつけないでください。`WorldView.agents` は `MappingProxyType` です。実際、最初の版はこれを断って毎回 `Unsupported` になり、**ハッシュはすべて通っていました**（下の「kernel が動いたことを確かめる」）。

### 4. 段階の手順を差し替える

`experiments/phases/segments.py` の `SEGMENTS` に1行足します。kernel は `(controller, *手順の引数)` を受け取ります。方策は `policy_version` の文字列で確かめ、知らないものは `Unsupported` です。

### 5. 検証を任せる

`domain/<モジュール>.py` の末尾で登録します。

~~~python
if native.available():
    register_rust_validator(KEY, native.state("<name>").validate)
~~~

kernel は「Python の規則がすべて通す」ときだけ true を返します。行に入った時点で型は確かなので、値の範囲だけを見ます。`SHELTERSIM_NATIVE=0` では `native.state` が `None` なので、`if` が要ります。

### 6. kernel が動いたことを確かめる

**ハッシュは kernel が動いたことを言いません。** `Unsupported` なら Python が動いて、ハッシュは同じだからです。`benchmarks/rule_shadow/sitecustomize.py` を写して、次の名前空間の手順を比べます。

~~~shell
SHELTERSIM_RULE_SHADOW=出力先 PYTHONPATH=benchmarks/rule_shadow:src:native/pylib \
  python benchmarks/gate_all_paths.py --output 別の出力先 --expect fixtures/gate-all-paths.json
python benchmarks/rule_shadow/summary.py 出力先
~~~

毎回、Python の規則を素の写しの上で動かし、そのあと kernel を世界の上で動かして、JSON の文字列（小数のビット、`1` と `1.0` の違い、キーの順序が見えます）で比べます。行と木の一致（`verify`）も毎回見ます。Project の worker の別プロセスも数えます。`Unsupported` は理由ごとに数え、`records_added`、`day_ends` などで枝に届いたかも分かります。比べる回数が少ない枝は、`test_psychology_row.py` の乱数のテストのように、シナリオに頼らず入力を作って比べます。

`first_difference.py --both` も使えます。こちらは段階ごとの digest で `SHELTERSIM_NATIVE=0` と比べます。

### 7. テスト

`test_psychology_row.py` を写します。

- 行：`row_json` が `canonical_json` と同じバイト列、作り直した木の順序。
- taint：合わない値ごとに。
- 転送：Python からの1項目の書き込みが行に届くこと。
- **丸ごとの書き込みがないこと：** `stats()["whole_writes"]` が tick で増えないこと。kernel が動かないと、ここで失敗します。
- 任せる場合：何も変えずに `Unsupported` になり、Python の規則が同じ文を出すこと。
- 検証：Python が断るものを kernel が取らないこと。
- Python の規則との一致：ビット単位。1% 変えた値が区別できることも。

新しいテストのファイルは、`tests/support/tracked_mirror_plugin.py` の `own_sinks` にも足します（本物の sink を確かめるテストだからです）。

### 8. 確認

`gate_all_paths.py --expect`、7日分のハッシュ、`rule_shadow`、`tick_budget.py`、両モードの全テスト、`tracked_mirror_gate.py`、`ruff`、`cargo clippy`。

## テストの影の文書との関係

`tests/support/tracked_mirror.py` の `Session(tracked=True)` は、自分の sink を世界の文書に載せます。Rust の行とは一緒に動かせないので、その間は `OWNED_NAMESPACES`・`OWNED_FIELDS` を空にします（kernel は `Unsupported` で、Python の規則が動きます）。`Session(tracked=False)` の回は本番のままです。`tracked_mirror_gate.py` は「素の回」と各段階で比べるので、**今は Rust の規則と Python の規則を段階ごとに比べていることになります**。

`tick_probe.py` の順序の逆転は、Rust の持ち物については `dict` のメソッドで直接行います。順序は状態ではないので行に届きません（`clear` が転送されると構造体の行は taint します）。テストの sink の木は今までどおり転送します。Rust の規則は行の順序で動くので、逆転の回でも Rust の規則の中の順序は変わりません。今回移した規則は人の ID で並べるので、順序に頼りません。

## 費用

手順ごとの値は、100人・480分の生成 Run の中でスレッドの CPU 時間を測りました（他のレーンと同時に動かした1回の値）。全体は `tick_budget.py` で、9600分から480 tick、同時実行、スレッドの CPU 時間です。late の tick は約 5.3 ms（負荷の高い回は 8〜10 ms）で、load average は 5〜80 でした。

| `tick_budget.py` | 基準との比 | A/A |
|---|---:|---:|
| 規則も移した今の形（5回） | **0.994**（0.994、0.983、1.007、0.994、1.009） | 0.993（0.977、0.993、1.004、1.056、0.934） |
| 持ち主の宣言だけで規則は Python（3回、コミットしていないコピー） | 1.016（1.016、0.995、1.035） | 1.011（1.011、1.026、0.973） |

世界の `state_digest` は、どの回も3つとも一致しました。

| 部品 | 費用（100人） |
|---|---|
| 手順 `_update_long_term_psychology`：基準（素の dict、Python の規則） | 127 µs/tick |
| 同：持ち主の宣言だけ（Python の規則、丸ごとの書き込み） | 213 µs/tick |
| 同：Rust の規則 | 65 µs/tick |
| 名前空間の丸ごとの書き込み（`replace_root`） | 79 µs/回。Rust の規則では tick の中で0回 |
| 検証：Python（`psychological_agents_valid` の kernel を含む） | 8.9 µs/回 |
| 検証：`rust_validated` を通した kernel | 4 µs/回（kernel だけなら 0.11 µs。残りは計測の枠と `rust_validated`） |
| `metric_state` への1項目の書き込み（転送） | 116 ns（素の dict は 20 ns）。約6回/tick |
| 記録の1項目の書き込み（転送） | 161 ns。tick の中で0回 |
| `model_state` の読み取り（`NamespaceTable`） | `d[k]` 10→20 ns、`d.get` 16→29 ns。約 670回/tick で約 8 µs |

**P4 の +17% について：** 今回の名前空間では、持ち主の宣言だけの場合も規則を移した場合も、A/A の揺れの中で、+17% に当たる費用は見つかりませんでした。P4 の計測は `body_state_v1` など書き込みの多い3つの名前空間で `ValueRow` でした。この2つの名前空間は読み書きが少ないので、その +17% の原因の切り分けにはなっていません。分かったのは、上の表の部品を足すと、tick あたり約 50 µs（約 1%）減る計算だということです。この差も `tick_budget.py` では見えません。**内訳は部品ごとの計測の足し算で、全体の差として確かめたものではありません。**

## 結果

2026-10-31、WSL2、16コア。

| 確認 | 結果 |
|---|---|
| `gate_all_paths.py --expect` | 55個すべて一致 |
| 7日分のハッシュ | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。`--expect` と一致 |
| `rule_shadow`（ゲート全体） | 手順 31,530回のうち kernel が 28,850回動き、比べた値 9,903,800個、違い0件。`Unsupported` は 2,680回で、すべて「長期の状態を持たない方策」です。記録の追加 153件、日の終わり2回（32記録）。検証の kernel は 31,667回受け入れ、断りは0回で、受け入れるたびに Python の検証も通りました |
| `rule_shadow`（7日分） | kernel 10,080回、比べた値 11,337,522個、違い0件、`Unsupported` 0回。日の終わり7回（700記録） |
| 道具の確認：kernel の重みを 1% 変えたコピー | `rule_shadow` が日の終わり2回を違いとして報告。ゲートも `long_generated` で失敗 |
| `first_difference.py --both`（ゲート全体） | 55個すべて、220,892 段階で digest が同じ |
| `first_difference.py --both --draws`（`generated:1500:100`、5分ごと） | 301段階で digest が同じ。両側にある抽選 14,349件で、値にも比較相手にも違いなし。Rust の中の抽選は比べられません |
| `gate_all_paths.py --expect --reversed-order`（段階の終わりと段階の途中） | 55個すべて、どの回も一致 |
| `tracked_mirror_gate.py`（55シナリオ × 4回） | 220行、問題0件。段階の比較は合計 662,130 回。追跡する回は Python の規則、比べる相手の素の回は Rust の規則です |
| `pytest -p support.tracked_mirror_plugin` | `test_first_difference.py` の4件だけ失敗（順序の逆転のテストが plugin の木を想定していません）。P4 の commit でも同じファイルの6件が失敗するので、この作業で増えたものではありません |
| `gate_all_paths.py --coverage` の行の網羅（kernel ありとなしの合計） | `domain/psychological_state.py` 87%、`domain/distress_monitor.py` 78%。kernel ありだけでは 25%、19% で、Python の規則がほとんど動かなくなったことを示します |

## 残っている作業（レーン A）

Rust に移したのは、更新の段階の最後の手順だけです。下の心理の規則は、その後レーン A が移しました（[a-psychology.md](a-psychology.md)）。

- `native_deltas`（Python で入力を作って `native/src/psychology.rs` を呼ぶ形）と `explain_update`、`deltas_without_traces` の本体。
- `apply_state_deltas`（`policies/psychology.py`）。人と `body_state_v1` を書くので、人の窓（P6）のあとです。
- `create_psychological_state`（世界を作るときの丸ごとの書き込み。tick の外です）。
- `distress_monitor_summary`（読むだけ。指標の計算で、tick の外です）。
- `metric_state` のほかの書き手（`domain/`、`events/`、`experiments/phases/` の約100箇所）。1項目ずつの転送で動きます。それぞれの規則を移す作業が一緒に移します。
- 整数の設定値：`chronic_retention` などが厳密な `float` でないと kernel は `Unsupported` です（`0` や `1` を JSON に書いた Scenario）。結果は変わらず、毎 tick が Python になります。ゲートにはありません。

## 危ないところ

- **`WorldView` の読み取り：** `chronic_distress` は木の値を毎回読みます。切り離しはまだできません。
- **別名：** 名前空間は置き換えられなくなったので、`state = world.model_state[KEY]` を持ち続けると、次の tick の値が見えます。今までは古い tick の値でした。`src/` にそのようなコードはなく、テストが通ることで確かめましたが、通らない経路は分かりません。
- **`policy_version` で方策を判断します。** 同じ名前を名乗る別の実装や、`update_long_term_state` を差し替えるテストは、kernel に気づかれません。`@pytest.mark.python_rules` はまだありません。今のテストに差し替えるものはありません。
