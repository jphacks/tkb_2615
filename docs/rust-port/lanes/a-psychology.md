# レーン A：毎 tick の心理

P5 で残っていた、更新の段階の心理を Rust に移しました。`native/src/rules/psychology.rs` に3つの kernel があり、`experiments/phases/segments.py` が段階の手順を差し替えます。結果は変わりません。Python の規則は比較の基準として残ります。

## 移したもの

| 手順 | kernel | 中身 |
|---|---|---|
| `_psychology_update` | `update_psychology` | `explain_update`・`deltas_without_traces` の値。組み込みの5つのモデルすべて（`causal-stress-v1`、`bounded-stress-v1`、`mean-reverting-distress-v1`、`life-state-feedback-v1`、`mean-reverting-life-state-v1`）。Python で入力を作っていた `native_deltas` と `native/src/psychology.rs` はなくなりました |
| `_update_long_term_psychology` | `update_long_term` | P5 のまま。tick の初めのストレスを `WorldView` ではなく行から読むようにしました |
| `_apply_state_deltas`（新しい手順） | `apply_deltas` | `apply_state_deltas`。行、オブジェクトの属性、`body_state_v1.agents` の記録を書きます |

- **読むもの：** 人の項目（ニーズ、ストレス、信頼、活動、区域、感受性、支援の依頼の状態）は P6 の行から読みます。行にないもの（区域の環境、体調の記録、`group_id`、情報の圧力、必要な睡眠）は `AgentView` の `__dict__` から読みます。これらはレーン G が `WorldView` を移すときに行へ移せます。
- **書くもの：** 行とビットが同じ値は書きません。属性は `raw_setattr` で書くので、`__setattr__` を通らず転送も戻りません。`delta` の float オブジェクトをそのまま使います。
- **`body_state_v1`：** Rust はまだ持ちません。素の dict だけを C API で書き、それ以外（追跡する木、`ReverseReadDict`）は `Unsupported` です。持ち主を移す作業は、ここを setter に置き換えます。
- **`AgentStateDelta`：** Python のオブジェクトのまま返します。社会の伝播と長期の状態がまだ受け取るからです。これらが行で受け取れるようになれば、作らずに済みます。

## 任せるもの（`Unsupported`）

変更の前に返し、Python の手順が同じ文を出します。

- **trace：** 心理の trace を記録する tick の `_psychology_update` は Python の `explain_update` のままです。kernel は trace の文書を出しません。`apply_deltas` と `update_long_term` は trace があっても動きます。
- **Policy：** クラスの完全一致で判断します（`policy_version` ではありません）。派生クラスは規則を変えられるので Python です。式は `BodyDynamicsFormula` だけを知っています。
- **値：** 厳密な `float` でない値（`0` や `1` と書いた設定を含む）、Python の確認が断るかもしれない値、taint した行。

## 確認の結果

2026-10-31、WSL2、16コア。ほかのレーンが同時に動いていました（load average 約 10〜190）。

| 確認 | 結果 |
|---|---|
| tick の予算（9600分から480 tick、3回） | 基準との比 **0.909**（0.909、0.840、0.969）。A/A は 1.007（1.219、1.007、1.005）。世界の状態は3つとも一致 |
| `gate_all_paths.py --expect`、`--reversed-order` | 55個すべて問題0件 |
| 7日分のハッシュ | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。`--expect` と一致 |
| `rule_shadow`（ゲート全体） | 下の表。違い0件 |
| `rule_shadow`（7日分） | 下の表。違い0件、`Unsupported` 0回 |
| `tracked_mirror_gate.py`（55シナリオ × 4回） | 220行、問題0件。段階の比較は合計 662,130 回。下の「テストの影の文書」の修正のあとの結果です |
| 道具の確認（コピーで 1% 変更） | `stress_pressure` と、書き込むストレスのそれぞれを 1.01 倍にして、どちらも違いとして報告 |

| `rule_shadow` | ゲート | 7日分 |
|---|---:|---:|
| `update_psychology` が動いた / 手順の数 | 30,690 / 31,530 | 10,080 / 10,080 |
| `update_psychology` の断り | 840（すべて trace） | 0 |
| 比べた値（`explain_update` と `deltas_without_traces` の両方） | 25,666,260 | 30,240,000 |
| `apply_deltas` が動いた / 手順の数 | 31,530 / 31,530 | 10,080 / 10,080 |
| 比べた値（書き込み） | 11,286,446 | 13,104,000 |
| `update_long_term` が動いた / 手順の数 | 28,850 / 31,530 | 10,080 / 10,080 |
| 比べた値（長期の状態） | 9,903,800 | 11,337,522 |

