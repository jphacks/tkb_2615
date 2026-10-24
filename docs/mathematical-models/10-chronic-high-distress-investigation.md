# 慢性的な高ストレスの原因調査

## 対象と結論

調査ブランチ `investigate/chronic-high-distress` は、統合後の `main` (`fbb75b9`) から作成した。モデル係数や通常の状態更新コードは変更していない。1日Runの診断は一時領域で実行し、Run成果物をGitへ追加していない。

高ストレスの主因は、自然回復が止まることではない。平均回帰は1分刻みで正しく計算されているが、合成Scenarioが毎分発生させる未充足ニーズ、睡眠負債、衛生・不満feedback、待ち行列の正味入力が回復を相殺する。急性stressが0.7を超えた後は `[0,1]` の上限clipが入力差を圧縮し、通常運営と不足の差を `stress >= 0.7` だけで見分けにくくする。

1日Runでは初期stress 0から、通常運営・不足の双方で終了時73%が0.7以上になった。通常運営Scenarioにも、平均初期空腹0.496・疲労0.492、100人中35人の初期排泄欲求0.7以上という強い初期ニーズがある。加えて、水分需要の合成成長率は0.006 normalized/minuteで、通常運営の水サービス完了は平均2.51回/人/日にとどまった。この組み合わせは「一般的な正常避難所」の校正済み条件ではない。

## 実装と数式

`IntegratedDistressLifeStatePolicy` の急性stress更新は、1分刻みでは次の平均回帰式に一致する。

\[
A_{t+1}=\operatorname{clip}_{[0,1]}\left[A_0+(A_t-A_0)e^{-\kappa\Delta t}
+F_t\frac{1-e^{-\kappa\Delta t}}{\kappa}\right],
\qquad \Delta t=1\text{ minute}.
\]

`_mean_reverting_terms` は指数項と定係数外力の積分係数を解析的に計算する。既定の `κ=0.01/min` は時定数100分、半減期約69.3分に相当する。一定外力のclip前平衡は `A*=A0+F/κ` であり、`A0=0` なら `F=0.007/min` で0.7、`F=0.01/min` で1.0に達する。

このScenarioでは `F` は一定でない。少なくとも `need pressure`、施設queue、support待ち、sleep-debt/uncertainty/discontent/hygiene/sanitation等のfeedbackを含み、身体・行動状態とstressに依存する。したがって上式の一定外力平衡は単純化した閾値比較にだけ使い、実際の固定点と同一視しない。

実装上の直接need入力は `0.012 × max(0, 最大need - 0.35) × stress_sensitivity`。統合Policyは睡眠中の旧式直接stress回復を無効にし、平均回帰とsleep-debt feedbackを二重に加算しない。ストレス依存のdiscontent更新にはtick開始stressを使うため、同じtick内の代数的循環はない。

一方、tick間には明示的な正feedbackがある。

\[
D_{t+1}=D_t+aA_t+\cdots,\qquad
A_{t+1}=\rho A_t+g(bD_{t+1}+\cdots),
\]

ここで `D` はdiscontent、`a=b=0.001/min`、`ρ=e^{-0.01}`、`g=(1-ρ)/0.01`。他の状態を固定して `[A,D]` の局所Jacobianを組むと固有値は約 `0.989952` と `1.000099`。この未clip線形化では遅い不安定方向のe-fold時間は約10,099分（約7日）である。ただし `D` 自体は `[0,1]` にclipされるため、発散は続かず、上限後は持続外力になる。これは一要因の数理切り分けであり、実系全体の安定性証明ではない。

## 実験方法

