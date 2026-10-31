# レーン I：移動の状態（`movement_v1`）

Rust が `movement_v1` を型付きの行で持ちます。**移したのは状態だけで、規則は Python のままです**（M4 で移します）。規則は記録を1件ずつ書くので、名前空間を丸ごと書く転送はありません。

## 移したもの

| もの | 場所 |
|---|---|
| 行（`Position`、`Point`、`Journey`、`MovementRow`） | `native/src/state/movement.rs` |
| 持ち主の宣言 | `core/state_mirror.py` の `OWNED_NAMESPACES` |
| 行と木の比較、`advance_movement` の逆順 | `benchmarks/rule_shadow/movement_shadow.py` |
| テスト | `tests/integration/test_movement_row.py` |

- `positions.point` は `Option<Point>` です。
- `Journey` は `state_row!` を使わず手で書きました。`queue_id`、`support_request_id`、`support_staff_id` は、記録に**ない**ことがあるからです。ここに null があれば `Misfit` です（Python の検証も通しません）。
- int の `progress` や座標は、Python の検証は通しますが、行は taint します。Python の規則が木の上で動くので、結果は変わりません。

モデルのコードは移していません。係数も式も変えていません。

## 規則を移さなかった理由

状態だけの費用が予算の中だったからです（`tick_budget.py`、9600分から480 tick、3回、同時実行、スレッドの CPU 時間。load average 約 13〜37）。

| 変種 | 基準との比 | 各回 | A/A |
|---|---:|---|---:|
| **型付きの行（このレーン）** | **1.039** | 1.022、1.039、1.049 | 1.023 |
| `movement_v1` だけ `ValueRow` | 1.070 | 1.126、1.053、1.070 | 1.025 |
| `body_state_v1` だけ `ValueRow` | **1.162** | 1.162、1.145、1.162 | 1.001 |
| `social_network_v1` だけ `ValueRow` | 1.007 | 1.007、1.003、1.068 | 1.001 |
| 3つとも `ValueRow`（P4 の形） | 1.242 | 1.242、1.232、1.273 | 0.979 |

**P4 の +17% の内訳：** ほとんどが `body_state_v1` です。`movement_v1` は `ValueRow` でも1つだけなら予算の中で、社会関係は差が見えません。3つ合計は今回 +24% で、P4 の +17% より大きい値です（基準のコードも負荷も違います）。A/A の揺れは 0.81〜1.06 あるので、小さい差は言えません。**`body_state_v1` は、M3 で規則と一緒に移さないと予算を超えます。**

最後の commit のビルドでの予算（2026-11-01、ほかのレーンと同時実行、load average 開始 26、終了 19）：**1.021**（各回 0.960、1.054、1.021）、A/A 0.985（各回 1.011、0.985、0.949）、状態は3回とも同じです。上の表は未使用の定数を消す前のビルドの値で、差は揺れの範囲です。

`body_state_v1` と `social_network_v1` はこのブランチでは持ちません。社会関係は費用が小さいものの、持っても規則が動かない間は速くならないので、規則と一緒に移します。

## 費用の内訳

同じ分岐を基準とレーンで同時に3回動かし、手順ごとにスレッドの CPU 時間を測りました（計測の枠の分だけ、上の表より遅く出ます）。

| µs/tick | 基準 | レーン |
|---|---:|---:|
| tick 全体 | 8,736、8,481、7,782 | 8,951、8,703、8,149 |
| `advance_movement` | 864、831、794 | 1,046、1,033、985 |
| `_set_stationary_position`（内数） | 388、376、360 | 534、530、504 |
| `validate_movement_runtime_state` | 91、86、79 | 86、86、80 |
| `_sync_movement_after_events` | 50、49、46 | 60、59、56 |

- tick の増分 215〜367 µs のうち、`advance_movement` が 182〜202 µs です。
- 転送は 114回/tick（480 tick で 54,725回）で、ほとんどが記録を丸ごと入れる書き込みです。
- 記録1件の書き込みは、素の dict の 820 ns に対して 3,156 ns（負荷の下の1回の値）。差の約 2.3 µs × 114回で約 260 µs となり、tick の増分とほぼ合います。
- 記録1件の読み（3項目）は 135 ns → 261 ns です。

足し算は部品の計測で、全体の差として確かめたものではありません。動かない人の位置は毎 tick 同じ値を書き直しているので、M4 で規則を移すと、ほぼ消える費用です。

## `advance_movement` の順序

`movement_shadow.reverse_advance_movement` が、人と職員を逆順に回します。

