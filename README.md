# FX売買シグナル監視システム L3 + Obsidian Wiki統合版 

> \*\*テクノ・ファンダメンタル戦略 + 個人ナレッジベース統合\*\*
> テクニカル指標 × 動的FAスコア × 経済指標カレンダー × 市場センチメント × \*\*Obsidian Wiki知識\*\*

\---

## レベル3+の特徴

|評価軸|データソース|内容|
|-|-|-|
|**テクニカル**|Frankfurter API（ECB）|200/50DMA, MACD, RSI, ATR|
|**金利・金利差**|手動JSON + 米財務省API|中央銀行政策金利の動的反映|
|**経済指標**|手動JSON（月初メンテ）|FOMC・ECB・NFP・CPI等の前後で取引控え|
|**市場センチメント**|Stooq|VIX・DXY・米10年債・金価格|
|**🆕 Obsidian Wiki**|Private GitHub repo|個人の投資戦略・分析・取引日記・教訓を自動反映|

\---

## ファイル構成

```
fx-signal-monitor/
├── signal\_scanner.py                 ← メインスクリプト
├── modules/
│   ├── \_\_init\_\_.py
│   ├── rate\_fetcher.py               ← 金利・債券利回り取得
│   ├── event\_filter.py               ← 経済指標近接判定
│   ├── sentiment\_monitor.py          ← VIX/DXY/金センチメント
│   ├── obsidian\_extractor.py         ← 🆕 Obsidian Vault読込
│   └── custom\_rules\_engine.py        ← 🆕 Wikiルール適用エンジン
├── data/
│   ├── central\_bank\_rates.json       ← 中央銀行金利（手動メンテ）
│   ├── economic\_calendar.json        ← 経済指標カレンダー（手動メンテ）
│   ├── economic\_calendar.json        ← 経済指標カレンダー（手動メンテ）
│   └── sentiment\_cache.json          ← 自動生成（前回値キャッシュ）
├── docs/
│   ├── index.html                    ← 自動生成ダッシュボード
│   └── last\_signals.json             ← 自動生成（差分検出用）
├── .github/workflows/
│   └── fx\_signal\_monitor.yml         ← GitHub Actions定義
└── README.md
```

\---

## セットアップ

### 基本セットアップ（既存ユーザーは飛ばしてOK）

### 1\. リポジトリにファイルをアップロード

GitHubリポジトリに以下のフォルダ構造でファイルをアップロードします：

```
リポジトリ直下
├── signal\_scanner.py
├── README.md
├── modules/
│   ├── \_\_init\_\_.py
│   ├── rate\_fetcher.py
│   ├── event\_filter.py
│   └── sentiment\_monitor.py
├── data/
│   ├── central\_bank\_rates.json
│   └── economic\_calendar.json
└── .github/workflows/
    └── fx\_signal\_monitor.yml
```

**アップロード方法**：

1. `Add file → Upload files`
2. パス欄に必要に応じて `modules/`, `data/`, `.github/workflows/` を入力
3. ファイルをドラッグ\&ドロップ
4. `Commit changes`

### 2\. GitHub Secrets の設定

`Settings → Secrets and variables → Actions → New repository secret` で以下を登録：

|Secret 名|値|必須|
|-|-|:-:|
|`DISCORD\_WEBHOOK\_URL`|Discord WebhookのURL|△|
|`SMTP\_HOST`|`smtp.gmail.com`|△|
|`SMTP\_PORT`|`465`|△|
|`SMTP\_USER`|Gmailアドレス|△|
|`SMTP\_PASS`|Gmail App Password（16桁）|△|
|`MAIL\_FROM`|送信元（SMTP\_USERと同じ）|△|
|`MAIL\_TO`|通知受信先|△|

DiscordかメールどちらかでもOK。両方設定すれば二重通知。

### 3\. GitHub Pages を有効化

`Settings → Pages → Source: GitHub Actions` を選択。

### 4\. 初回実行

`Actions → FX Signal Monitor → Run workflow` で手動実行。

\---

## 🆕 Obsidian Wiki 統合（任意・推奨）