1. `research_operational_100_7d.json` と `research_shortage_100_7d.json` を同じseed 2026、同じ100 profileのまま1,440分に短縮し、窓外イベントを除いて各1回実行した。`TraceLevel.SUMMARY` とインメモリの読み取り専用trace集計で、全Agentの毎tick状態、心理寄与、clip前候補、サービス完了直後のneedsを集約した。設定値と遷移式は変更していない。
2. 通常運営fixtureの先頭12 profile、seed 2026、180分で、baselineに加え、直接need stress入力、sleep-debt係数、discontent係数、queue入力、水分需要成長を一項ずつ0にしたScenarioコピーを比較した。さらにlife-stateなしPolicyを参考比較した。これは各1 seedの機構確認で、効果量・統計的有意差の推定ではない。`discontent_stress_rate` はdiscontentの蓄積とfeedbackの両方に使うため、このablationはその両方を同時に切る。
3. `test_exact_mean_reversion_matches_finer_time_steps_and_has_a_bounded_equilibrium` と `test_full_runner_recovers_initially_high_agents_and_records_online_recovery` を実行した。
4. 7日通常運営/不足の再実行はしていない。該当する統合済み記録は `docs/mathematical-models/09-internal-validation.md` と `research-validation` JSONにある。短時間Runの結果と混同しない。

## 1日Runの結果

| 指標 | 通常運営 | 資源不足 |
|---|---:|---:|
| 終了時stress平均 / 中央値 / p95 | 0.8272 / 0.8558 / 1.0000 | 0.8347 / 0.8710 / 1.0000 |
| 終了時 `stress >= 0.7` | 73% | 73% |
| 24時間の高stress合計 | 64,477 Agent-minute | 67,258 Agent-minute |
| 1人あたり高stress時間の全員平均 | 644.8分 | 672.6分 |
| 急性stressが上限候補を超えたtick | 18,533 Agent-minute | 21,421 Agent-minute |
| 心理Policyのraw delta - applied delta | 25.136 stress-unit | 32.098 stress-unit |

高stress時間は `100人 × 1,440分` を分母にした合計である。clip候補超過は心理Policy内のclip差分で、同じAgentへ後続の型付き支援・社会eventが加えるstress変化は含めない。`social_propagation`、information spread、明示的な環境stress入力はこのfixtureで無効または不在で、summary trace上も0だった。型付きの家族支援・voluntary support等のsocial interactionは別に実行されており、「社会的伝播が全くない」という意味ではない。

| 経過時間 | 通常 平均 / 高stress率 | 不足 平均 / 高stress率 |
|---:|---:|---:|
| 1時間 | 0.082 / 0% | 0.088 / 0% |
| 4時間 | 0.398 / 3.8% | 0.430 / 7.1% |
| 6時間 | 0.567 / 29.2% | 0.598 / 32.6% |
| 9時間 | 0.670 / 45.5% | 0.691 / 49.6% |
| 13時間 | 0.731 / 53.6% | 0.751 / 55.9% |
| 17時間 | 0.776 / 61.3% | 0.778 / 59.7% |
| 21時間 | 0.799 / 65.0% | 0.811 / 66.5% |
| 24時間 | 0.824 / 72.4% | 0.831 / 72.2% |

### 終了時の高ストレスAgentの特徴

同じ100 Profile・seed 2026・1,440分のScenarioをTrace OFFでも実行し、返された最終WorldStateで `stress >= 0.7` の73人と、閾値未満の27人を層別した。初期ProfileはSummary実行と一致した。以下はこの合成条件1回に限った記述値である。

| 状態 | 高stress 73人：平均（中央値） | 閾値未満 27人：平均（中央値） |
|---|---:|---:|
| 終了stress | 0.914 (0.961) | 0.594 (0.591) |
| stress sensitivity | 0.590 (0.610) | 0.125 (0.127) |
| 初期排泄欲求 / 空腹 / 疲労 | 0.525 / 0.494 / 0.501 | 0.561 / 0.500 / 0.467 |
| 終了排泄欲求 / 空腹 / 水分不足 / 疲労 | 0.568 / 0.840 / 0.979 / 0.794 | 0.432 / 0.857 / 0.988 / 0.764 |
| 睡眠負債（分） | 1,422 (1,440) | 1,420 (1,440) |
| 不満 / 負の感情 / 不確実性 | 1.000 / 0.937 / 0 | 1.000 / 0.594 / 0 |
| 衛生need | 0.131 (0.111) | 0.097 (0.070) |

