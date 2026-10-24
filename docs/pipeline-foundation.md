# シミュレーション基盤とpipelineの範囲

このrepositoryは、チームがsimulationの実行境界を独立して検討できるようにした作業用スケルトンです。製品本体の複製ではありません。

## 含まれる基礎部品

| 部品 | このrepositoryで確認できる契約 |
|---|---|
| SimulationClock | 整数分、有限duration、実時間時計から独立した進行 |
| CounterRNG | seedと安定keyから決まるSHA-256 keyed draw |
| WorldState | Scenario識別子、seed、時刻、domain stateを格納する最小状態 |
| TickEngine | 固定された7 phaseを順番に呼び、時刻の所有権を守る |
| Checkpoint | JSON stateとschema版を保存し、復元時にSHA-256を検査する |
| SimulationPipeline | 入力検証、初期状態作成、tick実行、Checkpoint sinkを接続する |

## 実行の流れ

~~~text
Scenario入力
    │
    ▼
注入されたvalidator ── 不正入力を拒否
    │
    ▼
WorldState factory
    │
    ▼
TickEngine ── 固定順phase handler ── keyed RNG
    │
    ├── intervalごとにCheckpointを生成
    ▼
注入されたCheckpoint sink
~~~

phase順は製品のtick契約に合わせた足場です。各phaseは現在no-opとして扱え、domain処理を外部接続や永続化と混ぜずに段階導入できます。handlerはengine所有のminuteを変更できません。

Checkpointはstateの破損を検出する基本形です。暗号学的な署名、複数schema間のmigration、atomic disk storage、event履歴やProvider tapeの永続化は実装していません。ハッシュは整合性検査であり、作成者の真正性を保証しません。

## ここに含めない製品機能

避難所、Agent、needs、施設、物資、event handler、policy、region data、LLM、HTTP、Project/Run/Branch storage、UIを含みません。製品側の契約やモデル文書をdocsに参照用として置いていますが、記述された全機能がこのpackageにあるという意味ではありません。製品全体へ変更を反映する際は、対応する実装と契約を比較し、必要な移植とレビューを行ってください。
