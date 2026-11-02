# L8c レーン：保存されるイベントの文の残り

L8a（`l8-texts-events.md`）が残した `EventSuppressed`、`EventApplicationError` の文（イベントの記録の `detail` に保存される文）を Rust に移しました。もう1つ、水・食事の依頼を列が断ったときの `ValueError` の文（依頼の記録の `detail`）も移しました。**結果は変わりません（ハッシュ、ゲート、7日分は同じ）。係数と式は触っていません。** 道具は L8a のもの（`core/errors.rs` の出口、`text_check.py`、`kernel_says_python_s_text`）をそのまま使いました。

作業は6つに分けて並べて進め、この枝でまとめました（効果、ガードと職員の指示、人口、社会、資源のサービス、歩きの完了）。

## 移したもの

か所は `src` の `raise EventSuppressed/EventApplicationError`（AST）です。

| 範囲 | Python | Rust | 移した | 残した・届かない |
|---|---|---|--:|---|
| 環境、床図、一時空間、構造のハンドラー（`events/effects.py`） | 54 | `rules/effects.rs` | 49 | 届かない5（下。うち3つは Rust に Python の文の検査があります）。構造の `staff_task_order` の2つを含みます |
| ガード（`_evaluate_guard_node`、`should_apply_effect_guard`、`effect_event_guard_or_suppress`） | 7 | `effects.rs`、`dispatch.rs` | 6 | `guard field ... is not implemented` は届きません（下） |
| `order_staff_operation`、`validate_staff_operation_order` | 15 | 新しい `rules/staff_order.rs` | 14 | `order_staff_operation` の1つは直前の検査と同じ条件で、届きません |
| 職員が使えなくなった抑止（到着、完了） | 2 | `staff_operation_events.rs` | 2 | 作業を止めてから抑止します（Python と同じ書き込み。`AFTER_CHANGE` に追加） |
| `domain/population.py`、`population_lifecycle.py` | 58 | `population.rs`、`population_lifecycle.rs` | 47 | 前のレーンから Rust が8。届かない6（下。うち3つは Rust に文があります） |
| `SocialInteractionHandler` | 32 | `social_interaction.rs` | 32 | なし |
| `ResourceServiceManager`（`resource_service.py`） | 21 | `service.rs` | 19 | `resource consume rejected: {exc}`、`resource reservation release rejected: {exc}`（下） |
| `AgentTravelCompletionHandler` | 16 | `travel_completion.rs` | 15 | `_after_arrival` の `str(exc)`（下） |
| 包み（`ValueError` を `EventApplicationError(str(exc))` に） | 3 | `handlers.rs`、`toilet.rs` | 2 + 一部 | 人口のハンドラーの包みは一部（下） |
| `ResourceServiceCompletionHandler` の v6 の検査 | 1 | `dispatch.rs` | 1 | 古い版ではありませんでした（下） |
| `upgrade_information_runtime_state` の包み | 1 | | 0 | 古い版（v1）の状態だけ。L9 の範囲 |
| `_apply_operational_transaction` | 1 | | 0 | 届きません（L8a のとおり） |
| 依頼を列が断った `ValueError`（`detail`） | — | `service.rs`、`proposals.rs` | 水・食事 | トイレは例外がそのまま出ます（L3 のとおり。保存されません） |

### 決めたこと、気を付けたこと

- **検査の順序。** Python の規則が `or` で並べる検査は、Rust でも同じ順で、前が通ったときだけ次を読みます。社会的な行動（毎 tick の道）は、人の照合が先にあったのを Python の順に並べ直しました。成功の道は読むものも書くものも同じで、人の照合は失敗するときだけ前に足します（成功の道の比較：`first_difference` と、kernel の測りで差が揺れの中）。
- **書いてから投げるもの。** dispatcher は巻き戻しません（`dispatch_one`、`dispatch.rs` を読みました）。
  - 歩きの完了：L8a の「多くは書いた後に投げる」は `apply` には当たりませんでした。18 か所すべてが最初の書き込み（寝床の位置）より前です。Rust は書く前に決め（`decide`）、印で区別して比較に出します。
  - 職員が使えなくなった抑止：Python と同じく作業を止めて（`interrupt_operation`）から抑止します。
  - 人口：到着の先送り、`profile_context` の更新、`approach_completion_event_id` の削除など、Python が書いてから投げるものは同じ書き込みをしてから投げます。テストは2つの世界の文書が同じことも確かめます。
