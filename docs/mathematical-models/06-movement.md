# 06. 移動

## 目的とモデル

既存Zoneグラフ最短経路を維持し、幾何情報が明示されたScenarioでは距離・移動能力・混雑を使う空間的所要時間を適用します。Social Forceの個体衝突力学はモデル化しません。地図座標がないScenarioに架空距離を割り当てません。

## 数式・単位

Zone edge travel time \(t_e\) はScenario値(分)です。geometry routeが利用できる場合は
\[
T_{ie}=\left(\max\left(t_e,\frac{L_e}{v_i^{free}}\right)\right)g(\rho_e),\quad
g(\rho)=\min(g_{max},1+c_\rho\max(0,\rho-0.5)).
\]
移動能力別 \(v_i\) はm/min、\(L_e\) は明示的route geometryのm、\(\rho\) は密度(person/m²)、最終所要時間は上方丸めの整数分です。route geometryがなければScenario edge timeを使います。設定速度と混雑係数は未校正仮定です。

仮に宣言edge時間2分、geometry長60m、独立移動の既定速度72m/min、密度2人/m²とすると、混雑係数は \(1+0.12(2-0.5)=1.18\)、所要時間は \(\max(2,60/72)\times1.18=2.36\) 分を上方丸めて3分です。すべて仮の設定例です。

## 実装・連携

`src/sheltersim/policies/space.py` はZone最短経路、`src/sheltersim/domain/movement.py` は `route_with_movement_time`、物理位置、進捗、version `spatial-route-and-room-waypoint-v2` を管理します。施設・スタッフ・Agent移動はイベント完了時に更新し、閉鎖edgeはroute graphから外れます。

## 内部検証と限界

既存移動テストは閉鎖route、mobility別速度、配置・隊列・checkpoint、容量制約を検査します。速度はサンプルScenarioに与えた値であり、公的な歩行速度や現場測定値ではありません。歩行者衝突・煙・視認性・身体的負荷は扱いません。
