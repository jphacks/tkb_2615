# P0：最初に違った場所を探す道具

移植でハッシュが変わったときに原因を絞る道具と、ハッシュでは見えない確率の違いを確かめる手順です。決まりは [testing.md](../../testing.md)、この文書は手順と計測の記録です。コードは `benchmarks/first_difference.py` と `benchmarks/tick_probe.py` で、`src/` には何も足していません。`TickEngine` の組み立てと `CounterRNG.random` だけに掛けるので、段階の処理がどのモジュールへ移っても動きます。

## 使い方

Rust の規則と Python の規則を比べるとき：

~~~shell
python benchmarks/first_difference.py --both Scenario名 --output 出力先 --draws
~~~

変更の前後を比べるとき（`PYTHONPATH` を変えて記録します。各記録は、もう一度動かす方法を覚えています）：

~~~shell
PYTHONPATH=src:native/pylib python benchmarks/first_difference.py --record Scenario名 --output A --draws
PYTHONPATH=変更後/src:native/pylib python benchmarks/first_difference.py --record Scenario名 --output B --draws
python benchmarks/first_difference.py A B
~~~

長い Run は、粗く見てから絞ります。

~~~shell
python benchmarks/first_difference.py --record generated:1440:100 --output A --every-minutes 15
# 違った15分の間を、1分ごとに記録しなおす
python benchmarks/first_difference.py --record generated:1440:100 --output A2 --minutes 300:315
~~~

- `Scenario名` はゲートの Scenario か、`generated:分:人数[:seed]` です。
- 終了コードは、0 が違いなし、1 が違いあり、2 が比べられなかったときです。
- `--no-values` は、値を見るためのもう一度の実行をしません。`--reverse phase|within_phase` は、順序を逆にしたときに最初に違う場所を探します。`--full-every-minutes` は下の「見えないもの」を参照してください。

### 出力の例（情報の規則に入れた不具合）

`query_pressure` の減衰を 1.5 倍にしたコピーと比べた結果です。

~~~text
digests: first difference at line 41 of the streams
  A: engine 0, minute 5, phase sample_endogenous_events
  parts that differ (1): model_state/information_spread_v1
  model_state/information_spread_v1: 1 values differ
    model_state/information_spread_v1.query_pressure.A001
      A: 0.498 (float 0x1.fdf3b645a1cacp-2)
      B: 0.497 (float 0x1.fced916872b02p-2)
draws: 117896 on both sides; 793 differ in value or use, 0 of them flip a comparison; only in A 0, only in B 0
  first difference: engine 0, minute 6, phase sample_endogenous_events
  each entry is [draw, [[operation, the other number (hex), result], ...]]
    [2026,["information-contact-transfer-v1",6,"A006","A001","claim-false","heard"]]
      at engine 0, minute 6, phase sample_endogenous_events
      A: [["0x1.927b1e0420a7ep-1", [["lt", "0x1.760f2edf484a3p-2", false]]]]
      B: [["0x1.927b1e0420a7ep-1", [["lt", "0x1.760f2edf38a97p-2", false]]]]
final hashes: differ in run: world_hash, run: events_hash, run: metrics_hash
~~~

`engine` は Run の中で組み立てた `TickEngine` の番号です。

### 見えるもの、見えないもの

- 段階ごとの部分：世界の正規の文書の各項目（`agents`、`active_tasks`、`event_queue`、`rng_state`、`id_counters`、`metric_state`、`model_configs` など）、`model_state/<名前空間>`、`event_log`。
- 順序は見ません。digest はキーでそろえた正規の JSON です。順序に頼る処理は `--reversed-order` で確かめます。
- 追記だけの長い履歴（`tick_probe.APPEND_ONLY_LISTS`）と `event_log` は、項目ごとに1回だけハッシュします。古い項目の書き換えは、`--full-every-minutes`（既定60分）ごとの最初からのハッシュで見えます。すぐに見たいときは 1 にします。
- `metric_state` は見ますが、指標の記録（`MetricsRecorder`）は見ません。最終の `metrics_hash` が確かめます。
- `TickEngine` の外で起きた変更は、次の段階か次の engine の記録に現れます。`engine_start` の行は engine ごとにすべての部分を書きます。

## 確率の比較

`--draws` は `CounterRNG.random` を置き換え、返す値を `ObservedDraw`（`float` の派生）にします。規則のコードを変えずに、抽選の鍵、値、比べた相手（`draw < p`、`p > draw`、`draw * n` など）を 16 進で `draws.jsonl` に記録します。クラスのメソッドを替えるだけなので、`type(rng) is CounterRNG` の判定も変わらず、同じ kernel を通ります。

**限界：Rust の中の抽選は記録されません。** `--both --draws` で Python 側だけにある抽選は「only in B」と数え、違いにはしません（今は `social-contact-bucket-v1` と `information-contact-partner-v1` の、まだメッセージがない間の一部）。確率の式を Rust に移すと、その比較は Rust 側に見えなくなるので、次の手順でビット単位の比較を足します。