この一回では初期needの差は小さく、stress sensitivityの群差が大きい。終了時には両群とも空腹・水分不足・疲労・睡眠負債・不満が高く、感度の低い群もstress平均0.594まで上がっている。よって「needが高い人だけが高stressになる」という分離ではなく、共通の持続負荷が広くstressを上げ、個人感度の違いが0.7閾値を超えるかに強く関係した可能性がある。ただし感度で層別した事後比較であり、因果推定や複数seedでの一般化ではない。negative affectはstressと一緒に高くなるが、現行stress式へ直接戻らない。

24時間を通したAgentあたりの寄与積分と1 Agent-minuteあたりの平均は次のとおり。各寄与はstress状態スケールのdeltaであり、物理単位や臨床単位ではない。

| 寄与 | 通常：24h積分 / 毎分平均 | 不足：24h積分 / 毎分平均 |
|---|---:|---:|
| 自然回復 | -9.198 / -0.006387 | -9.447 / -0.006560 |
| 未充足need | +4.861 / +0.003376 | +4.871 / +0.003383 |
| life-state feedback | +3.176 / +0.002206 | +3.422 / +0.002376 |
| 施設queue | +2.201 / +0.001528 | +2.275 / +0.001580 |
| staff support待ち | +0.049 / +0.000034 | +0.044 / +0.000031 |
| 環境stress / 誤情報 | 0 / 0 | 0 / 0 |

全Agentについて24個の時間別traceをAgentごとに合算した分布（Agentあたりstress-unit/日、p10 / 中央値 / p90）は次のとおり。need pressureのagent間分散が特に大きいが、life-stateとqueueの正入力は全体に持続している。

| 寄与 | 通常運営 p10 / 中央値 / p90 | 資源不足 p10 / 中央値 / p90 |
|---|---:|---:|
| 自然回復 | -12.328 / -9.431 / -5.614 | -12.562 / -9.545 / -6.149 |
| 未充足need | +0.883 / +4.727 / +9.061 | +0.907 / +4.719 / +9.043 |
| life-state feedback | +3.001 / +3.178 / +3.383 | +2.895 / +3.250 / +4.216 |
| 施設queue | +1.940 / +2.239 / +2.420 | +1.940 / +2.247 / +2.685 |
| staff support待ち | 0 / +0.025 / +0.124 | 0 / +0.029 / +0.125 |

通常運営では平均の正入力合計が約 `+0.007144/min`、不足は `+0.007369/min`。自然回復を含むraw netはそれぞれ約 `+0.000756/min`、`+0.000809/min` だった。時間変化・個人差・上限clipを含むため、これを一定外力固定点と解釈しない。7日記録でも自然回復は毎tick働き、正寄与の合計（need +3,584.73、life-state +4,396.14、queue +1,572.09、support +19.64）が自然回復 -8,624.44を上回った。

### life-state項の内訳

実装されたfeedback式と1日traceの状態平均から、加法項を再集計した。sanitation deficitは、式の既知項とtraceされたlife-state合計の差から逆算している。表示値は24時間・全Agentで積分したstress-unit/Agent。

| life-state項 | 通常 | 不足 | 符号と解釈 |
|---|---:|---:|---|
| sleep debt | +0.813 | +0.815 | 最大1,440分の負債を毎tick入力 |
| uncertainty | +0.136 | +0.126 | 小さい正入力。誤情報pressure自体は0 |
| discontent | +1.012 | +1.024 | ストレスと未充足needにより上がり、feedbackへ戻る |
| hygiene need | +0.237 | +0.243 | 衛生needの正入力 |
| sanitation deficit | +1.148 | +1.386 | sanitation level低下による正入力 |
| 家族分離pressure | +0.0002 | +0.0006 | このfixtureではほぼ0 |
| reassurance | -0.009 | -0.009 | 小さい回復方向 |
| positive affect | -0.162 | -0.163 | 回復方向 |
| negative affectの直接入力 | 0 | 0 | stateは増えるが、このstress式へ直接は戻らない |

