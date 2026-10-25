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

approved hashは確認した正準ScenarioSpecそのものに結び付きます。CLIとWebの継続実行は設定されたLLM modeを使い、モデル設定と費用記録を保持できます。過去frameの読み出しではnetwork adapterを作りません。offline履歴が暗黙にlive履歴へ変わることはありません。記録が欠落または非互換なら、過去の再構築を拒否します。

時刻Mはtick Mの直前境界です。timelineのWorldState KPIはその境界で取得します。保存metrics.csvはtick終了時刻の意味を保ちます。frameにはexact、state_hash、computed_untilを含み、未計算時刻は拒否します。親と兄弟のstateは独立しています。geometry点を物理メートルとして扱うのは検証済みmovement位置がある場合だけです。旧来の表示用位置はdisplay_onlyと表示します。

Checkpointを保存したworkerは、その時点のframeをメモリ上のWorldStateから描画し、`runs/{run}/frames/{checkpoint_hash}.json.gz` に保存します。frameはCheckpointから導出できる置き換え可能なread modelで、`schema_version: 1`、描画版 `timeline-frame-v1`、元の `checkpoint_hash`、frame本体のSHA-256を持ちます。timeline GETは、要求時刻が保存境界で、キャッシュの版・hash・Run・Branch・時刻が一致する場合だけそのframeを返し、`computed_until` を現在のBranch値に置き換えます。それ以外の時刻、旧Run、欠落・破損・版不一致のキャッシュは従来どおりCheckpointから復元して描画します。GETはキャッシュを書き込みません。キャッシュの書込失敗や容量超過はCheckpoint保存を失敗させず、次の読取が復元経路になるだけです。描画結果を変える変更では `FRAME_PROJECTION_VERSION` を上げます。

archiveは記録を保持するだけで、disk容量を空けません。deleteは非稼働Runに対し、同じRun IDを明示確認した場合だけ実行でき、そのRunだけを削除します。ProjectStoreは容量とfile数を返します。Checkpointは版管理されたgzip chunkとcontent hashを使い、公開Checkpoint JSON v1と従来のraw保存も読み込みます。Scenario、Run、Branch IDは実在避難者名ではなく安定識別子です。credential fieldを拒否し、一般的なsecret/contact文字列をredactまたは拒否します。Checkpoint保存時のこの検査は、archiveがまだ保存していないchunkの値と、分割されたobjectのフィールド名に対して行います。既存chunkは最初の書込時に同じ検査を通っています。新しいchunkは全検査が通ってから書き込むため、拒否されたCheckpointは何も書きません。workerが `capture()` した直後のCheckpointは `save_captured_branch` で保存し、`capture()` 時の検証を再実行しません。それ以外の入力は `save_branch` が再検証します。会話記録はserverが検証したRun/Branch/時刻に結び付くEvidenceReferenceを使い、browserから送られたkeyを保存しません。

no-op childは親に記録された既存Provider応答を、cache/fallback/費用metadataごと引き継いで再生します。明示的に通常継続を依頼した場合は設定済みrouterで記録範囲の先へ進められますが、過去seekがProviderへfallbackすることはありません。承認済み介入は将来分の継承response replayを無効にし、既に発生した予算記録と過去の承認済みtapeは保持します。