### ゲートの弱い2件での確認

[rust-port.md](../../rust-port.md) で検出されなかった2件を、`src/` のコピーに入れて確かめました（コミットしていません。`tests/integration/test_first_difference.py` がテストの中で同じことをします）。Scenario は `model__information__evidence-aware-rumor-v1`（20人、180分）です。

| 変更 | 最終のハッシュ | digest | 抽選の比較 | 最初の違い |
|---|---|---|---|---|
| 忘れる確率を1%増 | 同じ | 同じ（1261 段階） | 117,896 件のうち 9,697 件の比較相手が違う。比較の反転は0件 | 5分、`sample_endogenous_events`、`0x1.05e1ccc32891cp-9` と `0x1.088037c51c2cdp-9` |
| 訂正を受け入れる確率を1%減 | 同じ | 同じ（1261 段階） | 同じ母数のうち 59 件が違う。反転は0件 | 60分、`dispatch_due_events`、`0x1.8f5c28f5c28f5p-1` と `0x1.8b5dcc63f1411p-1` |

### レーンが足すテスト（確率の式ごと）

1. Rust に、**確率だけを返す関数**を作ります（`#[pyfunction]`、`native.rules("<レーン>")` で呼ぶ）。抽選と比較は含めません。数学関数は `core/mathx.rs` を通します。
2. Python の規則を基準にして、同じ入力で比べるテストを `tests/integration/test_native_kernels.py` に足します。基準の値は `SHELTERSIM_NATIVE=0` の記録から取ります（移植後も Python の規則は `=0` で残ります）。
3. 1%変えた値が基準に入らないことも確かめます。テストが本当に区別できることの確認です。

**実際の例：忘れる確率**（`test_a_rust_probability_has_the_bits_python_compared_with_its_draws`）。入力が設定の値1つなので、記録した比較相手と直接比べられます。

~~~python
# --record ... --draws を SHELTERSIM_NATIVE=0 で動かし、output に書いたあと
python_compared_with = set()
with (output / "draws.jsonl").open(encoding="utf-8") as rows:
    for line in rows:
        row = json.loads(line)
        if row["k"][0] == "information-forgetting-v1":
            python_compared_with.update(use[1] for use in row["u"] if use[0] == "lt")
rate = 0.002  # gate_scenarios.with_rumors
# レーンの関数に置き換える。今は parity の expm1 が代わりをしている
rust = -native.rules("parity").expm1([-rate])[0]
assert python_compared_with == {rust.hex()}
assert (rust * 1.01).hex() not in python_compared_with
~~~

**入力が人ごとに違う式**（訂正の受け入れなど）は、記録だけでは入力が分かりません。Python の式を同じ値を返す名前付きの関数に切り出して（ハッシュは変わりません）、テストの中で包みます。次の `correction_acceptance_probability` は、レーンがこのとき作る関数の形を示すもので、今はまだありません。

~~~python
def test_the_correction_acceptance_probability_has_the_python_bits(generated_scenario, monkeypatch, tmp_path):
    rule = native.rules("information").correction_acceptance_probability
    python = handlers.correction_acceptance_probability
    seen = 0
    def both(config, trust, reliability):
        nonlocal seen
        value = python(config, trust, reliability)
        assert rule(config.correction_acceptance_probability, config.correction_trust_sensitivity,
                    config.correction_source_sensitivity, trust, reliability).hex() == value.hex()
        seen += 1
        return value
    monkeypatch.setattr(handlers, "correction_acceptance_probability", both)
    run_scenario(with_rumors(generated_scenario), tmp_path)
    assert seen  # Scenario がこの式に届いたこと
~~~

### 確率の比較がある場所（ゲート全体、`SHELTERSIM_NATIVE=0`）

移すときの一覧です。「比較」の列は `lt` などの回数で、合計はゲートを1周した値です。

| 鍵の先頭 | 回数 | Scenario数 | 使い方 | 場所 |
|---|---:|---:|---|---|
| `information-contact-partner-v1` | 7,686,779 | 53 | `mul`（`int(draw * total)`） | `policies/information.py`、`native/src/contacts.rs` |
| `social-contact-bucket-v1` | 3,170,911 | 49 | `mul`（`int(draw * total)`） | `policies/social.py`、`native/src/contacts.rs` |
| `information-forgetting-v1` | 625,045 | 37 | `lt` | `policies/information.py` |
| `behavior` | 63,697 | 55 | `lt` 231,229 回（累積確率）、`ge` 63,697 回、`mul` 224 回（同点の抽選） | `policies/behavior.py` |
| `information-contact-transfer-v1` | 62,845 | 43 | `lt` | `policies/information.py` |
| `toilet-service` | 3,345 | 55 | `ge`・`lt`（範囲の確認）、`mul`（所要時間） | `domain/facilities.py` |
| `rumor-belief-acceptance-v1` | 3,111 | 37 | `lt` | `events/handlers.py` |
| `contact-correction-acceptance-v1` | 1,747 | 37 | `lt` | `events/handlers.py` |
| `correction-acceptance-v1` | 1,266 | 37 | `lt` | `events/handlers.py` |
| `profile` | 9,016 | 49 | `mul`（tick の外、Scenarioの生成） | `experiments/profiles.py` |
| `population` | 5,288 | 49 | `mul`・`sub`・`lt`（tick の外、Scenarioの生成） | `region/population.py` |

