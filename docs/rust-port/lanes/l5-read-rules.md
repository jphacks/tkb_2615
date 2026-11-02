# レーン L5：読むだけの規則

手順4で Python のモデルを消す前に、Rust に道がなかった読むだけの規則を移しました。式の意味も係数も変えていません。新しい係数はありません。Python の規則は比べる基準として残します。

## 移したもの

| Python | Rust | 中身 |
|---|---|---|
| `domain/environment.py` の `zone_environment_view` | `rules/zone_environment.rs` の `ZoneInputs::conditions` | 区域の環境のすべて。修飾の並び、最後の値、騒音の最大、需要の積と上下限、出典の集合 |
| `policies/registry.py` の `ZoneEnvironmentFormula`（8つの式） | 同じファイルの `temperature_deviation`、`rest_efficiency`、`ventilation_hygiene_growth`、`privacy_discontent` | 選んだ式の出典 ID も |
| `ScenarioZoneEnvironmentConfig.model_validate`、`ZoneEnvironmentBaseline` | `read_config`、`read_baseline` | 既定値、旧版の中立の値、上下限、順序の確認 |
| `PersonContext.model_validate`、`_fields_of_person`、`_person_entry` | `rules/person_context.rs` の `parse` | 契約のすべての項目を確かめ、view が読む項目を返します。`_person_entry` は消しました |
| `_misinformation_applies` | `rules/world_view.rs` の `misinformation_applies` | 人生の状態の心理かどうか。関数は消しました。**`selected_models` は契約として Python です**（`step4-plan.md` の4節） |
| `DistanceSampledProximityPolicy.sample`、`_sample_proximity_contacts` | `rules/proximity.rs` の `sample_contacts` | 段階の手順。`SEGMENTS` と driver の両方の経路です。位置は行から、行が taint していれば木から読みます |
| `_schedule_information_contact` の Rust からの呼び出し | `rules/information.rs` の `refused_contact_event` | 行が断る接触は、同じ文の `ValueError` を、番号を取ったあとに出します。Python の呼び出しはなくなりました |

`_zone_environments` は今までどおり「`model_configs` が変わった」の合図です。kernel は同じ dict に同じ項目を書き、その `views` が同じ間は計算した値を使います。`_person_contexts` は kernel が読まなくなりました（Python の `from_world` だけが使います）。

## モデルの担当の方へ（D13）

**新しい係数はありません。** 同じ値が Rust にも書かれたものは次のとおりです。

- 既定の view（`ZoneEnvironmentView()`）：休息 1.0、判断の間隔 1 / 60、換気 0.7、照明 0.5、プライバシー 0.5、気温 20.0、衛生 0.0001、不満 0.0001、気温のストレス 0.0
- 基準の既定値：0.7、0.5、0.5、20.0
- 設定の既定値：騒音 0.5、確率 0.5 / 2.0、間隔 1 / 60、照明 0.2、換気 0.0001、プライバシー 0.0001、気温 0.0001、快適 18.0 / 26.0。旧版の4つは 0.0
- 設定と基準の上下限（`bounded_float`、`bounded_int` の引数）、`PersonContext` の長さと範囲
- 式：中点の `/ 2.0`、休息の下限 0.1、`1.0 -` の各項、プライバシーの閾値 0.4、換気の二乗
- 需要の所要時間の上下限 0.5 / 2.0（`rest`、`sleep` だけ）
- 出典 ID：式の8つ（モデルカタログと同じことをテストが確かめます）、`ASM-H19-ZONE-ENVIRONMENT-V2`、`-V1`、`ASM-PROXIMITY-CONTACT-V1`
- 優先度 40、`contact_minutes` 0、人生の状態の心理の2つの ID

**Rust だけの条件（断り）。** どれも Python の規則が動くだけで、結果は変わりません。L0 の census で、ゲートでも7日分でも0回です。Python を消す前に決める必要があるのは上の3つです。

| 断り | Python | どうするか |
|---|---|---|
| 修飾の `parameters` の int（`level: 1`）。Run は float だけを作りますが、検証は通します（`RECORD_REFUSED`）。 | 動きます | **D6 と同じ問題です。** 版を上げるときに float にそろえる案 |
| 座標のセルが i64 に入らない（約 4.6e18 m × 半径） | 大きな int で動きます | 床図では届きません。上限を決めていただくか、この断りをエラーにするか |
| `register_policy` で足した `zone_*` の式（`a zone formula of another module`） | 動きます | 組み込みの8つだけなら断りをエラーにできます |
| 前後に空白のある安定 ID（契約は取り除きます）。検証は正準の文書を求めるので通りません | 取り除いて動きます | 検証の断りのまま |
| JSON にない Python の型（`set`、`bytes` など）の人の文脈。別の種類の dict | 契約が受ければ動きます | 文書には入りません |
| 契約が断る設定、人の文脈、修飾の記録に必要な項目がない。`selected_models` が断る | 同じ条件で例外 | Python の文（L8） |

