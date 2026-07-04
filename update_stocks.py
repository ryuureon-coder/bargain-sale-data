# -*- coding: utf-8 -*-
"""
投資分析アプリ「バーゲンセール」データ更新スクリプト
- tickers.json の銘柄リストを読み込み
- yfinance で株価・財務データを取得
- 要件定義書のデータ構造に沿った stocks.json / market.json を出力

※このスクリプトは GitHub Actions 上で1日1回(平日の閉場後)自動実行されます。
"""

import json
import time
import statistics
from datetime import datetime, timezone, timedelta

import yfinance as yf

INPUT_FILE = "tickers.json"
STOCKS_OUTPUT = "stocks.json"
MARKET_OUTPUT = "market.json"

JST = timezone(timedelta(hours=9))


def safe_round(value, digits=1):
    """None安全なround"""
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def get_debt_ratio(ticker):
    """負債比率 = 有利子負債合計 ÷ 総資産(取得できない場合は None)"""
    try:
        bs = ticker.balance_sheet
        if bs is None or bs.empty:
            return None
        latest = bs.iloc[:, 0]  # 最新期
        total_debt = latest.get("Total Debt")
        total_assets = latest.get("Total Assets")
        if total_assets in (None, 0) or total_debt is None:
            return None
        ratio = float(total_debt) / float(total_assets)
        if ratio < 0:
            return None
        return round(ratio, 3)
    except Exception:
        return None


def get_profit_status(ticker):
    """直近通期の純利益で黒字/赤字を判定"""
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
    """直近2期の売上高を比較して増収/減収を判定"""
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


def fetch_stock(entry):
    """1銘柄分のデータを取得"""
    code = entry["code"]
    symbol = f"{code}.T"  # 東証銘柄は .T を付ける
    ticker = yf.Ticker(symbol)

    info = {}
    try:
        info = ticker.info or {}
    except Exception:
        pass

    # 株価(当日の始値・終値)
    price = None
    open_price = None
    close_price = None
    try:
        hist = ticker.history(period="5d")
        if hist is not None and not hist.empty:
            last_row = hist.iloc[-1]
            open_price = safe_round(last_row.get("Open"), 1)
            close_price = safe_round(last_row.get("Close"), 1)
            price = close_price
    except Exception:
        pass

    per = safe_round(info.get("trailingPE"), 2)

    return {
        "code": code,
        "name": entry["name"],
        "market": entry.get("market", "prime"),
        "price": price,
        "open_price": open_price,
        "close_price": close_price,
        "per": per,
        "debt_ratio": get_debt_ratio(ticker),
        "profit_status": get_profit_status(ticker),
        "revenue_growth": get_revenue_growth(ticker),
    }


def main():
    with open(INPUT_FILE, encoding="utf-8") as f:
        tickers = json.load(f)["tickers"]

    stocks = []
    errors = []

    for i, entry in enumerate(tickers, start=1):
        try:
            stock = fetch_stock(entry)
            stocks.append(stock)
            print(f"[{i}/{len(tickers)}] OK: {entry['code']} {entry['name']}")
        except Exception as e:
            errors.append({"code": entry["code"], "error": str(e)})
            print(f"[{i}/{len(tickers)}] NG: {entry['code']} {e}")
        time.sleep(1.5)  # アクセス集中を避けるためのマナー待機

    # 市場PER = 取得できた銘柄のPERの中央値(市場全体の目安として使用)
    valid_pers = [s["per"] for s in stocks if s["per"] is not None and 0 < s["per"] < 200]
    market_per = round(statistics.median(valid_pers), 2) if valid_pers else None

    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")

    with open(STOCKS_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(
            {"updated_at": now, "stocks": stocks, "errors": errors},
            f, ensure_ascii=False, indent=2,
        )

    with open(MARKET_OUTPUT, "w", encoding="utf-8") as f:
        json.dump(
            {
                "updated_at": now,
                "market_per": market_per,
                # 日経平均・TOPIXの公式PERは無料APIでは取得できないため、
                # MVPでは対象銘柄群の中央値PERを市場比較の基準として使用する
                "nikkei_per": None,
                "topix_per": None,
            },
            f, ensure_ascii=False, indent=2,
        )

    print(f"完了: {len(stocks)}銘柄 / エラー{len(errors)}件 / 市場PER(中央値): {market_per}")


if __name__ == "__main__":
    main()
