"""
economic_calendar.json に 2026年7月・8月の中銀会合を追加する
"""
import json

with open('E:/files/fx-signal-monitor/data/economic_calendar.json', 'r', encoding='utf-8') as f:
    data = json.load(f)

events = data if isinstance(data, list) else data.get('events', [])
existing_keys = set((e.get('date', ''), e.get('name', '')) for e in events)

july_events = [
    # === RBA 2026-07-07 ===
    {
        "date": "2026-07-07T02:30:00Z",
        "country": "AU", "currency": "AUD",
        "name": "RBA Interest Rate Decision",
        "importance": "critical",
        "affects_pairs": ["AUDJPY", "EURAUD", "GBPAUD", "AUDUSD", "AUDNZD", "AUDCAD"],
        "source": "manual",
        "actual": None, "estimate": None, "previous": 4.1
    },
    # === BOC 2026-07-15 ===
    {
        "date": "2026-07-15T14:00:00Z",
        "country": "CA", "currency": "CAD",
        "name": "BOC Interest Rate Decision",
        "importance": "critical",
        "affects_pairs": ["CADJPY", "USDCAD", "EURCAD", "GBPCAD", "AUDCAD"],
        "source": "manual",
        "actual": None, "estimate": None, "previous": 2.25
    },
    {
        "date": "2026-07-15T15:00:00Z",
        "country": "CA", "currency": "CAD",
        "name": "BOC Monetary Policy Report",
        "importance": "high",
        "affects_pairs": ["CADJPY", "USDCAD"],
        "source": "manual",
        "actual": None, "estimate": None, "previous": None
    },
    # === ECB 2026-07-23 ===
    {
        "date": "2026-07-23T12:15:00Z",
        "country": "EU", "currency": "EUR",
        "name": "ECB Interest Rate Decision",
        "importance": "critical",
        "affects_pairs": ["EURUSD", "EURJPY", "EURGBP", "EURAUD", "EURNZD", "EURCAD", "EURCHF"],
        "source": "manual",
        "actual": None, "estimate": None, "previous": 2.25
    },
    {
        "date": "2026-07-23T12:45:00Z",
        "country": "EU", "currency": "EUR",
        "name": "ECB Press Conference",
        "importance": "critical",
        "affects_pairs": ["EURUSD", "EURJPY", "EURGBP", "EURAUD"],
        "source": "manual",
        "actual": None, "estimate": None, "previous": None
    },
    # === FOMC 2026-07-28-29（statement on 29th） ===
    {
        "date": "2026-07-29T18:00:00Z",
        "country": "US", "currency": "USD",
        "name": "FOMC Interest Rate Decision",
        "importance": "critical",
        "affects_pairs": ["USDJPY", "EURUSD", "GBPUSD", "AUDUSD", "NZDUSD", "USDCAD", "USDCHF", "SGDJPY"],
        "source": "manual",
        "actual": None, "estimate": None, "previous": 4.5
    },
    {
        "date": "2026-07-29T18:30:00Z",
        "country": "US", "currency": "USD",
        "name": "FOMC Press Conference",
        "importance": "critical",
        "affects_pairs": ["USDJPY", "EURUSD", "GBPUSD", "AUDUSD"],
        "source": "manual",
        "actual": None, "estimate": None, "previous": None
    },
    # === BOJ 2026-07-30 ===
    {
        "date": "2026-07-30T03:00:00Z",
        "country": "JP", "currency": "JPY",
        "name": "BOJ Interest Rate Decision",
        "importance": "critical",
        "affects_pairs": ["USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "CADJPY", "CHFJPY", "SGDJPY", "HKDJPY"],
        "source": "manual",
        "actual": None, "estimate": None, "previous": 1.0
    },
    {
        "date": "2026-07-30T06:30:00Z",
        "country": "JP", "currency": "JPY",
        "name": "BOJ Governor Press Conference",
        "importance": "critical",
        "affects_pairs": ["USDJPY", "EURJPY", "GBPJPY", "AUDJPY", "SGDJPY"],
        "source": "manual",
        "actual": None, "estimate": None, "previous": None
    },
    # === BOE 2026-07-30 ===
    {
        "date": "2026-07-30T11:00:00Z",
        "country": "GB", "currency": "GBP",
        "name": "BOE Interest Rate Decision",
        "importance": "critical",
        "affects_pairs": ["GBPJPY", "EURGBP", "GBPUSD", "GBPAUD", "GBPCAD", "GBPCHF"],
        "source": "manual",
        "actual": None, "estimate": None, "previous": 4.25
    },
    # === RBA August 2026-08-04 ===
    {
        "date": "2026-08-04T02:30:00Z",
        "country": "AU", "currency": "AUD",
        "name": "RBA Interest Rate Decision",
        "importance": "critical",
        "affects_pairs": ["AUDJPY", "EURAUD", "GBPAUD", "AUDUSD", "AUDNZD"],
        "source": "manual",
        "actual": None, "estimate": None, "previous": None
    },
    # === BOE August 2026-08-06 ===
    {
        "date": "2026-08-06T11:00:00Z",
        "country": "GB", "currency": "GBP",
        "name": "BOE Interest Rate Decision",
        "importance": "critical",
        "affects_pairs": ["GBPJPY", "EURGBP", "GBPUSD", "GBPAUD", "GBPCAD"],
        "source": "manual",
        "actual": None, "estimate": None, "previous": None
    },
]

added = 0
for evt in july_events:
    key = (evt["date"], evt["name"])
    if key not in existing_keys:
        events.append(evt)
        existing_keys.add(key)
        added += 1

print(f"Added {added} events. Total: {len(events)}")

if isinstance(data, list):
    out = events
else:
    data["events"] = events
    out = data

with open('E:/files/fx-signal-monitor/data/economic_calendar.json', 'w', encoding='utf-8') as f:
    json.dump(out, f, ensure_ascii=False, indent=2)

# 7月以降の確認
july_plus = [e for e in events if e.get("date", "") >= "2026-07"]
print(f"\n7月以降のイベント ({len(july_plus)}件):")
for e in sorted(july_plus, key=lambda x: x.get("date", "")):
    print(f"  {e['date'][:10]} {e['currency']:3s} [{e['importance']:8s}] {e['name']}")
