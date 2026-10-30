# アーキテクチャ

## コンポーネントの流れ

~~~text
CLI / ローカルHTTPサーバー
        |
Project、Scenario、Inspector、Demoサービス
        |
Scenario + Policy設定 -> WorldState -> 決定的tick engine
        |                            |                |
地域・Provider adapter             |             EventQueue
任意LLM adapter                    |             EventLog
        |                            |
ProjectStore / TimelineStore <- Checkpoint / BranchRuntime
        |
Run成果物、履歴index、Inspector projection、比較レポート
~~~

Python標準ライブラリのHTTPサーバーがローカルブラウザーアプリを配信します。ブラウザーは型付きrequestを送信し、simulation stateを直接書き換えません。保存済みRunのInspectorはread-onlyです。Project操作は別の検証済みcommandを使います。

## モジュールの責務

| 領域 | 主な責務 |
|---|---|
| core | WorldState、simulation clock、keyed RNG、tick engine、Checkpoint |
| domain | 人物、施設、Zone、route、資源、職員、運営規則 |
| events | 予定・実行済みevent、型付きhandler、EffectProgram |
| policies | 行動、ニーズ、空間、心理、社会、情報モデル |
| experiments | Scenario、Run、Branch、timeline、metrics、evaluation |
| inspection | 証拠検索、Run projection、trace要約 |
| region | 地域データ、geometry検証、施設図面取込 |
| llm | Provider adapter、型付き出力、cache、応答再生、利用額記録 |
| storage | Project、asset、Run、Checkpoint、履歴indexの永続化 |
| web | ローカルHTTP route、Project API、Inspector、Demo、ブラウザーasset |

実装の正本はパッケージ構成です。公開入力・状態契約はsrc/sheltersim/contracts、event/world model、およびserializationと互換性を確認するテストにあります。

## シミュレーションと状態境界

