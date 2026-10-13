# Signal experiment registry

This is a pre-registration queue for the scheduled research workflow. A result
must be recorded here before another variant of the same idea is tested.

| ID | Hypothesis | Fixed candidate rules | Status | Required evidence before production review |
|---|---|---|---|---|
| EXP-001 | RSI reversal plus MACD confirmation improves entry quality | RSI recovers above 40 after dipping below 40 within 10 bars; MACD line crosses above signal for longs, inverse for shorts | queued | Cost-inclusive rolling OOS improvement versus current baseline; multiple-testing adjustment |
| EXP-002 | Bollinger squeeze followed by directional break identifies tradable expansion | 20-period, 2-sigma bandwidth below its trailing 20th percentile; close breaks the matching band with trend confirmation | queued | Same as EXP-001, evaluated separately by volatility regime |
| EXP-003 | 50/200 moving-average regime filter reduces trend-following false positives | Long only when 50DMA is above 200DMA and price is above both; inverse condition for shorts | queued | Same as EXP-001, with a no-filter baseline |
| EXP-004 | Bearish TA/FA agreement produces viable short signals | Mirror the existing long score thresholds only where technical and fundamental directions agree | queued | Long/short-separated, cost-inclusive rolling OOS improvement |

## Informal preliminary checks (not a substitute for the pre-registered gate)

- **2026-08-26, EXP-001 short side / EXP-004**: quick single-pass in-sample
  check (`run_macd_deadcross_rsi_short_test.py`, 27 tradable pairs, 180-day
  lookback, no cost model, no OOS split) of "MACD dead cross + RSI rebound
  (RSI<=40 within 10 bars then turning up)" as a SHORT entry gate.
  EXP-004's fixed rule (TA/FA direction agreement) produced **0 trades**
  across all pairs — for XXXJPY pairs specifically, FA direction is "buy" for
  34/34 pairs right now (positive carry vs BOJ), so requiring FA agreement
  structurally forecloses any JPY-cross short. Dropping the FA-agreement
  requirement and testing TA-only: base short (no gate) 98 trades, 38.8% win,
  PF 0.29; +MACD dead cross 36 trades, 41.7% win, PF 0.56; +RSI rebound only
  52 trades, 38.5% win, PF 0.20; +both 3 trades (too few to read). All
  variants net losing (PF<1); MACD dead cross alone is the least-bad filter
  but still unprofitable. This does not meet the evidence bar above (no
  costs, no OOS, no multiple-testing adjustment) — treat as a reason to
  deprioritize, not a formal rejection.

- **2026-08-26, EXP-001 long side, drift-controlled** (`run_macd_rsi_signal_vs_drift.py`):
  followed up on a user hypothesis that the short-side loss above implies a
  real long-side edge for the same "MACD golden cross + RSI rebound" combo.
  Compared forward returns starting on signal days vs forward returns
  starting on *any* trading day (the drift baseline), same pairs/period, no
  cost model. 14 JPY-cross pairs, 117 signal days vs 2565 baseline days:
  5d excess +0.015%, 10d excess +0.267%, 20d excess -0.127% (win rate 61%
  vs 56%, 60% vs 54%, 49% vs 53%). Per-pair excess ranges from +1.8% to
  -1.2% on n=1-11 each — noise-level sample sizes. Not consistent across
  horizons (sign flips at 20d) and not distinguishable from zero given the
  per-pair variance. Consistent with the 2026-08-11 result (V1/V2 as a LONG
  gate = opportunity cost): this combo does not show a reliable edge in
  either direction once drift is controlled for.

## 2026-09-09/10 構造診断・再設計フェーズ（EXP-005 / EXP-006）

ユーザー指摘「売買シグナルとして実際に使えるものではない」を受けた全面診断。
スクリプト: `research/analyze_structure_2026_09_09.py`, `counterfactual_signal_lost.py`,
`build_price_series.py`, `redesign_edge_search.py`, `redesign_risk_overlay.py`

### 診断（実運用81トレード, 2026-06-01〜08-21）

評価単位をraw pipsからR倍数（実現pips÷初期SL幅）に是正。ペア横断のpips合計は
経済的に無意味なため、過去の集計はすべて再解釈が必要。

- 全期間 n=81: 平均R **+0.034**, PF 1.14, **t=+0.41**, 95%CI [-0.130, +0.198] → ゼロと区別不能
- スプレッドフィルタ後 n=11: 平均R +0.068, **t=+0.16**, 95%CI [-0.758, +0.893] → 検証不能な標本サイズ
- 有意性に必要な n≈1,900（現行頻度で5年以上）

**反実仮想テスト**（git履歴から復元した毎時価格系列で、SIGNAL_LOST 60件を設計通り完走させた場合）:

| | n | 勝率 | PF | 平均R |
|---|---|---|---|---|
| 実際（SIGNAL_LOSTで途中決済） | 60 | 30.0% | 0.80 | -0.031 |
| 反実仮想（SL/TP/トレール完走） | 60 | 43.3% | 0.98 | -0.011 |