- 生成 Scenario の240分：逆順でも世界が同じです（`test_advance_movement_gives_the_same_world_in_reverse`）。
- **同じ tick に2人の列の経路を引き直すと、順序に依存します。** `_replan_physical_queue_trip` が次のイベント番号を取るので、2人の `event_id` が入れ替わります。ほかの値は同じです（`test_two_queue_replans_in_one_tick_...`）。並列にするときの障害は設計の `id_counters["event"]` と同じものです。
- **`_queue_slot_position` も順序に依存します（確かめました）。** 列の前の人の `positions` を読むので、前の人が同じ walk で歩くと、後ろの人の行き先が変わります。先頭が後ろの人の枠に立っていて、この tick に離れる状態を手で作ると、順方向では後ろの人がその枠に、逆順では次の枠に向かいます。番号だけでなく**行き先の座標が変わります**（`test_a_slot_replanned_in_a_tick_depends_on_whether_the_one_ahead_has_walked`）。並列にするときは、前の tick の位置を読むと決めるだけでは結果が変わるので、この枝を別に扱う必要があります。
- 実際の走行ではこの枝に入りません。ゲートは `advance_movement` の引き直しと無効な旅の枝（`movement.py` の 430、436、442、452 行）を1回も通らず（`--coverage`）、列の引き直し（`_replan_physical_queue_trip`）は0回です。引き直しは閉鎖の枝の1回だけです。7日分は `queue_id` のある旅も引き直しも0件です。
- **そのため、下の逆順のハッシュ一致は、歩いている人と止まっている人の順序に依存しないことしか言いません。** 上の2つの依存はゲートと7日分のハッシュでは見えず、見ているのは2つのテストだけです。

## 結果

2026-10-31〜11-01、WSL2、16コア。同じ木で動かしました（テストの追加を除く）。

| 確認 | 結果 |
|---|---|
| 7日分 | world 69aba6590f6b、events 5e4c7645ef7f、KPI 94c8cbb26041。期待値と一致 |
| `rule_shadow`（7日分） | 比較 20,160回、比べた値 13,846,748個、違い0件、taint 0件。転送 1,153,266回、丸ごとの書き込みは最初の1回。新しい旅 23,970件（部屋の中の経路 20,876 tick 分、支援 3,261、`queue_id` 0） |
| `rule_shadow`（ゲート全体） | 55シナリオすべてハッシュ一致。比較 56,820回、比べた値 12,621,981個、違い0件、taint 0件。転送 959,191回、丸ごとの書き込み 419回（419の世界の最初の1回ずつ）。旅 28,336件（部屋の中の経路 23,433 tick 分、`queue_id` 448、支援 6,391） |
| 道具の確認：行の `progress` を 1% 変えたコピー（`long_generated`） | `rule_shadow` が1分目から違いを報告。ハッシュは問題0件で、**ハッシュには見えない差です** |
| kernel `movement_positions_valid` | ゲートで受け入れ 31,716回、7日分で 10,434回。断りはどちらも0回 |
| `SHELTERSIM_MOVEMENT_REVERSED=1` | ゲート55シナリオと7日分でハッシュ一致、`rule_shadow` の違い0件（意味は上の順序の節のとおり） |
| `gate_all_paths.py --reversed-order` | 55シナリオ、問題0件 |
| `first_difference.py --both` | 55シナリオすべて違いなし。`--draws`（`generated:1500:100`）は両方にある抽選 14,652回で違い0件。Python 側だけの 783,934回は Rust の中の抽選で、ほかのレーンと同じ数です |
| `--coverage`（`domain/movement.py`） | 976行のうち 788行（81%）。この中の移動の規則は Python のままなので、行は Python で動いています |
| `tracked_mirror_gate.py` | 220行（55シナリオ × plain、tracked、`reverse_at_phase_end`、`reverse_reads`）、問題0件。7日分の `generated_project` も tracked で全10,080手順を比較 |
| 両モードの全テスト | Rust あり 2479件成功、6件 skip。`SHELTERSIM_NATIVE=0` 1772件成功、713件 skip |
| `ruff check`、`ruff format --check`、`cargo clippy`（`disallowed_methods`） | 問題なし |

このレーンで動く kernel は `movement_positions_valid` だけです。新しい規則の kernel はないので、ほかの `Unsupported` の数はこのレーンでは増えません。

## 残っているもの

- 規則（`advance_movement`、`start_movement`、`finish_movement`、`staff_position_snapshots` など）は M4 です。
- 検証の登録（`register_rust_validator`）はしていません。`validate_movement_runtime_state` は約 85 µs/tick のままです。

## 確かめていないこと

- Windows と macOS では動かしていません。
- int の座標を持つ Scenario では行が taint したままになります。結果は変わりませんが、M4 の kernel はそこで `Unsupported` になります。