## どれを何で比べたか

| 規則 | ゲートの影（全部の影、55シナリオ） | 7日分の影 | テスト（`test_read_rules.py`） |
|---|---|---|---|
| 区域の環境（全体） | 239,760区域回、比べた値 3,836,160、違い0。view としても `world_view` の影で 228,583,660 値、違い0 | 80,640区域回、1,290,240 値、違い0 | 乱数の設定と修飾、全16通りの選択 |
| 快適帯、線形、閾値、掛け算 | 225,840区域回。**閾値で不満 > 0 は 120、快適帯でストレス > 0 は 120、騒音あり 180**（`interventions`） | 80,640区域回。**どれも既定の入口のみ：不満 0、ストレス 0、騒音 0** | 両側の値 |
| 中点、制限、二乗、比例 | 各 1,440 区域回（`model__zone_*`）。**制限に騒音あり 0** | 届きません | 同上。**制限の騒音はテストだけ** |
| 休息の下限 0.1 | **0回** | 0回 | テストだけ |
| 需要の修飾 | 120区域回 | 0 | 同上 |
| 設定を省いた既定値、基準の既定値、既定の view | シナリオは全項目を書くので、**既定値の 1% はテストだけが見ます**。既定の view は 12,480 区域回 | 0 | 省いた文書で |
| 上下限 | **テストだけ** | 0 | `test_each_bound_...` |
| 人の文脈 | 871,866人回、6,974,928 値、断り0 | 1,008,000人回、8,064,000 値、断り0 | 契約と accept / refuse まで比べる乱数 |
| 信念を読むか | True 28,850、False 2,680 | True 10,080 | 3つの心理で |
| 近接の抽選 | 180回、ペア 918、値 20,196、違い0。すべて行から（taint の木の経路は**テストだけ**） | **7日分は選んでいません。0回** | 行と木の両方、飛ばす値それぞれ |
| 接触の予定（行が断る枝） | **Run では届きません**。予定そのものは `information` の影でイベント 5,125 件、違い0 | 同左 | 負の番号で、文と番号 |

## 確認の結果

2026-11-02、WSL2、16コア。ほかのレーンとこのレーンの計測が同時に動いています（load average 5〜20）。基準は `rust/l0-refusals`（`8c47411`）です。

| 確認 | 結果 |
|---|---|
| 両モードの全テスト | Rust あり 4362件成功、6件 skip。`SHELTERSIM_NATIVE=0` 2079件成功、2289件 skip。その後に足したのはテスト1件だけで、そのファイルを流し直しました（78件成功） |
| `gate_all_paths.py --expect` | 55シナリオ、問題0件 |
| `--reversed-order` | 55シナリオ、問題0件 |
| 7日分 `--expect` | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。すべて一致。91秒、スレッドの CPU 時間 68秒 |
| `first_difference.py --both`（driver の経路、55シナリオ） | 220,892段階で digest が同じ。最終のハッシュも55個同じ |
| 影の断り（L5 の規則） | ゲートも7日分も0回 |
| `refusal_census`（ゲート、7日分。ハッシュも一致） | `zone_environment.rs`、`person_context.rs`、`proximity.rs`、`world_view.rs` で0回。**断りは増えていません** |
| `tick_budget.py`（9600分から480 tick、3回） | 基準との比 **0.950**（0.950、0.996、0.949）。A/A 0.958（0.958、1.024、0.941）。状態は同じ。load average 5〜7 |
| `ruff`、`clippy` の disallowed_methods | 通過 |
| `tracked_mirror_gate.py` | **動かしていません。** `core/state_mirror.py` を変えていないので、依頼のとおりです |

### 1% の変更

コピーで85か所を1つずつ変え（`<S>/speed/lane-l5/mutate.py`、結果は `mutants*.jsonl`）、**85か所すべてを `test_read_rules.py` が見つけました**。うち11か所は最初のテストでは見つからず、テストを足しました（上下限の2つ、リストの長さ5つ、セルの大きさ、最後の分、断る接触の2つ）。