- 時刻は整数分です。event順と同順位の決着は、固定IDと明示的なsort keyで決めます。
- WorldStateが変更可能なsimulation stateの正本です。Policyとadapterは型付き提案を作り、controllerが検証して適用します。
- 乱数はScenario seedと安定したdomain IDから決定します。simulation結果の判断にwall-clockやmodule-global乱数を使いません。
- EventQueueは今後処理する予定を、EventLogは結果を記録します。sourceやcausationを含む、相互に区別された記録です。
- TickEngineは毎分の終わりに、現在の状態と、その分に追記された履歴記録（直前の1件を含む）を検証します。追記専用の履歴（共有運用、生活状態の社会履歴、給水・配食の依頼と停止の履歴、資源台帳の監査記録）とEventLogの全体の再生、共有運用の支援ライフサイクルの再構成は、Runの開始・終了とCheckpointの作成・復元で行う完全検証で確認します。毎分の検証が履歴全体を再走査しないため、1分あたりの検証時間は履歴の長さに比例して増えません。完全検証のうちEventLogについては、次のように読む量を減らしています。EventLogは、記録を足すときに作る索引（IDと順序番号、イベントの種類ごと、来歴に介入（EffectProgram）の印を持つ記録）を持ちます。運営イベント・介入・環境・インフラの完全検証は、確かめるイベントの種類の記録だけを索引から読み、IDによる参照も、EventLogを写した辞書ではなく索引（`EventsById`）で引きます。ほかの種類の記録は、これらの確認を素通りするだけなので、確かめる範囲は変わりません。EventLog自体の監査（IDと順序番号の重複、実現時刻）は、前の監査で確かめた記録が先頭に同じオブジェクトのまま残っていれば、増えた記録だけを、それが索引の指す記録であることと合わせて確かめます。記録は凍結されていて、追記しかされないためです。先頭の記録が差し替わっていたり、前より早い時刻での監査だったりすれば、全体を確かめ直します。
- 資源台帳への操作（納入、損失、予約、解放、消費）と毎分の指標集計は、現在の残高・予約と、直前の監査記録・各資源の最新の監査記録を確認します。操作は変更する口座だけを複製した候補に適用し、確認を通ってから差し替えます。監査記録の全件の再生は完全検証で行います。
- Checkpointは対応する復元に必要なstateとruntime情報を含みます。BranchRuntimeは分離したstateを復元し、兄弟Branchで可変queue、RNG状態、actor objectを共有しません。
- 計算を進めるworker（`TimelineStore`の継続計算）は、Checkpointを保存するたびに、前の保存で変換した追記専用の履歴（EventLog、KPIと資源の標本、上の追記専用の履歴）の文書と正準JSONのbyte列、EventLog記録の復元結果を再利用し、新しく増えた分だけを変換・復元します（`core/checkpoint_cache.py`、#44）。保存されるCheckpointのbyte列、hash、archiveのchunkは、全体を変換した場合と同じです。Checkpoint v2のhashはchunkのhashの木なので（`docs/project-persistence.md`）、保存は新しいchunkだけをhashします。項目は同一性で照合し、古い項目を先頭から捨てる窓にも対応します。凍結されていない履歴の項目は保存済みの文書と等しい間だけ再利用し、書き換えられていれば変換し直します。保存前の完全検証は、従来どおり保存のたびに行います。復元の確認では、保存した文書から世界を復元し、それが直前に完全検証した世界と同じ状態か（`WorldState.same_state`）を比べます。同じなら、同じ検証を通るので、復元した世界の完全検証は省きます。違えば、従来どおり復元した世界を完全検証します。EventLogは、前の保存の確認で同じと分かった記録を比べ直さず、増えた記録だけを比べます。復元の確認で作る世界は比べたら捨てるので、保存した文書を写さずにそのまま使って組み立て、EventLogは前の保存の確認で作ったものに増えた記録だけを足して使います（索引もそのまま保ちます）。計算を再開したときと分岐したときは、読み込んだ（または分岐で作った）CheckpointのEventLogの文書と、そこから復元した記録を最初の保存で再利用します。復元した記録は、文書が記録と同じフィールドだけを持つとき、元の文書に戻るためです（`CheckpointHistoryCache.seed_events`）。
- 計算を進める間（`TimelineStore`の継続計算）とCheckpointの読込の間は、Pythonの循環参照の回収（gc）の頻度を下げます（`core/collection.py`の`fewer_collections`、#234）。最も若い世代の回収を、新しいコンテナオブジェクト700個ごとから50,000個ごとにし、最後の区間が終わると元の閾値に戻します。閾値はプロセスに1つなので、区間が有効な間は同じプロセスのほかのスレッドにも効きます。Webサーバーでは計算が別プロセスで動くので、サーバーのプロセスに効くのはCheckpointの読込の区間だけです。1日分の計算（100人）ではCPU時間が約9%短くなり、ピークRSSが約47 MiB増えます。
- Webサーバー（`serve_inspector`）の`ProjectAPI`は、計算1回（`POST R/resume`）と分岐1回（`fork`、intervention-apply）ごとに別プロセスを起動して処理します（`web/worker_process.py`、`ProjectAPI(worker="process")`、#48）。起動はWindowsと同じspawn方式です。子プロセスがRunの書込権（`claim_run`）を自分で取り、取れたことをサーバーに知らせてから計算します。取れなければ従来どおり`run_worker_busy`で断ります。中止は`multiprocessing.Event`で伝え、サーバーのプロセスがなくなった場合も、子プロセスは次の保存境界で止まります。子プロセスが結果の状態を書かずに終わった場合（強制終了など）、サーバーはRunを`interrupted`にします。分岐の間はRunを`running`にし、サーバーが結果（`last_fork`）を記録してから元の状態に戻します。分岐の子プロセスが強制終了した場合も、Runは元の状態に戻ります。分岐の間も、サーバーはほかのHTTP要求に応えます。計算中もサーバーのプロセスはHTTPの応答でGILを取り合わず、計算が終わると子プロセスが使ったメモリはOSに戻ります。telemetryの`run_peak_memory_bytes`には、子プロセスのピークRSSを記録します。同じBranchの計算と、そのBranchからの分岐は1つずつですが、同じRunのほかのBranchや異なるRunは、上限（`sheltersim web --max-parallel-runs`、既定2）まで同時に、それぞれ別プロセスで計算・分岐します（#48）。書込権はBranchごとです。計算はそのBranchの、分岐は作る子Branchの書込権（`ProjectStore.claim_branch`、`branches/{branch}/worker-lock`）を取ります。Branchの書込権は、Runの入口のロック（`runs/{run}/worker-lock`）を一瞬だけ取ってから取ります。Runの削除・履歴索引の作り直し・回復のようなRun全体の操作（`claim_run`）は、入口のロックを持ち続けたうえで、どのBranchも書かれていないことを確かめます。占有ロックだけを使うので、Linux・macOS・Windowsで同じ組み方です。上限に達しているときの要求は、`background workers are busy` で断ります。計算1本で、1日分のRun（100人）なら約0.5〜0.9 GBのメモリを使うので、上限はCPUの数ではなくメモリに合わせて決めます。テスト、CLI、計測器（`benchmarks/investigate_performance.py`）は、同じプロセスのスレッドで計算します（`worker="thread"`、既定）。
- Branchの分岐（`TimelineStore.fork`）は、分岐する時刻の状態からCheckpointを1回だけ作ります。そのCheckpointが分岐の記録（`fork_origin`）の`state_hash`と`checkpoint_hash`になり、作成時の復元検証で組み立てた世界と指標が、そのまま子Branchになります（`Checkpoint.capture_restored`、#46）。この世界と指標は、Checkpointとも分岐元とも何も共有しません。子Branchに引き継ぐ応答テープは、分岐元のhead Checkpointの`runtime_state`だけを読みます。
- inspection、描画、metrics、trace levelはWorldStateを変えず、simulation乱数も消費しません。

ScenarioSpec、WorldState、Checkpoint、EffectProgram、Project recordはそれぞれ個別に版管理します。state変更には互換性テストが必要です。古い永続データを読み続ける場合は明示的な移行を用意します。

## 永続化とインターフェース

ProjectStoreとTimelineStoreは、ローカルProject、承認済みScenario、Run、Branch、Checkpoint chunk、asset、index付き証拠を保持します。CLI Run成果物にはmanifest、凍結Scenario、event記録、metrics、任意のモデル利用記録が含まれます。formatは有効化した機能によって異なります。APIリファレンスを参照してください。

HTTP APIは次の3領域を分けています。

- /api/v1/projects: 永続Projectと状態変更操作。
- /api/v1/runs: 保存Run成果物の上限付きread-only参照。
- /api/v1/demo: process内のデモBranch。

HTTPサーバーは既定で127.0.0.1にbindします。認証機能がないため、信頼できないネットワークへ公開してはいけません。

## 外部接続との境界

地域データProviderと任意Anthropic adapterはsimulation coreの外側にあります。Providerから得たデータは信頼せず、sourceと適用範囲を保持します。offline replayは保存済み応答か決定的fallbackを使い、過去時点の参照でlive Providerを呼びません。

モデル仮定と検証範囲はsimulation-models.mdとmodel-validation.mdを参照してください。