## 順序の確認

`--reversed-order`（`gate_all_paths.py`）は `world.agents` と `model_state` のすべての dict を逆にします。段階の終わりごとに逆にするので、どの段階も、分ごとに元の順序と逆の順序の両方を見ます。段階の途中で逆にする場所は `tick_probe.WITHIN_PHASE` です（イベント1件ごと、提案の適用ごと、`WorldView` を作る前、各 arbitrate と `assign_pending` の前）。反復の途中では逆にできないので、規則が反復を終えている場所だけです。

### 結果（2026-10-31、55個の Scenario）

| 回 | 結果 |
|---|---|
| `reversed_order`（段階の終わり） | **55個すべて、3つのハッシュと拒否の結果が同じ** |
| `reversed_within_phase`（段階の途中も） | **55個すべて同じ**。全件の実行では、54個が同じで、`generated_project` だけ `wait_idle` の期限で止まりました。止まった原因は結果の違いではなく遅さで（ほかの実行と重なった回）、単独でもう一度動かして同じになることを確かめました。そのあと期限を延ばしました |
| 追加の試し：段階の終わりに `zones`、`zone_edges`、`facilities`、`staff`、`support_requests`、`resources`、`active_tasks`、`id_counters`、`metric_state` も逆にする（コミットしていない、コピーでの1回） | 55個すべて同じ |

分かることと、分からないこと：

- 今の規則は、調べた場所では dict の順序に結果を頼っていません。型付きの行（`IndexMap`）で順序を保つ必要は、結果の一致のためには見つかっていません。ただし、規則が反復している最中の順序は変えていません。
- 試したのは「元の順序」と「逆の順序」の2通りだけで、任意の順序ではありません。リストの順序は値の一部なので変えていません。
- **これは手順4の並列化の証拠ではありません。** 多くの規則は ID で並べ替えてから処理するので、dict の順序を変えても処理の順序は変わりません。人を処理する順序に結果が頼らないこと（`advance_movement`、`_apply_proposal` など）は、この確認では分かりません。

## 計測

WSL2、16コア。指定がなければ `SHELTERSIM_NATIVE=1`、ほかに重い処理がない状態の1回の値です（1日の生成 Run は2回の範囲）。digest の費用は世界の大きさで変わります。

| Scenario | 記録なし | digest | digest と `--draws` |
|---|---:|---:|---:|
| `generated:1440:100`（100人、1日、保存なし） | 11.6〜13.8 s | 56〜62 s（約4.5倍） | 59〜64 s |
| 同、`--every-minutes 15` | | 18〜22 s（約1.5倍） | |
| `generated_project`（100人、12時間、保存あり） | 28 s | 77 s（2.7倍） | 88 s（他と重なった回） |
| `interventions` | 72 s | 103 s（1.4倍） | |
| `long_generated` | 7.6 s | 30 s（4.0倍） | |
| `model__information__evidence-aware-rumor-v1`（20人、180分） | 3.4 s | 3.8 s（1.1倍） | 6.4 s（1.9倍） |
| `branch_commands` | 0.9 s | 1.8 s（2.0倍、ほぼ読み込みの時間） | 1.7 s |

- `SHELTERSIM_NATIVE=0` の1日の生成 Run は、他と重なった回で 85 s から 291 s（約3.4倍）でした。
- 履歴を毎回ハッシュしていた最初の作りでは、同じ1日の Run が 277 s（約13倍）でした。それが `APPEND_ONLY_LISTS` を置いた理由です。
- `--draws` の記録は、`generated:1440:100` で 4.6 MB、噂の Scenario で 18 MB です。ゲートの全件を両方の kernel で動かすと、出力全体で約6 GB になります。出力先の空きに注意してください。
- `--both --draws` をゲートの55個すべてで動かすと、7個ずつ並べて約10分でした（ほかの実行と重なった回）。結果は55個すべて digest も抽選も違いなしです（Rust の中の抽選は除きます）。
- 順序の確認は重いです。`--reversed-order` は 12並列で1回 約20分でした。`generated_project` は 22 s に対して、段階の終わりで 242〜260 s、段階の途中も含めると 1205 s です。

digest と `--draws` で最終のハッシュが変わらないことは、`fixtures/gate-all-paths.json` の値と比べるテストで確かめています。順序を逆にした回は、`--reversed-order` が1回目と比べます。
