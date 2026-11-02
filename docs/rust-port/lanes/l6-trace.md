# L6 レーン：trace の文書と Provider を driver に

trace つきの Run と、Provider を呼べる router の Run が、kernel と driver で動くようになりました。**結果は変わりません。係数は1つも増えていません。** 決定 D1 のとおり、文書の形は変えていません。Python の規則は手順4まで比較の基準として残ります。

## 大きさ（最初の見積もり）

計画は「他の 10 倍かもしれない」と警告していましたが、**そうなりませんでした。** Rust は約 1,200 行増（`rules/trace.rs` 660 行、残りは `psychology.rs` などの差分）です。小さく済んだ理由は2つです。

- **書き出しを移していません。** ファイル、spool、`_sanitize`、時間ごとの集計は `inspection/traces.py` のままで、Rust は同じ Python の dict を渡します。モデルの規則ではなく、D1 の範囲（`to_dict`、`explain_update`、接触の trace）にも入っていません。bytes が同じになる理由もここにあります。**手順4で書き出しも Rust にするなら、それは別の作業です。**
- trace の値の大半は、kernel がもともと途中で計算していました。

## 移したもの

| もの | 元の Python | Rust |
|---|---|---|
| `AgentPsychologyTrace` の値（5つのモデル） | `explain_update` 一族 | `psychology.rs` の `Stages`、`person_trace`。`step` は trace のある tick だけ途中の値を残します |
| `to_dict`、`trace_document`、寄与の一覧 | `policies/psychology.py`、`_record_psychology_trace` | `trace.rs` の `person_document`、`record_psychology_trace`（手順として `SEGMENTS` に足しました） |
| 接触の trace（3つのモデル） | `SocialContactTrace`、`policies/social.py`、`alternatives.py` | `social_propagation.rs` の `plan`、`trace.rs` の `social_document` |
| 情報の接触の trace | `InformationSpreadResult.trace_document` | `information.rs` の `trace_document`。`traces` はもとからあり、並べ替えを足しました |
| 判断の文書 | `_decision_document` | `decisions.rs` の `decision_document`。受け取りは新しい `RunArtifactWriter.record_decision_document` |
| `TraceSink` | | `trace.rs`。sink の `record_*` に渡すだけです |

- **`TraceRows`：** `PsychologyUpdateResult.traces` に入る行です。Python の手順や比較が読むと `AgentPsychologyTrace` を作ります。
- **engine の完全な trace（`TraceLevel.FULL` の `TickTrace`）** は `phase_observer` に載せたので driver で動きます。
- **Provider：** `accept` の断りを消しただけです。hook（`_prioritize_support_request`、`_consider_social_contact_event`、`_consider_due_complaints`）はもとから kernel から同じ場所で Python を呼んでいます。LLM のコード、スキーマと意味の検証、応答テープは1行も変えていません。
- **`tick_probe`：** 常に `phase_observer` で見ます。`on_driver` はなくしました。**`--reversed-order` の段階の終わりの回も driver を通るようになりました**（M6 の文書の「通らない」は解消）。`within_phase` の回は手順を包むので、今も Python の経路です。
- **`PhaseProfiler`：** `phase_observer` の `observe` にしました。**ただしどこからも使われていません**（定義だけの dead code）。M6 の文書の「`measure_performance` の Run は `PhaseProfiler` で Python の経路」は誤りで、`measure_performance` は `RunTelemetryStore` で、もとから driver です。段階が例外を出すと、その回は数えません（前は `finally` で数えていました）。

## 断りの回数（前 → 後）

| 理由 | ゲート | 7日分 |
|---|---:|---:|
| `a Provider can be called`（`accept`） | 1,800 → **0** | 0 → 0 |
| `a trace is recorded`（`accept`） | 840 → **0** | 0 → 0 |
| `a trace is recorded`（`update_psychology`） | 840 → **0** | 0 → 0 |
| `the sink is Python's, or the run keeps a trace`（`record_outcome`） | 696 → **0** | 0 → 0 |
| `a trace records the contacts`（`inputs`） | 480 → **0** | 0 → 0 |
| `a trace needs the Python contact records` | 480 → **0** | 0 → 0 |
| driver が動いた tick | 28,892 → **31,532**（段階の数 31,530 のすべて。差の2は段階の前に失敗する回です） | 10,080 → 10,080 |

kernel の断りの合計はゲートで 6,280 → 6,847 に増えました。trace と Provider の tick が driver に来て、別の理由（`a free worker can take a request` など）が数えられるようになったからで、増えた分は他のレーンの理由です。7日分は 720 のままです。

## Python に残した規則