ナナのパパさんのObsidian Vault内に蓄積された投資戦略・分析・取引日記・教訓を、L3スキャナーが自動的に読み取り、シグナル判定に反映します。

### セットアップ手順

詳細な手順は別途用意した以下のガイドを参照してください：

* **`docs\_setup/01\_Vault同期セットアップガイド.md`**：Obsidian VaultをGitHub Privateリポジトリで同期する手順
* **`docs\_setup/02\_YAML\_frontmatter\_標準スキーマ.md`**：Wikiノートに付ける標準frontmatter形式

### 概要

1. Obsidian Git Plugin で Vault を **Private** リポジトリに同期
2. **Fine-grained Personal Access Token (PAT)** を Read-only 権限で発行
3. `fx-signal-monitor` の Secret に以下を追加：

   * `OBSIDIAN\_VAULT\_PAT` : 発行したPAT
   * `OBSIDIAN\_VAULT\_OWNER` : GitHubアカウント名（例: `applejoker01-afk`）
   * `OBSIDIAN\_VAULT\_REPO` : リポジトリ名（例: `obsidian-vault`）
   * `OBSIDIAN\_VAULT\_PATH` : 読み取り対象パス（例: `02\_Domains/finance`）

### ノートの書き方

Obsidian Vault の `02\_Domains/finance/` 配下に、以下のような frontmatter 付き Markdown を作成すると、L3が自動的に読み取って反映します。

#### 例1: シグナルルール（signal\_rule）

```markdown
---
domain: finance
type: signal\_rule
status: active
pairs: \[USDJPY]
priority: high
confidence: 0.85
tags: \[介入, USDJPY, リスク管理]
rule:
  name: "USDJPY 介入警戒水準"
  when:
    - "price >= 155.0"
  then:
    action: downgrade\_long
    severity: 2
---

# USDJPY 介入警戒水準

財務省の介入実績から、USDJPYが155円を超えた時点で...
```

→ USDJPYが155円超になると、ロングシグナルが自動で2段階下がります

#### 例2: 取引分析（analysis）

```markdown
---
domain: finance
type: analysis
pairs: \[AUDJPY]
title: "AUDJPY 2026年下期見通し"
---

# AUDJPY 2026年下期見通し

RBAの金利据え置きと中国需要を背景に...
```

→ AUDJPYのシグナル発生時、この分析が通知に自動添付されます

#### 例3: 取引日記（journal）

```markdown
---
domain: finance
type: journal
pairs: \[USDJPY]
trade\_date: 2026-05-15
result: profit          # profit / loss / breakeven
pips: 45
---

# 2026-05-15 USDJPYロング

エントリー: 156.50円、決済: 156.95円...
```

→ 過去30日の同通貨ペア勝率が通知に表示されます

#### 例4: 教訓（lesson）

```markdown
---
domain: finance
type: lesson
pairs: \[all]
priority: high
trigger\_pattern:
  - "macd\_divergence == 'bearish'"
  - "rsi > 65"
---

# ピラミッディング失敗教訓

買い増しタイミングを早まり…
```

→ 該当条件が成立した時、過去の教訓が通知に添付されます

### 動作モード

|状況|L3の動作|
|-|-|
|OBSIDIAN\_VAULT\_PAT 未設定|Obsidian統合をスキップ。既存機能のみで動作（後方互換）|
|Vault接続成功・ノート0件|既存機能のみで動作|
|Vault接続成功・ノートあり|各シグナル評価時にWikiルール適用・関連分析添付|
|Vault接続失敗（PAT期限切れ等）|警告ログを出して既存機能のみで継続動作|

**重要**：Obsidian統合は完全に任意機能です。設定しなくてもL3の既存機能は何も影響を受けません。

\---

## 手動メンテナンス（月1回・約15分）

### A. 中央銀行金利の更新

`data/central\_bank\_rates.json` を編集。中央銀行会合があった通貨だけ更新：

```json
"USD": {"rate": 4.50, "next\_meeting": "2026-07-30", "stance": "ease", "cb\_name": "FRB"}
                ↑ 4.75から4.50に変更         ↑ 次回会合日も更新   ↑ スタンスも変更
```

**`stance` の意味**：

