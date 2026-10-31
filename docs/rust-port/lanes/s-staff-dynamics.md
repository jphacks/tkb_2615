# 先行レーン S：`domain/staff_dynamics.py` を丸ごと移す

普通の domain モジュール1つを、テストとエラーの扱いごと移して、残りの大きさを測るレーンです（設計の「大きさで止まる」の実験）。

## 移したもの

Rust が `staff_dynamics_v1` を持ちます（`state/staff_dynamics.rs`）。`rules/staff_dynamics.rs` に、モジュールのすべての規則があります。

| Python の規則 | kernel | 呼ばれる場所 |
|---|---|---|
| `update_staff_dynamics` | `update` | tick（`ArbitratePhase`） |
| `staff_dynamics_is_available`、`staff_dynamics_on_shift` | `is_available`、`on_shift` | tick（5モジュールから） |
| `validate_staff_dynamics_state` | `state.staff_dynamics.validate` | tick の検証 |
| `canonical_staff_dynamics_config` | `canonical_config` | tick（`simulator_signatures` から約1回/tick）と Scenario |
| `add_staff` | `add_staff` | イベント、Branch |
| `create_staff_dynamics_state` | `create_state` | 世界を作るとき |
| `validate_staff_shift_windows_override` | `validate_override` | Scenario の編集のみ |

Python の規則は比較の基準と、kernel が決めない値のために残します。**エラーの文はすべて Python が出します**（`Unsupported` のあとで Python が動きます）。文を Rust に移すのは手順4です。

移ったモデルのコード（モデルの担当者へ）：`StaffFatigueFormula.fatigue`・`maximum_minutes`（`policies/registry.py`）、`update_staff_dynamics` の式、`staff_is_available` の読み取り（`domain/operational.py`）。**既定値 240、30、0.005、0.0 と上限 1440、480、10080、21 が Python と Rust の2か所になりました。** 片方だけ変えると `test_staff_dynamics_row.py` が失敗します。係数と式の意味は変えていません。

## ひな形に足したもの

| もの | 場所 | 足りなかった理由 |
|---|---|---|
| `Maybe<T>` | `core/row.rs` | キーがない場合。`Option` は None のキーで、古い `config` を表せません |
| `str_enum!` | `core/row.rs` | 閉じた文字列の集合（`last_activity`）。M4 の `phase`、`status` にも使えます |
| `exact_int`、`exact_float`、`exact_str`、`exact_dict`、`exact_list`、`attribute`、`unsupported`、`namespace_is_absent` | `core/row.rs` | kernel ごとに書き直していました |
| `set_all` | `state_row!` | 計画した記録を1回で書きます |
| `kernel_first` | `core/kernels.py` | `SEGMENTS` は段階の手順だけで、関数として呼ばれる規則に使えません |
| `shadow_kit.py`、`shadow_<名前>.py` の自動読み込み | `benchmarks/rule_shadow/` | `pure_shadow` で、読むだけの kernel を毎回比べます |

ひな形にまだないもの：`rules/policy_table.rs`（方策の選択。今回は ID を2つ書きました。`register_policy` は既存の ID を上書きできないので、これで安全です）、`@pytest.mark.python_rules` と lint（今回 patch しているテストはありません）。

## 残っているもの

- エラーの文（`raise` 18か所）を Rust に移す：手順4。
- 切り離し：`staff.py`、`staff_operations.py`、`inspection/frames.py` が木を読むので、まだミラーを捨てられません。M4 のあとです。
- `validate_override` はゲートから呼ばれません。比べたのはテストの入力だけです。

## 計測

`update` は 12人の世界、それ以外も同じ世界で、スレッドの CPU 時間の7回の最小値です（load average 約 110、merge 前の1回の計測）。

| 規則 | Python | wrapper と kernel |
|---|---:|---:|
| `update_staff_dynamics` | 15,120 ns | 2,141 ns |
| `staff_dynamics_is_available` | 859 ns | 450 ns |
| `staff_dynamics_on_shift` | 552 ns | 306 ns |
| `canonical_staff_dynamics_config` | 13,798 ns | 1,919 ns |
| 検証 | 15,994 ns | 578 ns |

`tick_budget.py`（9600分から480 tick、同時実行、スレッドの CPU 時間）：

| 版 | 基準との比 | A/A |
|---|---:|---:|
| 最初の merge 前（3回、load average 21〜96） | 1.037（1.054、1.017、1.037） | 0.988 |
| `60d7149`（レーン B、C の merge 後、5回） | 1.021（0.983、1.07、1.021、0.967、1.03） | 1.029（1.071、0.987、0.973、1.029、1.065） |

部品の足し算では tick あたり約 50 µs 減る計算ですが、全体の差は A/A の揺れの中で、**速くなったとは確かめられていません。** `33ce270`（レーン A の merge 後）では測り直していません。

## 結果

`feature/rust-port` の `1506f2b`（レーン A、B、C、P4〜P6）を merge した `33ce270` の結果です。

| 確認 | 結果 |
|---|---|
| テスト | kernel あり 2739件通過、6件 skip。`SHELTERSIM_NATIVE=0` 1774件通過、971件 skip |
| `gate_all_paths.py --expect`（素のまま、`rule_shadow` つき）、`--reversed-order` | どれも55シナリオ、問題0件 |
| 7日分のハッシュ（素のまま、`rule_shadow` つき） | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041` |
| `rule_shadow`（ゲート） | `update` は 31,530回のうち kernel 28,850回、残り 2,680回は名前空間がない世界。比べた値 870,006個。読む kernel は `on_shift` 185,456回、`is_available` 149,458回、`canonical_config` 36,517回、`create_state` 65回、すべて kernel が動きました。`add_staff` は34回のうち33回。違い0件、このレーンの `Unsupported` 0回 |
| `rule_shadow`（7日分） | `update` 10,080回すべて kernel、比べた値 453,600個、`on_shift` 311,311回、`is_available` 74,785回、`canonical_config` 10,799回、`create_state` 1回、違い0件、このレーンの `Unsupported` 0回 |
| `tracked_mirror_gate.py` | 220行、問題0件 |
| `ruff check`、`ruff format --check`、`clippy`（`disallowed_methods`） | 問題なし |
| 道具の確認（最初の merge 前）：kernel の疲労を 1% 変えたコピー | `rule_shadow` が違いを報告し、ゲートも `staff-fatigue-linear-v1` で失敗 |

ゲートの行の網羅（最初の merge 前、`domain/staff_dynamics.py`）：kernel ありだけで 34/179 行、kernel なしと合わせて 132/179 行です。残りは主に `raise` と `validate_override` で、`test_staff_dynamics_row.py` が確かめます。

Windows と macOS では未確認です（CI で動きます）。`exp` の一致は Linux でだけ確かめました。

## M2〜M6 の見積もり

このレーンの大きさから見た、残りの移植の見積もりです。誤差は2倍ほどあります。

| 項目 | 見積もり |
|---|---:|
| 残りの Python | 約 25,000 行 |
| レーン | 約 32 |
| 書く Rust | 約 55,000 行 |
| 実時間 | 約 32〜40 時間 |

実測した速さは 28 行/分の1つだけです。エラーの文の移植（`raise` 1,373か所）は含みません。