差分 +0.021R/件、対応t検定 t=+0.14 → **「途中決済をやめれば直る」仮説は棄却**。
問題は出口ではなくエントリーに予測力がないこと。

**フィルタ積層の影響**（スキャン履歴828スナップショット）: ★4+の生成が
6月5.53件/スキャン → 8月**0.47件/スキャン（-92%）**。にもかかわらず1件あたりの
平均Rは改善せず。**頻度のみが犠牲**になり、統計的検出力が失われた。

**過去のフィルタ根拠の崩壊**: ★5<★4逆転 → 現在★5(+0.154R)>★4(-0.020R)に反転。
ta_score逆U字 → 消失（Q4が+0.103Rで良い方）。どちらも当初からt<1で有意でなかった。

### EXP-005: 方向予測にエッジはあるか（22年USDJPY, train/test分割, コスト・スワップ込み）

| ID | 仮説 | 結果 | Status |
|---|---|---|---|
| EXP-005 | 標準的シグナル（TSMOM/CARRY/DCARRY/組合せ, 12変種）が買い持ちを超える | test期で買い持ちSharpe+0.70を超えた変種**ゼロ**（最良で超過+0.05）。12変種中5つがtrain/testで符号反転。trainで最良のDCARRY63日(t=2.16)はtestで+0.16に崩壊 | **rejected** |

### EXP-006: エクスポージャー管理にエッジはあるか（同データ, 8変種）

| ID | 仮説 | 結果 | Status |
|---|---|---|---|
| EXP-006 | ボラ連動のエクスポージャー調整がリスク調整後リターンを改善する | **リターン改善は否定**（全変種ΔSharpe±0.06以内）。ただし**最大DDと最悪20日はtrain/test両方で一貫して改善**（test最大DD 15.7%→10.3〜13.7%）。方向予測変種が符号反転を繰り返したのと対照的 | **review**（リスク低減目的に限定） |

> EXP-006の採用可能な主張は「ボラ連動でエクスポージャーを絞る**手法クラス**が一貫してDDを下げる」
> まで。特定閾値（例: ボラ>15%）の最適性は主張できない——testで最良だった閾値は
> trainでは平凡であり、閾値選択自体がこのプロジェクトの失敗パターンそのもの。

### EXP-007: 横断（クロスセクショナル）キャリー

データ: FRED の G10 9通貨（DEX系為替日次1971〜 + IR3TIB系3ヶ月金利）13,968営業日。
スクリプト: `research/fetch_g10_history.py`, `crosssectional_carry.py`, `carry_vs_shortjpy.py`
月次リバランス、片道2bpコスト、為替＋金利差の合計リターン、train(1982-2008)/test(2008-2026)。

| ID | 仮説 | 結果 | Status |
|---|---|---|---|
| EXP-007 | 高金利通貨ロング／低金利通貨ショートのポートフォリオがドリフト基準を上回る | **12変種すべてがtrain/test両方でプラス、符号反転ゼロ**。test Sharpe: キャリー3/3 +0.25 vs ドリフト基準 -0.14。1/1〜4/4のどのパラメータでもtest 0.19〜0.25でほぼ不変 | **review**（本調査で唯一OOSを生き延びた候補） |

**現行システムの実態との比較**（test期）:

| | Sharpe | 年率 | 最大DD | Calmar |
|---|---|---|---|---|
| 現行相当（常に円ショート） | +0.26 | +3.06% | 33.8% | 0.09 |
| 横断キャリー3/3 | +0.25 | +1.97% | **17.0%** | **0.12** |

リターンは同等、**DDは半分**。金利ランキングが足しているのは収益ではなく分散によるリスク低減。

**SBI制約下の実装可能版**（JPYクロスのロングのみ）: test Sharpe +0.33（年率+4.38%）だが
最大DD 35.2%、全期間では59.1%。ボラ管理（目標8%）併用で全期間DD 36.4%・Sharpe 0.35（t=2.32）に改善。

> [!warning] trainだけ見ていたら誤った実例
> 「最低金利通貨をショート」は train Sharpe +0.59（t=3.04）で最良だったが test で **-0.08 に崩壊**。
> OOS分割がなければ本番実装していた。

**前提条件の変化（2026-09時点）**: 現在の金利順位は NOK 4.57 > AUD 4.46 > GBP 3.71 > NZD 2.68
> CAD 2.27 > EUR 2.03 > SEK 1.95 > **JPY 1.27** > CHF -0.04。
**円は最低金利通貨ではなくなった**（CHFが最低）。対円最大金利差は 5.66%(3年前) → 3.30%(現在) に圧縮。
過去の円ショート収益をそのまま外挿してはならない。

### EXP-007b: 検証と実装の整合（JPY建て・USD含む）

旧EXP-007は USD を基準通貨として順位付けから除外していたが、実装では USDJPY を建てられるため
USD も順位対象になる。「検証した挙動＝運用する挙動」を保証するため再検証した。
スクリプト: `research/carry_jpy_numeraire.py`。対象 2002-04〜2026-09（JPY金利系列の開始に合わせ短縮）。

