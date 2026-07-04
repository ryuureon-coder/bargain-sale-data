# -*- coding: utf-8 -*-
"""
投資分析アプリ「バーゲンセール」データ更新スクリプト v3
v2からの変更点:
  詳細指標を追加取得(PBR / EPS / ROE / ROA / PSR / PEGレシオ /
  自己資本比率 / 営業CF / フリーCF / 利益の安定性)
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

DEBT_FREE_THRESHOLD = 0.01


def safe_round(value, digits=1):
    if value is None:
        return None
    try:
        v = float(value)
        if v != v:  # NaN
            return None
        return round(v, digits)
    except (TypeError, ValueError):
        return None


def get_debt_ratio(ticker):
    """負債比率 = 有利子負債合計 ÷ 総資産(Total Debt行なし=無借金0.0)"""
    try:
        bs = ticker.balance_sheet
        if bs is None or bs.empty:
            return None
        latest = bs.iloc[:, 0]
        total_assets = latest.get("Total Assets")
        if total_assets is None or float(total_assets) == 0:
            return None
        if "Total Debt" not in bs.index:
            return 0.0
        total_debt = latest.get("Total Debt")
        if total_debt is None or str(total_debt) == "nan":
            return 0.0
        ratio = float(total_debt) / float(total_assets)
        if ratio < 0:
            return None
        return round(ratio, 3)
    except Exception:
        return None


def get_equity_ratio(ticker):
    """自己資本比率 = 自己資本 ÷ 総資産"""
    try:
        bs = ticker.balance_sheet
        if bs is None or bs.empty:
            return None
        latest = bs.iloc[:, 0]
        total_assets = latest.get("Total Assets")
        equity = latest.get("Stockholders Equity")
        if equity is None or str(equity) == "nan":
            equity = latest.get("Total Equity Gross Minority Interest")
        if total_assets in (None, 0) or equity is None or str(equity) == "nan":
            return None
        return round(float(equity) / float(total_assets), 3)
    except Exception:
        return None


def get_cashflows(ticker):
    """(営業CF, フリーCF) を円単位で返す"""
    try:
        cf = ticker.cashflow
        if cf is None or cf.empty:
            return (None, None)
        latest = cf.iloc[:, 0]

        def pick(key):
            v = latest.get(key)
            if v is None or str(v) == "nan":
                return None
            return float(v)

        return (pick("Operating Cash Flow"), pick("Free Cash Flow"))
    except Exception:
        return (None, None)


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


def get_profit_stability(ticker):
    """(黒字期数, 集計期数) 直近最大4期の黒字回数"""
    try:
        fin = ticker.income_stmt
        if fin is None or fin.empty:
            return (None, None)
        total = 0
        black = 0
        for col in range(fin.shape[1]):
            ni = fin.iloc[:, col].get("Net Income")
            if ni is None or str(ni) == "nan":
                continue
            total += 1
            if float(ni) > 0:
                black += 1
        if total == 0:
            return (None, None)
        return (black, total)
    except Exception:
        return (None, None)


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

    op_cf, free_cf = get_cashflows(ticker)
    black_years, total_years = get_profit_stability(ticker)

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
        # ---- v3 追加指標 ----
        "pbr": safe_round(info.get("priceToBook"), 2),
        "eps": safe_round(info.get("trailingEps"), 1),
        "roe": safe_round(info.get("returnOnEquity"), 4),
        "roa": safe_round(info.get("returnOnAssets"), 4),
        "psr": safe_round(info.get("priceToSalesTrailing12Months"), 2),
        "peg": safe_round(
            info.get("trailingPegRatio") or info.get("pegRatio"), 2),
        "equity_ratio": get_equity_ratio(ticker),
        "operating_cf": op_cf,
        "free_cf": free_cf,
        "profit_years_black": black_years,
        "profit_years_total": total_years,
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
            print(f"[{i}/{len(tickers)}] OK: {entry['code']} {entry['name']}")
        except Exception as e:
            errors.append({"code": entry["code"], "error": str(e)})
            print(f"[{i}/{len(tickers)}] NG: {entry['code']} {e}")
        time.sleep(1.5)

    index_histories = {}
    for key, symbol in [("nikkei", "^N225"), ("topix_etf", "1306.T")]:
        try:
            index_histories[key] = get_weekly_history(yf.Ticker(symbol))
            print(f"指数OK: {key} ({len(index_histories[key])}点)")
        except Exception as e:
            index_histories[key] = []
            print(f"指数NG: {key} {e}")
        time.sleep(1.5)

    valid_pers = [s["per"] for s in stocks
                  if s["per"] is not None and 0 < s["per"] < 200]
    market_per = round(statistics.median(valid_pers), 2) if valid_pers else None

    for s in stocks:
        s["reason"] = build_reason(
            s["per"], market_per, s["debt_ratio"], s["profit_status"])

    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")

    with open(STOCKS_OUTPUT, "w", encoding="utf-8") as f:
        json.dump({"updated_at": now, "stocks": stocks, "errors": errors},
                  f, ensure_ascii=False, indent=2)

    with open(MARKET_OUTPUT, "w", encoding="utf-8") as f:
        json.dump({
            "updated_at": now,
            "market_per": market_per,
            # 公式の日経平均PER/TOPIX PERは無料データ源では取得不可のため
            # 本アプリでは市場PER(対象銘柄の中央値)を基準値として使用する
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