`update_long_term` の断り 2,680回は、すべて長期の状態を持たないモデルです。ゲートで動いた `update_psychology` の内訳は、`mean-reverting-life-state-v1` 28,130回、`causal-stress-v1` 1,840回、ほかの3つが各240回です。枝に届いた人数（ゲート）は、睡眠 107,103、休息 111,700、列 136,869、支援の依頼あり 175,904、家族が別の区域 537,333、誤情報 54,341 です。シナリオが届かない値は `tests/integration/test_psychology_update.py` が乱数の入力で比べます。

## 費用

100人の生成 Run の 480分。同時に動かした3回の範囲で、スレッドの CPU 時間です（µs/tick）。

| 手順 | Python の規則 | 基準（Python で入力を作る kernel） | 今 |
|---|---:|---:|---:|
| `_psychology_update` | 1,565〜1,842 | 870〜968 | 264〜342 |
| `_update_long_term_psychology` | 361〜431 | 96〜108 | 86〜104 |
| `apply_state_deltas` | 305〜370 | 288〜312 | 68〜85 |

基準の列は別の時間に測ったので、負荷が同じではありません。

ゲートの行の網羅（kernel ありとなしの合計 / kernel ありだけ）：`policies/psychology.py` 90% / 71%、`policies/alternatives.py` 98% / 77%、`domain/psychological_state.py` 87% / 25%、`domain/distress_monitor.py` 78% / 19%。

## 残っている作業

- trace の文書を Rust で出すこと（`explain_update` の trace の部分）。今はまだ Python です。
- `create_psychological_state` と `distress_monitor_summary` は tick の外なので移していません。
- `AgentView` から読んでいる項目を行にすること（レーン G、`body_state` の作業）。
- 定数の入力（レートの表）は tick ごとに Python の dict から読んでいます。

## テストの影の文書の修正

`tests/support/tracked_mirror.py` の `Session(tracked=True)` は `WorldState.__setattr__` を置き換えていて、`world.support_requests = {...}` を `state_mirror.entities_assigned` に渡していませんでした（`domain/population.py`、`domain/population_lifecycle.py`）。行を読む規則がなかった間は見えませんでしたが、この作業で `population` と `arrivals` の6行が違いとして出ました。行が古いままになり、kernel が古い依頼の状態を読んだためと見ています（直して全部が一致したことで確かめました。古い行そのものは見ていません）。本番の `__setattr__` は正しく受けるので、本番の結果には関係しません。**行を読む kernel を入れるほかのレーンにも同じ修正が要ります。**

## 確かめていないこと、危ないところ

- **Windows と macOS では動かしていません。** 使った C API（`PyObject_GenericSetAttr`、`PyDict_GetItemWithError`、`PyDict_SetItem`）は stable ABI です。
- **view と行が同じことに頼っています。** kernel は `view` の人の項目を読まず、同じ tick の行を読みます。view を作ってから kernel までに人を書くコードは今はありません。`WorldView.from_world` を差し替えて値を変えるテストや、手順に別の view を渡すコードは kernel に気づかれません。`rule_shadow` は、ゲートと7日分でこの一致も確かめています（kernel は行、比べる Python は view を読むからです）。
- **`deltas_without_traces` や `explain_update` への monkeypatch は kernel に届きません。** Python の規則を動かしたいテストは、`OfflineSimulationController._psychology_update` を `UpdatePhase._psychology_update` に戻します。`@pytest.mark.python_rules` はまだありません。
- **モデルの担当の方に見ていただくコード：** `policies/psychology.py` の `SimplePsychologyPolicy`、`MeanRevertingDistressPolicy`、`LifeStatePsychologyPolicy`、`IntegratedDistressLifeStatePolicy`、`sleep_debt_step`、`_mean_reverting_terms`、`policies/alternatives.py` の `BoundedStressPolicy`、`policies/registry.py` の `BodyDynamicsFormula.stress_feedback` の計算が `rules/psychology.rs` の `step` に入りました。係数と式の意味は変えていません。
