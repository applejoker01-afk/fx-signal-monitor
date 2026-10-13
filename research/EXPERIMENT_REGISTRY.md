# Signal experiment registry

This is a pre-registration queue for the scheduled research workflow. A result
must be recorded here before another variant of the same idea is tested.

| ID | Hypothesis | Fixed candidate rules | Status | Required evidence before production review |
|---|---|---|---|---|
| EXP-001 | RSI reversal plus MACD confirmation improves entry quality | RSI recovers above 40 after dipping below 40 within 10 bars; MACD line crosses above signal for longs, inverse for shorts | queued | Cost-inclusive rolling OOS improvement versus current baseline; multiple-testing adjustment |
| EXP-002 | Bollinger squeeze followed by directional break identifies tradable expansion | 20-period, 2-sigma bandwidth below its trailing 20th percentile; close breaks the matching band with trend confirmation | queued | Same as EXP-001, evaluated separately by volatility regime |
| EXP-003 | 50/200 moving-average regime filter reduces trend-following false positives | Long only when 50DMA is above 200DMA and price is above both; inverse condition for shorts | queued | Same as EXP-001, with a no-filter baseline |
| EXP-004 | Bearish TA/FA agreement produces viable short signals | Mirror the existing long score thresholds only where technical and fundamental directions agree | queued | Long/short-separated, cost-inclusive rolling OOS improvement |

## 2026-10-13

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

## Status vocabulary

- `queued`: rules are written but no run has been recorded.
- `running`: one pre-registered configuration is under evaluation.
- `hold`: data, cost model, or sample size is insufficient.
- `rejected`: failed its pre-registered gate; do not retune without a new ID.
- `review`: passed research gates and may be considered manually for a separate implementation PR.
