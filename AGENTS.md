# ShelterSim基盤repositoryの開発規則

このrepositoryはsimulation coreとpipelineの作業用骨格です。製品全体の仕様はREADMEとdocsを参照し、製品側の実装と契約の整合性を保ってください。

## 不変条件

- 同じScenario、seed、モデル版、記録済み入力には、同じstateとevent順を返します。
- 時刻は整数分とし、tickのphase順を固定します。結果の決定にwall-clockやmodule-global乱数を使いません。
- 乱数はseedと明示的な安定keyから計算し、他のAgentの実行順で変化させません。
- read-onlyなtraceやCheckpoint取得でsimulation stateを変更しません。
- Checkpointはschema版とstate hashを持ち、復元時に改変を検出します。
- 入力検証、state初期化、simulation、保存を分離し、外部接続をcoreへ入れません。
- 新しい永続fieldには版管理と復元testを追加します。

## 責務

- core/clock.py: 整数分の時計。
- core/rng.py: keyed deterministic RNG。
- core/world.py: 最小state契約。製品の避難所domain modelではありません。
- core/engine.py: tick phaseの順序と時計の所有。
- core/checkpoint.py: 最小Checkpointの生成、hash検証、復元。
- core/pipeline.py: validate、initialize、execute、checkpoint hookを接続。

製品固有のPolicy、facility、Agent、event handler、storage、HTTP、LLMをこの骨格へ不用意に持ち込まないでください。契約変更が必要なら本体との互換性を確認し、該当文書とtestを更新します。

## テストとチーム作業

- testはofflineで実行し、外部APIや個人データを使いません。
- 動作変更には回帰testを追加します。実行していないcheckをpassと報告しません。
- feature branchとPull Requestで変更を共有し、mainへ直接実装commitをpushしません。
- schema、Checkpoint、pipeline境界の変更は、該当箇所に詳しい担当者のレビューを受けます。
- secret、実在避難者記録、ローカルRun成果物をGitへ登録しません。