- **Python の呼び出しの文。** ハンドラーが `str(exc)` で包む呼び出し（`apply_geometry_change`、`register_temporary_space`、`place_installed_facility`、pydantic の契約、`apply_resource_operation`）は、Rust の規則も同じものを名前で呼び、Python と同じ例外のクラスだけを包みます。文は呼び出し先のものなので同じです。
- **ガードの道。** これまで印（`effect_program_v1`）のあるイベントは dispatch が丸ごと Python に渡していました。今は dispatch がガードを Rust で確かめ（`guard_or_suppress`）、偽なら抑止、読めないガードや対象のないガードは失敗を Python の文で記録し、真なら印のないイベントと同じく `others` のハンドラーを呼びます。表の6種類、ハンドラーのない種類、Rust が決められないガード（64 bit を超える int）は今までどおり Python です。ハンドラーの中のガード（直接呼んだとき）も同じ文を出します。
- **職員の指示。** `order_staff_operation` に Rust の道ができました（`staff_order.rs`）。`validate_staff_operation_order` の14の検査を同じ順と文で持ち、成功の書き込みも同じ順です。Python の `validate_staff_operation_order` は `validate_effect_program`（残るコード）のために残します。
- **Rust が決められない値**（`str` の派生型、64 bit を超える int、Python の `==` が要る別の型）は、その検査より前で `Unsupported` のままです。Python の規則が同じ文を出します。

### 結果の外で変わったこと（壊れた入力だけ）

- 人口：`queue_arrivals`、`queue_departures` に明示の `None`（`household_size` など）を渡すと、これまで Rust は既定値で成功し、Python は失敗していました。今は Python と同じ文で失敗します。コンパイラーは値を必ず入れるので、有効なプログラムでは起きません。人口の名前空間が `None` のときも同じです。
- 歩きの完了：`from_zone_id` が `None` で経路が `[5]` のような作った入力で、Rust が通していた移転を Python と同じく断ります。
- 構造：`add_staff` などの数が i64 の上限近くのとき、あふれずに Python と同じ「超える」の文になります。

## 届かないもの（テストなし）

| か所 | 理由 |
|---|---|
| `effects.py` `zone demand action requires resource-service policy` | 予定するときに断られ（`effects.py:1265`）、Run の間に名前空間はなくなりません（なくすと検証を通りません） |
| `condition modifier requires zone-environment-v2` | 予定するときに v1 では断られます（`:1178`）。設定は Run の間変わりません |
| `temporary-space effect is already expired` | `expire_temporary_space` が偽を返すのは、直前の検査で除いた場合だけです |
| `temporary-space operator differs from its approved program` | 演算子と op を直前に両方 `add_temporary_space` と確かめています |
| `staff handoff` の完了の記録がない | そういう世界は `validate()` を通らず、Python の写しが先に `StateInvariantError` を出します |
| `guard field ... is not implemented` | `GuardField` は7つの Literal で、知らない欄は「無効なガード」になります |
| `order_staff_operation` の最後の検査 | `validate` の最後の検査と同じ条件、同じ分で、先にそちらが出ます |
| `staff_operations.py:743`、`759` | tick からは pydantic の `StaffTaskOrderParameters` が先に断ります（直接の呼び出しでテストしています） |
| 人口 `population_lifecycle.py:494`、`503`、`667` | 契約が世帯を要求する、名前空間がないと写しが先に失敗する、検証が禁じる状態（Rust の検査は Python の文で持っています） |
| 人口 `:783`、`:848`、`:890` | 状態の検証、ID の型、契約の `zone_id` で先に決まります。Rust は `Unsupported` のまま |
| `_apply_operational_transaction` | Rust のハンドラーは `EventLogRecord` しか作りません |

## 残したもの（手順4で要るもの）