比較の基準で、手順4で消します：`explain_update` 一族と `deltas_without_traces`、`AgentPsychologyTrace` と `to_dict`、3つの `trace_document`、`_record_psychology_trace`、`_decision_document`、`record_decision`。**`TraceRows.__iter__` は `AgentPsychologyTrace` を読むので一緒に消します。**

kernel が今も Python に任せる条件（ゲートではすべて0回）：Python が作った view（`partial`）で trace がある、`traces` が Python のもの、人の最後の delta がない、合計が有限でない、知らない trace level、`decision_sink` のない sink。**手順4で Rust の道か、断らないことの確認が要ります。**

**残すもの：** `RunArtifactWriter`（ファイル、spool、`_sanitize`、`_accumulate_psychology_contributions`）、LLM の境界。

## モデルの担当の方に見ていただくコード

**新しい係数、新しい Rust だけの上限はありません。** 式の意味も変えていません。`step` は同じ式を `stress_delta` に切り出しました（計算の順序は同じです）。trace の項目の組み立てが2か所になりました：

| Python | Rust | 中身 |
|---|---|---|
| `explain_update` の `replace` | `person_trace`、`revert_to_mean` | 各段階が残す項目、`base_stage_clip`、`BoundedStressPolicy` の `scale`（最初の段階の成分と delta を残す点を含む） |
| `to_dict` | `person_document` | 直接の原因、成分の上書きの順序 |
| `StressPropagationPolicy`、`ExpressivenessChannelOpennessPolicy`、`DeGrootContactPolicy` の trace | `plan`、`channel_stress`、`degroot_stress` | `stress_difference`（Sparse は下限0、他は符号つき）、`aggregate_scale`、残りの cooldown |
| 文字列の定数 | `trace.rs`、`decisions.rs` | `ASM-TOILET-SERVICE-DURATION-V1`、`ASM-H02-RESOURCE-SERVICE-V1`、`ASM-STAFF-SUPPORT-LIFECYCLE-V1`、`ASM-PRIORITY-RULE-BEHAVIOR-V1`、`ASM-SLEEP-AWARE-BEHAVIOR-V2`、`ASM-UTILITY-BEHAVIOR-V1`、要約の 20 件 |

## 結果

2026-11-02、WSL2、16コア、ほかのレーンと同時です（load average 約 5〜78）。

| 確認 | 結果 |
|---|---|
| 全テスト | Rust あり 4,406件成功、6件 skip。`SHELTERSIM_NATIVE=0` 2,079件成功、2,333件 skip（`821319e` の前の木。その後に足したのは Rust のないテスト1つと benchmarks です） |
| `gate_all_paths.py --expect`（計測あり、なし） | どちらも 55シナリオ、問題0件 |
| `--reversed-order` | 55シナリオ、問題0件。段階の終わりの回（`reversed_order`）は driver を通り、段階の途中の回は Python の経路です |
| 7日分 `--expect`（driver、計測あり） | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。一致 |
| **`first_difference.py --both`：55シナリオすべて** | 段階の前後の digest 220,892か所、違い0件。`trace_levels` 8,826か所、`llm_tape` 12,606か所を含みます |
| **`trace_bytes.py`：55シナリオ** | 違うファイル0件 |
| **`trace_bytes.py --models`：40モデルすべてを完全な trace で** | 違うファイル0件。全 tick が driver |
| **`trace_bytes.py --traced`：ゲートの library の Run すべてを完全な trace で** | 違うファイル0件。比べた trace：心理 996 MB、情報 303 MB、判断 74 MB、接触 39 MB、時間ごと 4 MB、LLM の呼び出し 6 KB、社会の判断 115 KB |
| `tracked_mirror_gate.py` | **動かしていません**（指示によります）。`core/state_mirror.py` は変えていません |
| `ruff`、`cargo clippy`（`disallowed_methods`） | 問題なし |

**予算：** `tick_budget.py`、`fork-b1` の 9600分から 480 tick、5回、load average 約 50〜78。**1.018**（1.018、1.020、1.016、0.977、1.041）。A/A 1.015（1.047、0.846、1.068、1.015、0.964）。状態は5回とも一致。A/A の揺れの中で、これより細かくは言えません。

**trace つきの Run の速さ**（100人、480分、スレッドの CPU 秒、基準とレーンを交互に2組。load 約 50）：

| | 基準 | レーン |
|---|---:|---:|
| off | 3.4 | 3.1〜3.3 |
| summary | 18.3〜19.9 | 7.3〜7.4 |
| full | 31.2〜31.5 | 20.3〜20.4 |

**影（ゲート、Python の経路。driver は通りません）：** 違い0件。

