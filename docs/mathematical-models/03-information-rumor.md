# 03. 情報・噂・訂正

## 目的と理論

Daley–Kendallの噂伝播過程とネットワーク上の噂伝播研究を参考に、局所接触を通じた情報曝露を表します。SNS向け伝播率を対面避難所へ移していません。真偽のシミュレーション上の値、Agentの曝露、信念、共有傾向、訂正曝露・受容を別々に扱います。

## 状態・数式

各Agent–主張ごとに曝露 \(E_i^m\)、信念区分 \(B_i^m\)、連続的信念強度 \(b_i^m\)、共有傾向 \(s_i^m\)、訂正曝露 \(C_i^m\)、訂正受容 \(R_i^m\) を保持します。保存状態は `AgentInformationState` です。新規の主張曝露時には、信頼度で調整した確率で信念強度を0または1に標本化します。曝露したことだけで信じたことにはなりません。`UNKNOWN` も曝露後の忘却として保存でき、共有傾向は0です。

情報runtime stateはschema 2です。schema 1の保存状態は検証・読み込みでき、次にevidence-aware更新/イベント処理を行うときに cumulative fields 付きへ明示移行します。`ever_misbelieved` と `ever_forgotten` は各Agent–claimの履歴flagです。既存schema 1からの移行では、保存された信念強度・真偽・UNKNOWN状態から復元できる範囲のみ初期化します。

対面接触の候補選択確率は、設定済みの基礎率・時間・相手信頼度・関係倍率で構成します。研究用variantではさらに共有傾向と急性心理状態を掛けます。
\[
p_{ji}^m=\min(1,p_0 f_{duration} f_{trust} f_{channel} f_{correction}\; s_j^m\min(1,1+\xi A_j)).
\]
曝露hazard型は \(1-e^{-\lambda\Delta t}\) を `hazard-information-v1` で用います。共有停止傾向は1分あたり線形減衰、忘却は \(p_{forget}=1-e^{-\omega\Delta t}\) です。

初回の主張受容は \(p_{rumor}=\operatorname{clip}_{[0,1]}\{p_r+2\eta_{rt}(T_i-0.5)+2\eta_{rs}(R_m-0.5)\}\) です。信頼度と発信源信頼性は未校正のShelterSim仮定です。訂正受容は独立した確率過程です。
\[
p_{accept}=\operatorname{clip}_{[0,0.95]}\{p_b+2\eta_t(T_i-0.5)+2\eta_s(R_m-0.5)\}.
\]
ここで \(T_i\) は受信者の信頼度、\(R_m\) は発信源別信頼性です。上限0.95によりこのvariantでは受容が確実にはなりません。確率 draw はseed、イベント、Agent、主張IDでkeyedです。

## 計算例

仮に \(p_b=0.7,\eta_t=0.2,T_i=0.8,\eta_s=0.2,R_m=0.9\) なら受容確率は \(0.7+0.12+0.16=0.98\) から上限により0.95です。数値は説明例で、受容率の実測値ではありません。

## 実装・連携

- `src/sheltersim/domain/models.py`: claim・belief・exposure/share/correction stateと設定
- `src/sheltersim/policies/information.py`: 疎接触候補、hazard、共有傾向減衰、忘却
- `src/sheltersim/events/handlers.py`: 噂開始、確率的訂正受容、次tick接触反映
- `src/sheltersim/domain/body_state.py`: 研究variantでは受容された訂正だけを安心・不確実性へ反映
- version: `evidence-aware-rumor-v1`; legacy comparator: `sparse-information-contact-v1`

信念と共有は別です。心理状態は共有傾向へ影響し、受容訂正は身体/感情状態と問い合わせ圧力を下げ得ます。正解ラベル自体はAgentの信念ではなく、Scenarioで設定するシミュレーション側の真偽です。

選択した心理policyが `mean-reverting-life-state-v1` の場合、falseと定義された主張に対する現在の信念強度が0.5を超えると、不確実性更新に持続的なbelief pressureが入ります。これは `information_query_pressure`（曝露・問い合わせ由来）とは別の状態入力で、上限1に集約します。訂正を受容すると信念強度が訂正結果へ移り、false claimのpressureは消えます。拒否した人には訂正のbody-state安心効果を与えません。訂正eventによる不確実性低下・安心増加はevent時に一度記録し、その後の心理更新は更新後の身体状態を読みます。持続する誤信念によるtickごとの影響は、event replayではなく有効な信念状態から生じるper-minute inputです。

Traceには対象Agent、event minute、message ID、rumor origin event ID、beliefを最後に更新したevent ID、情報source ID、pressure、心理への不確実性経由の寄与を記録します。event-level correctionの状態差分は既存body/social historyにもevent IDとdeltaとして残ります。

## 内部検証と限界

集計では現在の誤信念Agent数・Agent-message数、過去に誤信念を保持したAgent数、訂正曝露/受容者数、現在/過去の忘却者数を分けます。忘却後に残る数値的なbelief_strengthは履歴として保持されますが、現在の誤信念には数えません。

`tests/integration/test_information_spread.py`・`tests/unit/policies/test_information.py` と `tests/unit/policies/test_psychology.py`・`tests/unit/domain/test_information.py` はlegacy挙動、誤情報訂正、曝露と受容の分離、受容者/非受容者、忘却後集計、schema移行、誤信念から不確実性・心理状態へ入る経路、同seed再現を確認します。接触・信頼・受容・忘却係数は未校正仮定です。曝露率や受容率を現実の避難所の予測と解釈しません。