- **`resource consume rejected: {exc}`、`resource reservation release rejected: {exc}`（`resource_service.py:580`、`674`）：** 中の文は資源台帳の規則の `ResourceLedgerError`（L8b の範囲）で、Python は連番を取ったあとに投げます。Rust は書く前に `Unsupported`、Python の規則が出します。台帳が `validate_resource_ledger_state` を通らないか、設定の量が正でないときだけです。
- **`_after_arrival` の `str(exc)`（`handlers.py:2217`）：** トイレの依頼の `ValueError` で、歩きの書き込みの後に出ます。今は `toilet::queue_up` がすべての断りを `Unsupported` にし、kernel が Python の `_after_arrival` を名前で呼ぶので、文は Python のものです。トイレの依頼の kernel が `ValueError` の文を印付きで出すようになれば、`arrive` の小さな包みで閉じます。
- **人口のハンドラーの包み（`handlers.py:1843`）：** 引数の `tuple()` の2つは移しました。規則の中の文（pydantic の検証の文など）は、kernel が断ってから Python のハンドラーが動いたときだけ包みに届きます。kernel の計画の中のエラーは今も `Unsupported` です。
- **資源台帳の包み：** `ResourceLedgerHandler` は Rust の台帳が断ると Python の `apply_resource_operation` を自分で呼び、`ResourceLedgerError` を `errors::failed(str(exc))` にします。包みは Rust ですが、中の約20の文は Python（L8b の範囲）です。
- **トイレの完了の `StateInvariantError("toilet service staff reservation is inconsistent")`：** 書いた後に出るので、Rust は書く前に `Unsupported` のままです（保存される文ではありません）。
- **`upgrade_information_runtime_state` の包み：** v1 の情報の状態だけ。L9。

## 比べ方と結果

すべて最後のコード（`origin/feature/rust-port` の L8a、L9 をマージした後）で、比較を入れた状態です。ログは `speed/lane-l8c/final3/`（ゲート、7日分、`first_difference`、予算）と `final4/`（全テスト。`final3` の後にテストだけ直したため）。

| 確かめたこと | 結果 |
|---|---|
| 全テスト、比較あり（`-p text_check`、`SHELTERSIM_TEXT_CHECK_STRICT=1`） | 4,963 件成功、8件 skip |
| 全テスト、`SHELTERSIM_NATIVE=0` | 2,267 件成功、2,704 件 skip |
| ゲート（`--expect`、比較あり、strict） | 55 Scenario、問題0件 |
| ゲート（`--reversed-order`） | 55 Scenario、問題0件 |
| 7日分（`--expect`、比較あり、strict） | world `69aba6590f6b`、events `5e4c7645ef7f`、KPI `94c8cbb26041`。一致 |
| `first_difference.py --both` | interventions 33,930、operational 7,005、population 14,920、arrivals 12,656、geometry 16,632、split_queues 4,202、conditions 3,782、`generated:1440:100` 10,081 の段階ですべて同じ |
| 予算（`tick_budget.py`、`fork-b1` の 9600分から 480 tick、5回。基準は `b76bee1` を別の target で作ったもの） | **1.004**（0.971、1.004、1.105、1.032、0.942）。A/A 1.014。上限 1.10 以内、状態は同じ。load average 5〜70（ほかのレーンと同時） |

**比較の回数（Rust が出した文を Python の規則と比べた回数。違い0）：** テスト 22,867、ゲート 7,015、7日分 3,447。Rust の出口 341 のうち 282 がどれかの比較で同じでした。比べていない 59 は、上の「届かないもの」に Rust の検査を残したもの、kernel を直接呼ぶテスト（`test_effect_kernels.py`、`staff_order` の直接の呼び出し）だけが通るもの、`ValueError` の出口（依頼を列が断った文。道具は `EventSuppressed`、`EventApplicationError` だけを数えます。テストで比べています）です。

**Run で Python がまだ文を出すか所：** ゲートで `validate_staff_operation_order` の2か所（各1回）だけです。これは tick ではなく、介入を予定するときの `validate_effect_program`（残るコード、D3）が呼んだものです。tick の道は `staff_order.rs` です。7日分は0です。L8a のときゲートで Python が出していた `ResourceServiceManager.complete`（取消し済み）、`effect_event_guard_or_suppress` は Rust になりました。

**感度：** 3つの文を1文字ずつ変えた木（`zone environment target does not exists`、`resource service task ... was canceled`、`social action reason code is not alowed`）で、テストは4件失敗し（それぞれの文のテスト）、ゲート（interventions、split_queues）は比較で違い1件、ハッシュは `interventions/add-staff` の world と events で違いました。

**kernel が動いた回数と断った回数：** 文で見ると、Rust が出した文はテスト 29,457、ゲート 7,279、7日分 3,447 回で、Run で Python が比較の外で出したのはゲートの上の2回だけ（予定のときの検証）です。テストの「比較の外」の回数は、テストが Python の規則を写しの上でわざと呼ぶもの（比べる側）です。dispatch の印のあるイベントのガードの数（`rules("dispatch").stats()` の `guard_passed`、`guard_suppressed`、`guard_failed`、`guard_refused`）は、テストでは確かめましたが、ゲートと7日分では集めていません。7日分に印のあるイベントはありません（`m6-effects.md`）。