終了時の平均sleep debtは通常約1,410分、不足約1,402分で、上限1,440分に近い。1日で選択されたsleep行動は通常97回（75人）、不足95回（71人）。これは睡眠状態更新の壊れた証拠ではなく、この行動Policy/60分判断間隔の合成条件では、起きている時間の負債増加に対して休息が少ないことを示す。最終stressは睡眠中も回復項の設定どおり減るが、他の外力が同時に残る。

## サービス・need・短時間ablation

通常運営の完了件数はトイレ146、水251、食事62、不足はトイレ118、水251、食事20。トイレ完了で排泄needはサービス前平均0.874下がり、そのtickのneed更新後は0.001だった。水サービスは設定どおりhydration needを0.35、食事サービスはhungerを0.5下げていた。したがって完了eventがneedに反映されない実装不具合は再現しなかった。一方で残量は直ちに0にならず、need成長が続く。

このfixtureの水分need growthは `0.006/min`（上限前なら8.64 normalized units/人/日）、水1回は250 mlでneedを0.35下げる。通常運営の完了平均2.51回/人/日による名目 relief は約0.879 normalized unit/日である。正規化状態とmlの対応は定義・校正されておらず、これは実際の飲水量の需給計算ではなく、Scenario内で需要とサービスが釣り合っていない証拠である。

12人・180分・seed 2026の同一条件ablationでは、最終stress平均はbaseline 0.2268、直接need stress入力を0にした場合0.0929、sleep-debt feedback rateを0にした場合0.2150、discontent rateを0にした場合0.1672、queue入力を0にした場合0.2212、水分need growthを0にした場合0.1523だった。各1 seed・短時間の記述結果なので、長期効果量の推定には使わない。need入力と水分need growthが早期stressへ強く作用し、睡眠負債は短時間では小さくても長時間で高止まりを支える候補と整合する。

## 仮説ごとの判定

| 仮説 | 判定 | 根拠 |
|---|---|---|
| 自然回復が適用されない | 支持されない | 一定外力の解析更新・細分化一致テストと、高stress初期値の回復Runner testが成功。1日Runでも自然回復は合計 -9.20/-9.45 per Agent。 |
| 外力が自然回復に勝つ | 支持 | 24hと7日で正寄与の積分が回復と同程度以上。 |
| サービス後にneedが変化しない | 支持されない | toilet=0、water=-0.35、meal=-0.5の設定効果を直後のtraceで確認。成長と行動頻度が残る。 |
| 睡眠更新が動かない | 支持されない | sleep action/transitionがあり、debtは更新される。平均debtが上限近くまで増え、休息頻度が十分でない。係数・行動頻度は未校正。 |
| negative affectがstressを直接増幅 | 現行式では該当しない | `negative_affect` はstressを受けて増えるが、body formulaのstress入力はuncertainty、discontent、sleep debt、hygiene/sanitation等。positive affectとreassuranceは負入力。 |
| discontentの正feedback | あり、ただし飽和する | stress→discontent→stressのtick間経路。指定係数で線形化固有値はわずかに1超。`D=1` clip後は有界な正入力となる。 |
| clipが差を隠す | 支持 | 1日で13–15%のAgent-minuteにclip前候補>1。Policy traceのclip差は不足側で大きい。7日p95は両条件1.0で、threshold share差は3ポイント。 |
| 誤情報・環境・social contagionが主因 | 支持されない | 対象fixtureで情報・環境・伝播入力は無効または0。施設queueの直接入力と型付きsocial/care eventは別。 |

## 原因の分類と改善の順番

### A. 明確な実装バグ

この調査では、高stress継続の原因となる再現可能なruntime bugは確認していない。平均回帰と、トイレ・水・食事サービス後のneed反映は機能している。

