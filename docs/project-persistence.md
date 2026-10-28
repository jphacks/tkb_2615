# Projectの永続化、正確な時刻、復旧

Web serverはRUNS_DIR/.projectsを開き、ProjectAPI経由で/api/v1/projectsを提供します。Scenario版とRun入力は不変です。条件編集では新しいScenario版を保存してからRunを作ります。Branch位置は、integrity確認済みCheckpointの保存後にだけ進めます。browser再読込後も同じIDを参照できます。server再起動時は中断済み作業として記録し、最後に完了したCheckpointから再開します。不完全なchunkを完了扱いしません。

同じ操作をCLIから利用できます。

~~~text
sheltersim project --root runs/.projects create --project-id study
sheltersim project --root runs/.projects save-scenario --project-id study --scenario scenario.json --approved-hash <reviewed-canonical-sha256>
sheltersim project --root runs/.projects new-run --project-id study --run-id trial --scenario-id <id> --scenario-version 1
sheltersim project --root runs/.projects resume --project-id study --run-id trial --minute 100
sheltersim project --root runs/.projects frame --project-id study --run-id trial --minute 73
sheltersim project --root runs/.projects fork --project-id study --run-id trial --minute 73 --child-id alternative
~~~

approved hashは確認した正準ScenarioSpecそのものに結び付きます。Webの計算開始（POST R/resume）は、BranchのポインタとScenarioの期間だけで目標時刻を検査して受け付けます。head Checkpointの展開と検証はworkerが1回だけ行い、破損や非互換を検出した場合はRunを `failed` にして、Branchのポインタは変えません。CLIとWebの継続実行は設定されたLLM modeを使い、モデル設定と費用記録を保持できます。過去frameの読み出しではnetwork adapterを作りません。offline履歴が暗黙にlive履歴へ変わることはありません。記録が欠落または非互換なら、過去の再構築を拒否します。

時刻Mはtick Mの直前境界です。timelineのWorldState KPIはその境界で取得します。保存metrics.csvはtick終了時刻の意味を保ちます。frameにはexact、state_hash、computed_untilを含み、未計算時刻は拒否します。親と兄弟のstateは独立しています。geometry点を物理メートルとして扱うのは検証済みmovement位置がある場合だけです。旧来の表示用位置はdisplay_onlyと表示します。

Checkpointを保存したworkerは、その時点のframeをメモリ上のWorldStateから描画し、`runs/{run}/frames/{checkpoint_hash}.json.gz` に保存します。frameはCheckpointから導出できる置き換え可能なread modelで、`schema_version: 1`、描画版 `timeline-frame-v1`、元の `checkpoint_hash`、frame本体のSHA-256を持ちます。timeline GETは、要求時刻が保存境界で、キャッシュの版・hash・Run・Branch・時刻が一致する場合だけそのframeを返し、`computed_until` を現在のBranch値に置き換えます。それ以外の時刻、旧Run、欠落・破損・版不一致のキャッシュは従来どおりCheckpointから復元して描画します。GETはキャッシュを書き込みません。キャッシュの書込失敗や容量超過はCheckpoint保存を失敗させず、次の読取が復元経路になるだけです。描画結果を変える変更では `FRAME_PROJECTION_VERSION` を上げます。

archiveは記録を保持するだけで、disk容量を空けません。deleteは非稼働Runに対し、同じRun IDを明示確認した場合だけ実行でき、そのRunだけを削除します。ProjectStoreは容量とfile数を返します。容量は呼ばれるたびにdisk上の通常fileを数え直すので、ほかのprocessが書いたfileも含みます。symlinkは数えず、たどりません。数えている間に消えたfile（書込途中の一時fileなど）は0 bytesとします。容量上限を超える書込は、保存fileでも履歴索引でも拒否し、書く前の状態に戻します（#47）。Checkpointは版管理されたgzip chunkとcontent hashを使い、公開Checkpoint JSON v1と従来のraw保存も読み込みます。Scenario、Run、Branch IDは実在避難者名ではなく安定識別子です。credential fieldを拒否し、一般的なsecret/contact文字列をredactまたは拒否します。メールアドレスは、`@`のあとのドメインの末尾が英字などの文字のものだけを検出します（`storage/projects.py`の`EMAIL_ADDRESS`。Run成果物と保存Runの表示も同じ定義を使う）。モデルの署名のように`@`のあとに小数点を含む値は、連絡先とみなしません（#187）。電話番号は、区切りのある3つの数字の組（`090-1234-5678`、`+81 90 1234 5678`など）を検出します（`PHONE_NUMBER`。Run成果物と保存Runの表示も同じ定義を使う）。論文のDOIや日付は電話番号とみなしません。区切りのない数字の並びは検出しません（#190）。Checkpoint保存時のこの検査は、archiveがまだ保存していないchunkの値と、分割されたobjectのフィールド名に対して行います。既存chunkは最初の書込時に同じ検査を通っています。新しいchunkは全検査が通ってから書き込むため、拒否されたCheckpointは何も書きません。workerが `capture()` した直後のCheckpointは `save_captured_branch` で保存し、`capture()` 時の検証を再実行しません。それ以外の入力は `save_branch` が再検証します。会話記録はserverが検証したRun/Branch/時刻に結び付くEvidenceReferenceを使い、browserから送られたkeyを保存しません。

Runの履歴（`history`）は、計算しながら書く履歴索引（`runs/{run}/history.sqlite`）から読みます。索引には、Checkpointから作れる記録（イベント、KPI、社会・職員・人口の履歴など）のほかに、計算中に記録した行動・会話の判断（候補、方針の版など）が入ります。判断はCheckpointに入らないので、索引を作り直すと戻りません。そのため「既存Runの履歴索引を再構築」（`POST R/index`）は、索引が分岐の先頭のCheckpointと一致しているとき（状態`ready`）は`history_index_current`で断ります。作り直せるのは、索引のない古いRunや、先頭がずれた索引だけです。作り直した索引には判断の記録がなく、応答の`limits`にそのことを書きます（#213）。索引への登録はCheckpointの保存1回ごとに1つのトランザクションで行い、記録を数百件ずつまとめて書き込みます。すでにある記録と同じkeyの記録は追加せず、内容が違えば`immutable_history_record_conflict`で拒否して、その登録全体を戻します（#47）。

no-op childは親に記録された既存Provider応答を、cache/fallback/費用metadataごと引き継いで再生します。明示的に通常継続を依頼した場合は設定済みrouterで記録範囲の先へ進められますが、過去seekがProviderへfallbackすることはありません。承認済み介入は将来分の継承response replayを無効にし、既に発生した予算記録と過去の承認済みtapeは保持します。
