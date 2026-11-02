# L0 レーン：断りの実測と手順4の計画

**規則は1つも動かしていません。結果は変わりません。係数は1つも増えていません。** 手順4の棚卸しが文を読んで付けた分類を、実測に置き換えました。結果とレーンの組み替えは [step4-plan.md](../step4-plan.md) の2節と8節、一度も届かない場所の一覧は [step4-refusal-sites.md](../step4-refusal-sites.md) です。

## 足したもの

- `native/src/core/census.rs`：`Unsupported` が作られた行と理由、受けたところ（`or_rule!`、`__wrapped__`）を数えます。切っているときは断りの途中でフラグを1回読むだけです。**環境変数は読みません**（`native/src` に `std::env` がないまま）。
- `native/src/lib.rs`：`Unsupported` を型1つにしました。`Unsupported::new_err` が `#[track_caller]` で呼び手の行を数えるので、か所を直していません。Python から見た例外のクラスは同じです（名前、モジュール、継承）。`fn unsupported` 6つ（`core/row.rs` と5つの `rules/`）と `tick.rs` の `refuse` に `#[track_caller]` を足しました。
- `benchmarks/rule_shadow/refusal_census.py`：`SHELTERSIM_REFUSAL_CENSUS=DIR` で `sitecustomize.py` が入れます。`rules`、`state` の関数の Python の入口を包み、`TickEngine.step` で tick ごとに分けます。`report` が表を作ります。
- `tests/integration/test_refusal_census.py`。

**作らなかったもの：** `rule_shadow` の `Recorder` と `pure_shadow` は kernel を比べるためのもので、Rust の中で捨てられる断りが見えません。プロセスごとに書く形だけ同じにしました。`tick.stats()` と `after_tick.stats()` は残し、計測と突き合わせました。

## 結果

2026-11-02、WSL2、16コア、ほかのレーンと同時です。コードは `5785df7`。その後の変更は `refusal_census.py` の `report` と文書だけです。

| 確認 | 結果 |
|---|---|
| 全テスト | 計測なし 4,284件成功、6件 skip。`SHELTERSIM_NATIVE=0` 2,079件成功、2,211件 skip。計測あり 4,277件成功、1件失敗（下）、12件 skip |
| `gate_all_paths.py --expect` | 55シナリオ、問題0件 |
| `--reversed-order` | 55シナリオ、問題0件（driver は通りません） |
| 計測ありのゲート、`validate` の確認つきのゲート | どちらも 55シナリオ、問題0件 |
| 7日分（`--expect`、driver が動く回）、計測なし | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。一致 |
| 7日分、計測あり | 同じ3つ。一致 |
| `SHELTERSIM_DISPATCH_COUNT_ONLY=1` のゲート（`rule_shadow` が新しい型でも動く確認。**driver の回**） | 55シナリオ、問題0件、違い0件 |
| `tracked_mirror_gate.py` | **動かしていません**（指示によります）。`core/state_mirror.py` は変えていません |
| `ruff`、`cargo clippy`（`disallowed_methods`） | 問題なし |

**動いた回数と断られた回数（driver、基準の木）：** 動いた tick はゲート 28,892、7日分 10,080。断った tick はゲート 2,640（Provider 1,800、trace 840）、7日分0件。kernel の断りはゲート 6,280、7日分 720 で、**`or_rule!` の合計と一致します。** 計測と別に数える `after_tick.stats()` も理由ごとに一致しました（1,296 回）。

**影と感度：** 動かした規則がないので、比べる相手がありません。計測そのものの感度は、テストが1回の断りを行と理由まで確かめます。

**予算：** `tick_budget.py`、`fork-b1` の 9600分から 480 tick、5回、同時。load average 約 9〜26。計測は切った状態です。**1.026**（1.046、1.074、0.968、1.026、0.995）。A/A 1.051（0.931、1.101、0.867、1.081、1.051）。上限 1.10 以内、状態は5回とも一致。**A/A の揺れの中で、これより細かくは言えません。** 7日分の断りは 0.07 回/tick で、増えたのはその途中のフラグの読み1回と `#[track_caller]` の隠れた引数です。計測をかけた7日分は Run のスレッド時間 79.9 秒、切った回は 79.2 秒でした（1回ずつ、同時）。

## Python に残した規則

このレーンで動かしたものはありません。残りは step4-plan.md の8節です。

## モデルの担当の方に見ていただくコード

**係数も式もありません。Rust だけの条件も増えていません。** 担当の方に確かめていただくこと（D6、D11、D13、`transfer_equipment` の不具合）は step4-plan.md の1節にまとめました。

## 確かめていないこと

step4-plan.md の10節です。一番大きいのは、**「一度も届かない」は有効な状態から届かないという意味ではないこと**と、Windows、macOS で動かしていないことです。

## レーンとの重なり

`native/src/lib.rs`、`core/row.rs`、`rules/{behavior,information,psychology,social_propagation,world_view,tick,population,population_lifecycle}.rs` の1〜3行、`sitecustomize.py` の4行。`rust/m6-handlers-rest` とは `sitecustomize.py` で衝突します（両方を残します）。