契約上の確認事項として、`negative_affect_stress_rate` という名前の係数がstress式では `uncertainty` に掛かり、`negative_affect` 自体には掛からない。実装とModel Catalogの式は一致しているので、現時点ではruntime bugと断定しない。係数名・単位・意図を設計者が確認し、negative affectからstressへの経路が要件なら別のversioned式・migration・テストとして扱う。

### B. 数理モデルの構造的課題

1. 閾値 `stress >= 0.7` と上限clipにより、1.0へ近づいた条件間の運営差が失われる。比較KPIには急性stressの中央値/p95、Agent-minute、高いraw candidateの割合、回復までの時間を併記する。
2. `discontent` はストレス比例で上がり、同じ状態がstress入力になる。線形化上の正feedbackは弱いが、ストレスを維持する項となる。減衰を加える等の変更は候補に留め、避難所の反復時系列に対する推定後に判断する。
3. stressはclipされた1つの指標で、平均回帰・need・睡眠負債・衛生・sanitation・行動・支援の時間尺度が結合している。急性状態と累積負荷を別状態として扱う案は、仕様・Checkpoint version・再生テストを含む設計課題である。
4. ユーザーの循環例のうち、実際の経路は `stress → discontent → stress`。`stress → negative_affect` は状態更新にあるが、negative affectからstressへの直接辺は現行式にはない。

### C. 未校正パラメータ・Scenario

`κ=0.01/min`、need growth/threshold、`stress_pressure=0.012`、sleep-debt/discontent/sanitation rates、250 mlあたりneed relief、行動判断間隔、初期need分布は、出典台帳で合成・仮定に分類され、実測の避難所心理時系列に校正されていない。通常運営と名付けたfixtureも実環境の基準条件ではない。係数を高stressを下げる目的だけで調整してはいけない。

優先順位:

1. **先にデータ定義を整える（1–2人日、入手可能な運用データが前提）**: 水・食事・トイレの実測需要/完了時刻、sleep/rest time、sanitation、支援待ち、主観stressを同じ時系列にし、normalized needから現実量への対応と初期分布を定義する。
2. **一要因感度と複数seedを追加（1–2人日＋計算時間）**: 今回短時間で効いたneed/water/discontent、続いてsanitation/sleepを、少なくとも通常・不足の同seed対応条件で比較する。1日から候補を絞り、必要な条件だけ7日へ延長する。
3. **モデル変更は根拠後（設計・実装で数日規模）**: discontentの緩和、stress/affectの結合、clip/KPI設計を別案として比較する。Scenario/Checkpoint version・source manifest・offline replay testsを同時に更新する。実測係数の取得・専門家レビューは別途必要で、数日程度の実装だけでは校正完了にならない。

## 外部研究との境界

経験サンプリング研究では、感情状態の基準点、外力への感度、回帰の強さを個人差のある動的特性として扱い、単一の平均回帰係数が全員に当てはまるとはしていない。一方、これらは避難所の0–1 stress、1分単位の係数や睡眠/水分need係数を与えるものではない。[Kuppens, Oravecz & Tuerlinckx (2010)](https://pubmed.ncbi.nlm.nih.gov/20853980/)。

災害後の大規模縦断研究では、安定低値から高値持続まで複数のdistress軌跡が観察される。これは人口全員を同じstress軌道に置くことへの検証要求を示すが、個別研究の割合をShelterSimの合成Agentへ直接移せない。[Ko et al., WTC Health Registry 15-year trajectories](https://pubmed.ncbi.nlm.nih.gov/33823957/)。東日本大震災後のIwanuma高齢者コホートでも、発災前を含む3時点の追跡で、発症した症状の一部は5.5年までに回復し、群の有病率と個人軌跡は異なることが報告されている。[Long-term Trends in Mental Health Disorders After the 2011 Great East Japan Earthquake and Tsunami](https://pmc.ncbi.nlm.nih.gov/articles/PMC7428812/)。

これらの研究は状態の異質性や反復測定の必要性を支えるが、ShelterSimの係数を校正しない。今回の実験は内部機構の整合性確認であり、臨床的・実地の予測精度を示さない。
