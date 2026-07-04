# -*- coding: utf-8 -*-
"""
投資分析アプリ「バーゲンセール」データ更新スクリプト v2
変更点:
  1. 無借金企業の判定を修正(貸借対照表にTotal Debt行が無い = 負債ゼロと判定)
  2. チャート用に1年分の株価履歴(週次)を history.json として出力
     - 各銘柄 + 日経平均(^N225) + TOPIX連動ETF(1306.T ※TOPIXの代替)
  3. 理由文(reason)を実データから動的に生成
"""

import json
import time
import statistics
from datetime import datetime, timezone, timedelta

import yfinance as yf

INPUT_FILE = "tickers.json"
STOCKS_OUTPUT = "stocks.json"
MARKET_OUTPUT = "market.json"
HISTORY_OUTPUT = "history.json"

JST = timezone(timedelta(hours=9))

# 実質無借金とみなす負債比率の閾値(1%未満)
DEBT_FREE_THRESHOLD = 0.01


def safe_round(value, digits=1):
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def get_debt_ratio(ticker):
    """負債比率 = 有利子負債合計 ÷ 総資産
    v2: 貸借対照表は取得できたが Total Debt 行が存在しない場合は
        「有利子負債ゼロ(無借金)」とみなして 0.0 を返す
    """
    try:
        bs = ticker.balance_sheet
        if bs is None or bs.empty:
            return None
        latest = bs.iloc[:, 0]
        total_assets = latest.get("Total Assets")
        if total_assets is None or float(total_assets) == 0:
            return None
        if "Total Debt" not in bs.index:
            return 0.0  # ← v2修正: 無借金企業
        total_debt = latest.get("Total Debt")
        if total_debt is None or str(total_debt) == "nan":
            return 0.0  # 値が空欄の場合も無借金扱い
        ratio = float(total_debt) / float(total_assets)
        if ratio < 0:
            return None
        return round(ratio, 3)
    except Exception:
        return None


def get_profit_status(ticker):
    try:
        fin = ticker.income_stmt
        if fin is None or fin.empty:
            return None
        net_income = fin.iloc[:, 0].get("Net Income")
        if net_income is None:
            return None
        return "black" if float(net_income) > 0 else "red"
    except Exception:
        return None


def get_revenue_growth(ticker):
    try:
        fin = ticker.income_stmt
        if fin is None or fin.empty or fin.shape[1] < 2:
            return None
        latest = fin.iloc[:, 0].get("Total Revenue")
        prev = fin.iloc[:, 1].get("Total Revenue")
        if latest is None or prev is None or float(prev) == 0:
            return None
        return "increase" if float(latest) > float(prev) else "decrease"
    except Exception:
        return None


def build_reason(per, market_per, debt_ratio, profit_status):
    """v2: 実データから理由文を動的に組み立てる(要件書■5対応)"""
    parts = []
    if per is not None and market_per is not None:
        if per < market_per * 0.7:
            parts.append("低PER")
        elif per < market_per:
            parts.append("市場平均よりやや低PER")
    if debt_ratio is not None:
        if debt_ratio < DEBT_FREE_THRESHOLD:
            parts.append("無借金")
        elif debt_ratio <= 0.3:
            parts.append("財務健全")
    if profit_status == "black":
        parts.append("黒字")
    return "＋".join(parts) if parts else "データ不足"


def get_weekly_history(ticker_obj):
    """1年分の週次終値履歴を返す(チャート用・約52点)"""
    try:
        hist = ticker_obj.history(period="1y", interval="1wk")
        if hist is None or hist.empty:
            return []
        result = []
        for date, row in hist.iterrows():
            close = row.get("Close")
            if close is None or str(close) == "nan":
                continue
            result.append({
                "date": date.strftime("%Y-%m-%d"),
                "close": round(float(close), 1),
            })
        return result
    except Exception:
        return []


def fetch_stock(entry):
    code = entry["code"]
    symbol = f"{code}.T"
    ticker = yf.Ticker(symbol)

    info = {}
    try:
        info = ticker.info or {}
    except Exception:
        pass

    price = open_price = close_price = None
    try:
        hist = ticker.history(period="5d")
        if hist is not None and not hist.empty:
            last_row = hist.iloc[-1]
            open_price = safe_round(last_row.get("Open"), 1)
            close_price = safe_round(last_row.get("Close"), 1)
            price = close_price
    except Exception:
        pass

    stock = {
        "code": code,
        "name": entry["name"],
        "market": entry.get("market", "prime"),
        "price": price,
        "open_price": open_price,
        "close_price": close_price,
        "per": safe_round(info.get("trailingPE"), 2),
        "debt_ratio": get_debt_ratio(ticker),
        "profit_status": get_profit_status(ticker),
        "revenue_growth": get_revenue_growth(ticker),
    }
    weekly = get_weekly_history(ticker)
    return stock, weekly


def main():
    with open(INPUT_FILE, encoding="utf-8") as f:
        tickers = json.load(f)["tickers"]

    stocks = []
    histories = {}
    errors = []

    for i, entry in enumerate(tickers, start=1):
        try:
            stock, weekly = fetch_stock(entry)
            stocks.append(stock)
            histories[entry["code"]] = weekly
            print(f"[{i}/{len(tickers)}] OK: {entry['code']} {entry['name']} (履歴{len(weekly)}点)")
        except Exception as e:
            errors.append({"code": entry["code"], "error": str(e)})
            print(f"[{i}/{len(tickers)}] NG: {entry['code']} {e}")
        time.sleep(1.5)

    # 指数の履歴(チャート比較用)
    # 日経平均: ^N225 / TOPIX: 直接取得できないためTOPIX連動ETF(1306.T)を代替使用
    index_histories = {}
    for key, symbol in [("nikkei", "^N225"), ("topix_etf", "1306.T")]:
        try:
            index_histories[key] = get_weekly_history(yf.Ticker(symbol))
            print(f"指数OK: {key} ({len(index_histories[key])}点)")
        except Exception as e:
            index_histories[key] = []
            print(f"指数NG: {key} {e}")
        time.sleep(1.5)

    # 市場PER = 中央値
    valid_pers = [s["per"] for s in stocks if s["per"] is not None and 0 < s["per"] < 200]
    market_per = round(statistics.median(valid_pers), 2) if valid_pers else None

    # v2: 理由文を動的生成して各銘柄に付与
    for s in stocks:
        s["reason"] = build_reason(s["per"], market_per, s["debt_ratio"], s["profit_status"])

    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")

    with open(STOCKS_OUTPUT, "w", encoding="utf-8") as f:
        json.dump({"updated_at": now, "stocks": stocks, "errors": errors},
                  f, ensure_ascii=False, indent=2)

    with open(MARKET_OUTPUT, "w", encoding="utf-8") as f:
        json.dump({
            "updated_at": now,
            "market_per": market_per,
            "nikkei_per": None,
            "topix_per": None,
        }, f, ensure_ascii=False, indent=2)

    with open(HISTORY_OUTPUT, "w", encoding="utf-8") as f:
        json.dump({
            "updated_at": now,
            "interval": "1wk",
            "period": "1y",
            "indexes": index_histories,
            "stocks": histories,
        }, f, ensure_ascii=False, indent=2)

    print(f"完了: {len(stocks)}銘柄 / エラー{len(errors)}件 / 市場PER: {market_per}")


if __name__ == "__main__":
    main()
