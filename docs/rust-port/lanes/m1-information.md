# M1 レーン C：情報の伝播（`information_spread_v1`）

標本の段階の手順 `_sample_information_contacts` を、メッセージがある場合も含めて Rust に移し、名前空間を Rust が持つようにしました。結果は1バイトも変わりません。

## 移したもの

| もの | 中身 |
|---|---|
| `native/src/state/information.rs` | 行（メッセージ、信念、接触、問い合わせの圧力、支援の依頼、カウンタ。すべて型付きで、自由な形の部分はありません）と検証の kernel |
| `native/src/rules/information.rs` | `sample_contacts`（手順そのもの）と `planned_contacts`（比べるため。何も変えません） |
| `experiments/phases/sample.py` | Python の手順を比べる基準として残し、イベントの予定を `_schedule_information_contact` に分けました。kernel もこれを呼びます |
| `benchmarks/rule_shadow/information_shadow.py` | 毎回、抽選1つずつまで Python の規則と比べる道具 |
| `tests/integration/test_information_row.py` | 行、taint、転送、丸ごとの書き込みがないこと、任せる場合、乱数の入力でのビット一致 |

`InformationSpreadPolicy.native_update` と `information_contacts_without_messages` は、要らなくなったので消しました。

**モデルの担当の方へ（移したモデルのコード。係数も式の意味も変えていません）：**

- `policies/information.py`：`update`（接触の抽選、接触の分数、伝える主張の選び方、`planned_pairs`）、`_transmission_probability`、`_sender_sharing_factor`、`EvidenceAwareInformationPolicy.update`（共有の傾向の減衰と忘却）
- `policies/alternatives.py`：`HazardInformationPolicy._transmission_probability`（`-expm1(-p)`）

## 仕組み

- **kernel が見る方策：** `InformationSpreadPolicy`、`HazardInformationPolicy`、`EvidenceAwareInformationPolicy` のちょうどその型だけです。派生クラス、`CounterRNG` でない乱数（`policies/information.py` の抽選）、trace のある Run は `Unsupported` で Python の手順が動きます。
- **読むもの：** 人は `entities.rs` の行から読みます（ID、区域、`trust`、`stress`、`profile.group_id`）。世帯は `WorldView` と同じく `population_presence_v1.people` の `household_id` を優先します。この名前空間はまだ Python のものなので、誰かが信念を持つ tick にだけ dict を読みます。
- **書き込み：** 問い合わせの圧力、カウンタ、信念の項目は値ごとに書きます（同じビットなら書きません）。接触はレーン B が足した `refill_record_map` で詰め直すので、前の記録の節点を使い回します。**名前空間を丸ごと書くことはありません。** 接触の記録への別名を tick をまたいで持つと、新しい値が見えます（`src` にそのようなコードはありません）。
- **Unsupported の条件：** Python の規則が解析する値（`TypedMessage`、`AgentInformationState`）が1つでも通らないとき、信念の主張が `messages` にないとき、人に信念や圧力の項目がないとき。Python の規則が実際には読まない値でも断るので、Python より広く断ります。結果は変わらず、遅くなるだけです。
- **検証：** `validate` は `validate_information_runtime_state` のすべての規則を行で確かめ、全部通るときだけ true を返します。schema 1 の状態と、escape の要る接触のキーは Python に任せます。

**キーの順序：** JSON からの写し（分岐など）で、信念の記録のキーはソート順になります。Python の規則は毎 tick その記録を `to_dict` の順で作り直しますが、Rust は値だけを書くのでソート順のままです。ゲートの 1,070 tick で、この順序だけが違いました（`another key order in beliefs`）。値はすべて同じで、保存は `canonical_json` でキーを並べるので byte 列も同じです。接触のキーの順序は Python と同じです（テストで確かめます）。

## 残っているもの

