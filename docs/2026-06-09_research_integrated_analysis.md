# 2026-06-09 研究結果統合シグナル分析

**Source**: claude-obsidian wiki autoresearch（金利動向×ML×通貨ペア）
**Full report**: `C:\Users\user\claude-obsidian\wiki\finance\2026-06-09-investment-signal-analysis.md`
**Signal snapshot**: 2026-06-09T12:12 UTC
**Method**: 学術研究（IRP理論、キャリートレード、LSTM/XGBoost）を fx-signal-monitor L3.1 シグナルに適用

---

## エグゼクティブ・サマリー

### 🔴 即時アクション（24時間以内）

| ペア | 現在シグナル | 推奨アクション | 根拠 |
|------|-----------|--------------|------|
| **CADJPY LONG** | ★4 | **エグジット** | BoC会合 25.6h後、trend_direction DOWN |
| **NZDJPY LONG** | ★4 | **損切検討** | レジスタンス強4が直近、勝率0% |
| **USDJPY LONG** | ★5 | **TP1半量利確** | 米CPI 24h後、SLをBE移動 |
| **EURUSD SHORT** | ★5 | **継続TP3狙い** | ECB緩和×FRB引き締めで完全合致 |
| **USDCHF LONG** | ★5 | **継続保有** | 金利差+3.50%、SNB緩和継続 |

### 🟡 中期アクション（1週間）

| イベント | 時刻 | 対象ペア | アクション |
|---------|------|---------|----------|
| 米国CPI | 24h | 全USD | ボラ拡大警戒 |
| BoC利率決定 | 26h | CADJPY | エグジット必須 |
| BoJ JGB Purchase | 39h | 全JPY | 縮小なら円高材料 |
| ECB金利決定 | 48h | EUR | EURUSD SHORT追加調整 |
| BoJ Interest Rate | 158h | 全JPY | **全JPYロング縮小** |
| RBA Interest Rate | 160h | AUD | AUDJPY新規禁止 |

---

## 研究知見の現状適用（主要ファインディング）

### 1. キャリートレード（[[carry-trade]]）

**研究結果**: 2025年でJPYキャリー収益性悪化、BOJ tighten転換でスプレッド縮小

**現状検証**:
- HKDJPY carry_score 20.18（最高、45日でSL回収）
- USDJPY carry_score 19.42（46日）
- ⚠️ 6/16 BoJ で 0.75% → 1.00% なら全キャリーが約30%圧縮

### 2. 金利動向の通貨ペア影響（[[central-bank-rate-impact]]）

**研究結果**: 中央銀行政策の divergence が為替トレンドを駆動

**現状検証**:
- USD（tighten）vs EUR（ease）= EURUSD SHORT極めて優位
- USD（tighten）vs CHF（ease）= USDCHF LONG優位
- USD（tighten）vs JPY（tighten） = USDJPYは「両者引き上げ」で金利差変動なし
- AUD（neutral 4.35%）vs JPY（tighten 0.75%）= 金利差大だが ADX 4.8 でレンジ→順張り不利

### 3. ML モデル選択（[[ml-model-performance-comparison]]）

**研究結果**: ペアごとに最適モデルが異なる

| ペア | 最適モデル | 現状システムへの示唆 |
|------|----------|-------------------|
| USDJPY | LSTM (R²=0.9234) | テクニカル重み再評価 |
| EURUSD | XGBoost (R²=0.9694) | 現★5シグナルは信頼性高 |
| AUDJPY | Random Forest | レンジ検出が必須 |

---

## 統合判定: 9オープンポジションの再評価

### ✅ 継続推奨（3ポジション）
- **USDJPY LONG**: TP1半量利確、SL→BE移動
- **EURUSD SHORT**: TP3まで継続
- **USDCHF LONG**: 継続保有

### ⚠️ 部分決済推奨（3ポジション）
- **HKDJPY LONG**: TP1（20.47）半量利確
- **SGDJPY LONG**: TP1（124.6）半量利確、押し目候補(50EMA)で再エントリー検討
- **GBPJPY LONG**: レジスタンス213.845突破確認待ち、突破なければ全量決済

### ❌ 撤退推奨（3ポジション）
- **CADJPY LONG**: BoC会合（26h）前にエグジット
- **NZDJPY LONG**: 損切検討（既に-0.72%、レジスタンス強4）
- **EURGBP SHORT**: 監視（4★維持だがECB会合48h後で要再評価）

---

## バックテスト・改善提案

### Phase 1: XGBoost 統合（実装容易、推奨）

研究結果: XGBoost が **EURUSD で R²=0.9694** を達成

```python
# modules/ml_signal_layer.py (新規作成)
import xgboost as xgb
from sklearn.preprocessing import StandardScaler

class MLSignalLayer:
    def __init__(self):
        self.models = {}  # ペア別モデル
    
    def predict_direction(self, pair, features):
        """
        features: dict
          - dma200_ratio: float
          - rsi: float (0-100)
          - macd_histogram: float
          - fa_rate_diff: float
          - vix: float
          - dxy_change: float
        
        returns: (direction, confidence)
        """
        # XGBoostで方向確率を予測
        # 既存 ta_score, fa_score に重み付けして統合
        pass

# signal_scanner.py の evaluate_full() で:
ml_direction, ml_conf = ml_layer.predict_direction(pair, features)
if ml_direction == fa_direction and ml_conf > 0.75:
    stars += 1  # ML確認ボーナス
```

### Phase 2: バックテストコマンド（推奨実行）

```bash
# 過去2年の金利上昇・下降両局面でWalk-forward検証
cd E:/files/fx-signal-monitor
python run_backtest.py --pair USDJPY --period 24months --regime both
python run_swap_hold_validation.py --pair AUDJPY --carry-threshold 3.0
python run_threshold_optimization.py --pair EURUSD --target sharpe
```

### Phase 3: 日銀利上げ準備（緊急）

```bash
# 全JPYペアでBoJ会合前のヒストリカル分析
python run_bond_validation.py --currency JPY --event "BoJ Interest Rate Decision" --window 7d
```

---

## オープン質問（次回検証）

1. **6/16 BoJ会合の織り込み度**: OISスワップから 0.75→1.00% の確率を抽出する方法
2. **撤退タイミング最適化**: BoJ会合 24h前 vs 48h前 vs 1週間前のバックテスト
3. **キャリー閾値の動的調整**: 金利上昇局面ではCarry スコア基準を引き上げるべき
4. **ML統合の優先順位**: XGBoost（容易）→ LSTM（高精度）→ RF（レジーム検出）の順序確定

---

## 関連ファイル

- 完全レポート（ウィキ）: `C:\Users\user\claude-obsidian\wiki\finance\2026-06-09-investment-signal-analysis.md`
- 元シグナル: `docs/last_signals.json`
- 金利データ: `data/central_bank_rates.json`
- 経済カレンダー: `data/economic_calendar.json`
- オープンポジション: `data/open_trades.json`

研究背景:
- [[interest-rate-parity]] — IRP理論
- [[carry-trade]] — キャリートレード戦略
- [[central-bank-rate-impact]] — 中銀政策の影響
- [[lstm-forex-forecasting]] — LSTM活用
- [[transformer-models-fx]] — Transformer/Self-attention
- [[quantitative-fx-trading-ml-models]] — 統合システム設計