| 構成 | test(2016-2026) Sharpe | test年率 | test最大DD | 全期間最大DD |
|---|---|---|---|---|
| 旧: 3/3 USD除外 | +0.26 | +1.50% | 15.0% | 36.2% |
| **実装: diversified 3/3** | **+0.34** | +1.84% | **9.4%** | 36.2% |
| 実装: diversified 3/3 +ボラ8% | +0.34 | +1.84% | 9.4% | 26.9% |
| **実装: long_only 上位3** | **+0.66** (t=2.05) | +6.09% | 17.6% | **59.3%** |
| 実装: long_only 上位3 +ボラ8% | +0.64 | +5.44% | 17.8% | 36.1% |

USDを順位に含めた方が分散版は改善（test最大DD 15.0%→9.4%）。
long_only は高リターンだが全期間DD 59.3%、ボラ管理併用で36.1%。

**本番実装**: `modules/carry_portfolio.py` / `modules/paper_ledger.py` / `run_carry_paper.py`。
ペーパー運用のみ・発注機能なし。分散版と長期のみ版を同時並行で記録する。

**運用上の既知の弱点（実行時に自動警告）**:
- 検証はFRED IR3TIB（3ヶ月インターバンク）だが、月次＋公表遅延で2026-09時点のEUR/GBPは8ヶ月前の値。
  ライブは政策金利（`central_bank_rates.json`）を主系列にし、FREDと照合して乖離を警告する。
- 上位3の3番目がデータ源で入れ替わる（政策金利ならGBP 3.75%、FREDならUSD 3.63%、差0.12%）。
  この僅差問題へのヒステリシス導入は**新パラメータ**なので、事前登録と検証を経ずに入れないこと。

### EXP-008: 横断モメンタムのキャリーへの重ね合わせ（2026-10-13 事前登録）

**登録は実行前に行った。以下のルールは結果を見た後に変更しない。**

- 仮説: キャリー順位と横断モメンタム順位の合成は、キャリー単独よりリスク調整後成績が良い。
  機序: キャリーの最大の弱点は急激な巻き戻し（クラッシュ）で、その局面では高金利通貨の
  直近リターンが悪化しているため、モメンタムが高金利通貨のウェイトを自動的に下げる
  （Asness-Moskowitz-Pedersen 2013、Menkhoff et al. 2012）。
- データ・母集団: EXP-007bと同一（`crosssectional_carry.build()`, JPY建て, USD含む10通貨,
  train=先頭60%/test=残り40%）。
- モメンタム: 各通貨のJPY調達超過リターン（為替+金利差）の累積を、252営業日前〜21営業日前で計測（12-1）。
- 合成ルール（主仮説・1変種のみ）: リバランス日に キャリー順位 + モメンタム順位 の和で並べ、
  上位3ロング/下位3ショート等ウェイト（同点はキャリー順位優先）。21営業日ごとリバランス。
- ベースライン: キャリー3/3（EXP-007b「実装と同じ 3/3 USD含む」）。
- コスト: 片道1bp/2bp/5bpの3シナリオ。判定は2bp。
- **判定ゲート（すべて満たせば review）**: ①test期の Sharpe差（合成−キャリー）> 0
  ②train期の Sharpe差 > 0（符号反転なし） ③test期の最大DDがキャリーの1.2倍以内
  ④5bpでも①が成立。1つでも外れれば rejected。パラメータ（12-1, 3/3, 等ウェイト）の再調整は禁止。
- 参考出力（判定には使わない）: モメンタム単独3/3。
- スクリプト: `research/xs_momentum_overlay.py`

| ID | 仮説 | 結果 | Status |
|---|---|---|---|
| EXP-008 | キャリー+横断モメンタム合成がキャリー単独を上回る | 4ゲートすべてFAIL。test Sharpe +0.22 vs キャリー +0.34、test最大DD 15.6% vs 9.4%。モメンタム単独はtestで -0.11。trainでのDD低減(36→27%)はOOSで再現せず | **rejected** |

結果ノート: `research/results/2026-10-13_EXP-008_xs_momentum_overlay.md`

### 多重検定に関する注記

本調査で累計33変種を検証（フェーズ1:12 / フェーズ2:8 / キャリー:5 / 実装可能版:7 / EXP-008:1）。
単発の t≈2 は有意とみなさない。EXP-007を他と区別して扱う根拠は t値ではなく、
①全パラメータで一貫 ②train/testで符号反転なし ③経済的機序が文献で確立、の三条件。

## Status vocabulary

- `queued`: rules are written but no run has been recorded.
- `running`: one pre-registered configuration is under evaluation.
- `hold`: data, cost model, or sample size is insufficient.
- `rejected`: failed its pre-registered gate; do not retune without a new ID.
- `review`: passed research gates and may be considered manually for a separate implementation PR.