- 名前空間の Python の書き手：`events/handlers.py` の `InformationRumorHandler`、`InformationCorrectionHandler`、接触の適用と反応、`propose.py` からの `record_support_request`、`domain/population.py`、`domain/population_lifecycle.py`。1項目ずつ転送されます。読み手は `WorldView`（`query_pressure` を毎回読むので、切り離しはまだできません）など。イベントのレーンとともに移します。
- `information_spread_summary`（tick の外）、`upgrade_information_runtime_state`（schema 1。行に入らないので taint し、Python の規則が丸ごと書いたときに行が戻ります）。
- trace つきの Run の Rust 化（`TraceSink`）。

## 計測

2026-10-31、WSL2、16コア。ほかのレーンと同時に動かしました（load average 80〜140）。

**tick の予算**（`tick_budget.py`、9600分から480 tick、3回、スレッドの CPU 時間）：基準との比 **0.974**（0.974、0.985、0.934）、A/A は 0.995（1.054、0.912、0.995）。世界の `state_digest` は3つとも一致しました。この Run にはメッセージがありません。

**部品の費用**（100人、360分、メモリ上、スレッドの CPU 時間、同時実行の1回）：

| 部品 | 基準 | このレーン |
|---|---:|---:|
| 手順、メッセージなし | 228 µs | 201 µs |
| 検証、メッセージなし | 217 µs | 33 µs |
| 手順、噂あり（`with_rumors`） | 44,930 µs | 801 µs |
| 検証、噂あり | 2,156 µs | 158 µs |
| tick 全体、噂あり | 61,503 µs | 15,103 µs |

基準の手順は名前空間を持っていないので、ここに丸ごとの書き込みの費用（P3 の計測で約 300 µs）は入っていません。持ち主だけを宣言した場合の費用は測っていません。

## 結果

| 確認 | 結果 |
|---|---|
| `rule_shadow`（ゲート全体、55個） | 手順 31,530回：kernel 28,890回、方策なし 2,160回、trace の Run で Python 480回。`Unsupported` はこのほかに0回。比べた値 48,186,146個、違い0件。抽選 8,064,697個（相手 7,404,967、伝達 61,279、忘却 598,451）、確率 37,179個、接触 2,441,644件、イベント 5,125件、忘却 962件。メッセージがある tick は 10,932。3つの方策すべて |
| `rule_shadow`（7日分） | kernel 10,080回、`Unsupported` 0回、比べた値 27,228,856個、違い0件。抽選 2,095,108個。この Run にメッセージはありません |
| 検証の kernel | ゲートで 32,201回、7日分で 10,431回受け入れ、断りは0回。受け入れるたびに Python の検証も通りました |
| 道具の確認（コピーで kernel を変更） | 基礎の確率を 1% 変えると、影が3シナリオで違いを報告し、ゲートのハッシュも失敗。忘却の確率を 1% 変えると、**ゲートは通り**（比較が裏返らないため）、影だけが報告しました |
| `gate_all_paths.py --expect` | 55個、問題0件 |
| `gate_all_paths.py --expect --reversed-order` | 55個、問題0件 |
| `tracked_mirror_gate.py` | 220行、問題0件 |
| 7日分のハッシュ | world `69aba6590f6b`、events と KPI も `--expect` と一致 |
| テスト | 拡張あり 2350件通過、6件スキップ。`SHELTERSIM_NATIVE=0` 1771件通過、585件スキップ |

`test_first_difference.py` の記録は `SHELTERSIM_NATIVE=0` にしました。Python の規則に入れたバグを見つけるテストで、kernel が動くと Python の規則に届かないからです。

## 確かめていないこと

- Windows と macOS では動かしていません。使った C API は stable ABI で、`PyDict_Clear` と `PyDict_Next` はレーン B と同じです。
- `WorldView.from_world` を作らないので、壊れた状態でそこが出す例外（知らない区域の `KeyError` など）は kernel の経路では出ません。各段階の終わりの検証を通った状態だけが来る前提で、メッセージがない間の前の kernel と同じです。
- 方策の型は確かめますが、クラスのメソッドを書き換えるテストには気づきません。`src` と今のテストにはありません。
- メッセージのある経路の予算は測っていません。7日の分岐にメッセージがないためです。部品の表の「噂あり」だけです。