| | 回数 |
|---|---:|
| `_psychology_update` の行を `explain_update` と比較 | 13,920行、値 696,000 |
| 心理の文書と寄与 | 840回、値 1,089,392、40 MB |
| 接触の文書 | 478回、値 48,994、1.5 MB |
| 情報の文書 | 480回、値 286,570、7.6 MB |
| 判断の文書 | 696回、値 36,504、1.2 MB |

届いた枝（ゲートの影）：原因 `unmet_need_pressure` 13,920、`staff_support_wait` 4,230、`active_false_claim_belief` 2,442、`facility_queue_wait` 622。部分 `life_state` 9,600、`environment_conditions` 9,600。**騒音の部分（`zone_environment`）は影でも `--traced` でも0回です。** `test_the_trace_of_a_noisy_zone_is_the_python_rule_s` だけが比べます。モデルは影で `causal-stress-v1`、`mean-reverting-life-state-v1` の2つです。**残りの3つと、社会の2つ、情報の2つは `--models`（bytes）とテスト（乱数の値）で比べます。**

**7日分の影（自分の分だけ。`SHELTERSIM_RULE_SHADOW_ONLY=tick,decisions,update,information`、Python の経路。trace はありません）：** 同じ3つのハッシュ、違い0件、`Error in sitecustomize` 0件。`_psychology_update` 10,080 / 10,080、社会 10,079 / 10,080（残り1回は `the namespace has no row`、L1 の分）、情報 10,080 / 10,080、`record_outcome` 87,464 / 87,464。比べた値は心理 30,240,000、書き込み 13,104,000。**7日分は trace の文書を比べていません**（文書を作らないので比べる相手がありません）。上の数字は `step` を切り出しても trace のない経路が変わらないことの確認です。

**ハッシュは trace を見ません。** 下の変更でゲートは「問題0件」でした。

**感度（コピーで1回に6つ、値を 1.01 倍）：**

| 変更 | 見つけたもの |
|---|---|
| 心理の `need_pressure` | 影の行の比較（`traces of`）、`trace_bytes`、テスト |
| 判断の `selected_probability` | 影（判断 696件すべて）、`trace_bytes`、テスト |
| 情報の `transmission_probability` | 影、`trace_bytes`（`--models` を含む）、テスト |
| Sparse の `stress_difference` | 影、`trace_bytes`（`--models` を含む）、社会のテスト |
| Channel の `influence_weight` | `trace_bytes --models`、社会のテスト（4件すべて） |
| DeGroot の寄与 | `trace_bytes --models`、社会のテスト（4件すべて） |

後ろの4つは、前の2つと分けてもう一度作って確かめました。Channel と DeGroot はゲートの trace の回には出ないので、影と `trace_bytes`（`--models` なし）では見えません。

## Provider の確かめ方

`test_a_router_that_can_call_a_provider_runs_on_the_driver_as_through_the_phases`：台本の Provider（有料の API は呼びません）で、`record` の Run を driver と Python の経路で動かし、保存したもの全部、hook の順と引数、Provider に渡した入力が同じことを見ます。テープだけの `replay` も同じ結果です。スキーマに合わない応答は、どちらの経路でも fallback に落ち、その値は世界に入りません。ゲートの `llm_tape` は `first_difference` と `trace_bytes` の両方を通っています。

## 確かめていないこと、危ないところ

- **Windows と macOS では動かしていません。** 足した C API はありません。
- **Rust の行の網羅は測っていません。** 上の枝の回数で見ています。
- **騒音の部分は、1つのテストだけが比べます。**
- **`events_shadow` は違い 1,100件を出します（プロセスごとに上限20）。このレーンの前からです**（L5 の影の回も同じ 1,100件）。kernel が Rust で列から取り出した予定を比較用の双子が知らないためと読んでいますが、直していません。列の比較はこの影では効いていません。
- **本物の Provider では動かしていません。** 台本とテープだけです。hook は Python の同じ関数です。
- **最初の予算の回は無効でした。** `git archive` の古い mtime で cargo が基準の木をレーンのコードのまま作っていました。基準を作り直して測り直したのが上の数字です。ほかのレーンも `target` をコピーするときは気をつけてください。

## レーンとの重なり

`rules/tick.rs` の2か所、`rules/decisions.rs`、`rules/world_view.rs` の `pub(crate)` 1語、`experiments/phases/segments.py`、`driver.py` の説明、`update.py` のコメント、`core/engine.py`、`inspection/traces.py`、`benchmarks/tick_probe.py`、`first_difference.py`、`rule_shadow/sitecustomize.py`、`tests/support/python_rules.py`、`tests/integration/test_tick_driver.py`。**他のレーンへ：** `tick_probe.install` の `on_driver` はなくなりました。L2、L3、L4 の `events/handlers.py`、`care_receipts.py` には触っていません。
