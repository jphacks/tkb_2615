# 保存したRunの地図

Runのmanifestの`model_configs.facility_geometry`に図面があれば、その図面から描きます（#161）。メートル単位の図面を、縦横比を保って地図のcanvas（1000×560、余白20）に縮小し、区画の多角形と施設の位置（`facility_points`）を表示用の座標に変換します。応答の`layout.id`は`facility-geometry`、`synthetic_layout`は`false`です。生成Scenarioは図面を持つので、CLIで実行したRunもこの方法で描きます。

図面のないRunは、同梱の合成体育館で描きます。fixtures/layouts/gym_synthetic.jsonは表示用の合成図です。実在避難所の平面図ではありません。座標はZonePolicy、移動時間、event処理、乱数状態、保存simulation stateに影響しません。選択Runに対応するZoneがない区画は表示専用と明記します。

Agent markerは、記録済みZone polygon内にAgent ID、Zone ID、選択minuteの安定hashを使って配置します。同じ状態なら同じ分布となり、simulation乱数は消費しません。markerは選択minute以前の最新decision前snapshotを使います。その時点の記録がなければ初期profile位置を使います。地図に多角形のない区画（合成体育館にない区画や、介入で増えた区画など）にいるAgentは、黙って外さず、応答の`undrawn_agents`にAgent IDと区画を載せ、地図の注記に人数と区画を示します。

施設状態はRun manifestから始め、選択minute以前に正常適用された施設停止・復旧eventだけを反映します。便器queueと使用中数は選択時刻以前で最も近いmetrics rowを使います。これらはZone単位集計で、個別便器のqueueではありません。座標、marker位置、混雑overlayは説明用表示です。物理距離、定員推奨、現実環境を示す根拠にはなりません。

map契約はofflineのtests/unit/web/test_inspector.py、tests/integration/test_web_inspector.py、tests/unit/web/test_static_app.py（注記）で確認します。Scenarioを変更せず、long simulation regressionも必要ありません。