* `tighten`：引き締めバイアス（さらなる利上げを示唆）
* `neutral`：中立（金利据え置きが基本）
* `ease`：緩和バイアス（さらなる利下げを示唆）

### B. 経済指標カレンダーの更新

`data/economic\_calendar.json` に翌月分のイベントを追加：

```json
{
  "date": "2026-07-30T18:00:00Z",
  "country": "US",
  "currency": "USD",
  "name": "FOMC",
  "importance": "critical",
  "affects\_pairs": \["USDJPY", "EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF"]
}
```

**`importance` の意味**：

* `critical`：48h前から取引控え推奨（FOMC・ECB・NFP・各国中銀会合）
* `high`：24h前から警戒（CPI・GDP・PMI・要人発言）
* `medium`：情報提供のみ（取引制限なし）

参考：日本の経済指標発表予定は[内閣府](https://www.cao.go.jp/)、米国は[BLS](https://www.bls.gov/)、欧州は[ECB](https://www.ecb.europa.eu/)から月初に確認。

\---

## 通知例

### 平常時

```
🚨 FXシグナル変化検出
新規★4以上: 1件 / ★4→5昇格: 0件
市場モード: NORMAL

🌐 市場センチメント:
  VIX: 16.2 (normal)
  DXY: 105.2 (flat)
  米10y: 4.35%
  金: 2350.0

★★★★★ AUD/JPY - ◎ 高信頼ロング
価格: 114.39
TA: 82/100  FA: 75/100
金利差: +3.35%
📊 RBA(4.10% neutral) vs 日銀(0.75% tighten) 差+3.35%
```

### イベント前

```
⏸ USDJPY - イベント前自動見送り
  本来★5だったところを抑制
  🛑 取引控え: 重要イベント前 (FOMC まで 23.5h)
```

### パニック時

```
🚨🚨🚨 市場パニック警告
市場モード: PANIC

🌐 市場センチメント:
  VIX: 32.8 (panic)
  DXY: 106.5 (strong) ↑↑
  金: 2480.0 (surging) ↑

⛔ 新興国通貨3ペアを取引停止:
  ・TRY/JPY (本来★3→★1)
  ・ZAR/JPY (本来★4→★1)
  ・MXN/JPY (本来★4→★1)

⚠ USD/JPY: 円買い圧力警戒
```

\---

## カスタマイズ

### しきい値の調整

`signal\_scanner.py` の `evaluate\_full()` 内のスコア閾値を編集：

```python
# 現状: TA≥75 かつ FA≥65 で★5
if agree and ta\["ta\_score"] >= 75 and fa\["score"] >= 65:
    stars = 5
```

### 監視ペアを絞る

`PAIR\_API` 辞書から不要なペアを削除。

### センチメント感度を調整

`modules/sentiment\_monitor.py` の `evaluate\_market\_sentiment()` 内のVIX閾値を編集：

```python
elif vix > 25:    # ←ここを変更
    vix\_level = "risk\_off"
```

\---

## トラブルシューティング

### Stooqからデータが取得できない

Stooqは安定したフリーサービスですが、稀に一時障害が発生します。`sentiment\_cache.json` から前回値が使われるため、シグナル評価は継続できます。

恒久対策が必要な場合は、`modules/sentiment\_monitor.py` の `fetch\_vix()` などをYahoo Finance APIに切り替えることも可能です。

### 経済指標カレンダーの更新を忘れた

経済指標が空または古い場合、イベントフィルターは何も警告を出しません（取引控えなし）。月初には必ず確認してください。

GitHub Issues に「月1の Reminder Issue」を作っておくと安全です。

### 中央銀行金利が古い

中央銀行会合は月1〜8回程度。`central\_bank\_rates.json` を更新しないと、誤った金利差で評価され続けます。

\---

## ライセンスと免責

本システムは外国為替市場における中長期売買シグナル分析の教育的・研究的フレームワークです。**特定の金融商品の購入・売却を推奨するものではありません**。

実際の投資判断は、最新の市場情報・自己の財務状況・リスク許容度を踏まえ、自己責任のもと行ってください。

\---

**Currents FX Signal Monitor L3 / Techno-Fundamental Strategy / 2026 Edition**