影でも変更を入れて確かめました（`SHELTERSIM_RULE_SHADOW_ONLY=read_rules`）。

| 変更 | `model__zone*` | `interventions` |
|---|---|---|
| 中点、制限の `1.0 -`、掛け算の両方の `1.0 -`、換気の `1.0 -`、二乗、比例、気温のストレス、出典2つ | 見つけます（1,440〜16,682件） | |
| 快適帯、閾値 | 見つけません | 見つけます（120、240件） |
| 設定の既定値6つ | 見つけません | 見つけません。テストだけです |

近接は `model__social_proximity` で、距離、並び、優先度、`contact_minutes`、出典を見つけます。半径の変更は見つけません（距離がちょうど半径の近くのペアがありません）。テストが見つけます。

**driver の経路：** `first_difference.py --both` は、近接の距離（0分の `sample_endogenous_events`）、中点（0分の `update_agent_state`）、人の役割（`generated:240:30`）の変更で失敗しました。影は Python の経路を通るので、kernel が driver でも動いていることの確かめです。`parent_ids` を入れ替えた変更は `generated:240:30` では見えません（そのシナリオで空のため）。影とテストが見つけます。

### 影について

`SHELTERSIM_RULE_SHADOW_ONLY` は `rust/m6-handlers-rest` と同じ行を `sitecustomize.py` に足しました。**1人で動かすと driver が kernel を直接呼ぶので、影は1回も呼ばれず、ハッシュだけが通りました**（最初の7日分の回と、変更の最初の回）。`shadow_tick.py` の `python_path_only` が全部の影の回でしか効かないためです。`shadow_read_rules.py` は自分で `python_path_only(True)` を呼びます。**ほかのレーンの狭めた影にも同じ危険があります。** `sitecustomize` の中で呼ぶ形にするのがよいと思います（今回はしていません）。

## 費用

| 部品（100人、8区域、同時実行の1回、スレッドの CPU 時間） | Python | Rust |
|---|---:|---:|
| 8区域の環境（`view_of`。dataclass を作る分を含む） | 87 µs | 31 µs |
| 100人の文脈（`fields_of`。dict を作る分を含む） | 273 µs | 121 µs |
| 100人が同じ区域に近い世界の抽選（15分に1回） | 7,107 µs | 402 µs |

どれも入力が変わったときだけ、または15分に1回です。毎 tick の費用は `selected_models` の呼び出しと `ne` の比較で、前と同じです。

## 残っているもの

- **`selected_models`：** 契約として Python です。kernel が view ごとに1回呼びます。
- **tick の外の読み手：** `inspection/frames.py` の `zone_environment_view`、`policies/behavior.py` の `_zone_environment`。`zone_environment.view_of` を用意しました。L10 で本体をこれにします。
- **`PersonContext` のほかの使い道：** `domain/population.py`（131行、669行）、`rules/population.rs` の `context_is_plain`（生成された文書だけを受ける判定）。`person_context::parse` を使えますが、ほかのレーンの範囲なので触っていません。
- **`create_proximity_policy`、`canonical_proximity_contact_config`：** Run を始めるときの契約です。
- **検証：** `validate_zone_environment_runtime_state` は L7 と D3 の範囲です。
- **Python の規則：** `zone_environment_view`、`ZoneEnvironmentFormula`、`sample`、`_sample_proximity_contacts`、`_schedule_information_contact`、`from_world` の人の文脈と誤情報の部分は、比べる基準として残します。L10 で消します。

## 確かめていないこと、危ないところ

- **Windows と macOS では動かしていません。** 使った C API は stable ABI です。float の計算は四則と `floor`、`abs`、`mathx::hypot`、`py_max`、`py_min` だけです。
- **`DistanceSampledProximityPolicy.sample` などのクラスへのパッチは kernel に届きません。** クラスの完全一致だけを見ます。`src` と今のテストに、このテスト以外でパッチするものはありません。`_sample_proximity_contacts` のパッチは driver の `DRIVEN_STEPS` に足したので、Python の経路に行きます。
- **pydantic の断りとの一致は乱数のテストで見ています。** すべての入力を証明したものではありません。分かっている違いは上の断りの表だけで、どれも Rust が多く断る側です。
- **Python と Rust の両方が受ける枝で、`gate_all_paths.py --coverage` は動かしていません。** 届いた数は上の表の影のカウンタです。
- 木の経路（行が taint したとき）の近接はテストだけが通ります。
