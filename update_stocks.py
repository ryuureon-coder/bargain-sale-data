# -*- coding: utf-8 -*-
"""
投資分析アプリ「バーゲンセール」データ更新スクリプト v3.6.1
v3からの変更点:
  ・配当利回りを追加(年間配当額÷株価で自前計算し、表記ゆれを回避)
  ・株主優待フラグ(tickers.jsonの "yutai" キーをそのまま転記。手動管理)
v3.1からの変更点:
  ・業種フラグ(tickers.jsonの "sector" キーを転記。金融業の注記表示に使用)
v3.2からの変更点:
  ・配当履歴(2019年以降の暦年合算)と直近配当を追加
v3.3からの変更点:
  ・時価総額(market_cap)を追加。アプリ側の無料枠判定(時価総額TOP50)に使用
  ・対象を「日本大型銘柄225」(225銘柄)に拡大
v3.4からの変更点(v3.5):
  ・対象を「日本代表300銘柄」に拡大/指数取得を廃止し独自の市場平均
    (対象銘柄の等加重平均・初週=100)を universe_avg として出力
v3.5からの変更点(v3.6):
  ・株式分割によるチャート断層を解消。週次終値を splits で遡及調整。
    配当は非調整(実額のまま)を維持。
  ・split_history を出力(2019年以降・日付昇順)。配当カードの
    「N分割後」注記に使用。
v3.6からの変更点(v3.6.1):
  ・分割調整を「日付一律 ÷ratio」から「段差検知方式」に修正。
    yfinance の生 close は銘柄により分割調整済み/未調整が混在するため、
    日付一律だと調整済み銘柄を二重調整して壊れていた(全分割銘柄で発生)。
    split 日付付近で実測段差が ~ratio(相対±20%)のときだけ遡及調整する。
    (28分割銘柄+回帰銘柄を実データ検証済み。しまむら8227の断層解消を確認)
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


def get_dividend_yield(info, price):
    """配当利回り(%) = 年間配当額 ÷ 株価 × 100
    yfinanceのdividendYieldはバージョンにより%表記/小数表記が
    混在するため、配当額から自前計算して表記ゆれを回避する"""
    try:
        rate = info.get("dividendRate")
        if rate is None:
            rate = info.get("trailingAnnualDividendRate")
        if rate is None or price in (None, 0):
            return None
        r = float(rate)
        if r != r or r < 0:  # NaN/負値を除外
            return None
        return round(r / float(price) * 100, 2)
    except Exception:
        return None


def get_dividend_history(ticker):
    """2019年以降の年間配当(暦年合算)と直近1回の配当を返す
    戻り値: (history, latest)
      history: [{"year": 2019, "total": 44.0}, ...] 年昇順
      latest:  {"date": "YYYY-MM-DD", "amount": 22.0} or None
    """
    try:
        div = ticker.dividends
        if div is None or len(div) == 0:
            return ([], None)
        yearly = {}
        latest = None
        for date, amount in div.items():
            try:
                a = float(amount)
            except (TypeError, ValueError):
                continue
            if a != a or a < 0:  # NaN/負値を除外
                continue
            y = int(date.year)
            if y >= 2019:
                yearly[y] = yearly.get(y, 0.0) + a
            latest = {"date": date.strftime("%Y-%m-%d"),
                      "amount": round(a, 2)}
        history = [{"year": y, "total": round(t, 2)}
                   for y, t in sorted(yearly.items())]
        return (history, latest)
    except Exception:
        return ([], None)


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


def _adjust_weekly_splits(series, splits):
    # 週次終値を分割調整する(段差検知方式)。
    # yfinance(=Yahoo)の生 close は、銘柄によって「分割調整済み」で返る場合と
    # 「未調整」で返る場合が混在する(直近の分割ほど未調整で返りやすい)。
    # そのため日付で一律に ÷ratio すると、調整済みの銘柄を二重調整して壊す。
    # → split 日付付近で「実測の段差が ~ratio か」を検知し、未調整のときだけ
    #   その段差より前の週を ÷ratio して現在株数基準に揃える。調整済みは不触。
    # series: [{"date","close"}] 昇順(生値) / splits: [(date_str, ratio), ...]
    if not series or not splits:
        return series
    closes = [p["close"] for p in series]
    dates = [p["date"] for p in series]
    n = len(closes)
    first_date = dates[0]
    # 新しい分割から順に処理(複数分割の合成に対応)
    in_win = sorted(
        [(d, r) for (d, r) in splits if r and r > 0 and d >= first_date],
        key=lambda x: x[0], reverse=True,
    )
    for d, r in in_win:
        # ex-date 以降の最初の週(おおよその境界位置)
        b0 = next((i for i, dt in enumerate(dates) if dt >= d), n)
        # 窓 ±3 bars 内で obs が r に最も近い段差を argmin で選ぶ(first-match でなく)。
        # 相対±20%以内のときだけ「未調整」と判定。r=2 の通常下落(obs~1.22)は
        # 相対0.39 で弾かれる。逆分割(r<1)も expected obs=r なので同式で整合。
        # ※r が 1 に極めて近い分割(例 r=1.3)は通常変動と区別しづらく誤検知余地あり
        #   (現ユニバースの r<1.8 分割は全て 1 年窓外のため実害なし)。
        lo = max(1, b0 - 3)
        hi = min(n - 1, b0 + 3)
        boundary = -1
        best_rel = float("inf")
        for i in range(lo, hi + 1):
            if closes[i] and closes[i - 1]:
                obs = closes[i - 1] / closes[i]
                rel = abs(obs - r) / r
                if rel < best_rel:
                    best_rel = rel
                    boundary = i
        if best_rel > 0.20:
            boundary = -1
        if boundary == -1:
            continue  # 段差なし=既に調整済み→触らない
        for i in range(boundary):
            closes[i] = closes[i] / r
    return [{"date": dates[i], "close": closes[i]} for i in range(n)]


def get_weekly_history(ticker_obj):
    # 週次終値を分割調整して返す(Case B: 手動遡及調整)。
    # auto_adjust=False で生の終値を取得し、_adjust_weekly_splits で
    # 段差検知して現在株数基準に揃える。配当は非調整(実額のまま)。
    try:
        hist = ticker_obj.history(period="1y", interval="1wk", auto_adjust=False)
        if hist is None or hist.empty:
            return []
        raw = []
        for date, row in hist.iterrows():
            close = row.get("Close")
            if close is None or str(close) == "nan":
                continue
            raw.append({
                "date": date.strftime("%Y-%m-%d"),
                "close": float(close),
            })
        # 分割イベント(日付文字列, 比率)。併合(逆分割・r<1)もそのまま渡す。
        splits = []
        try:
            sp = ticker_obj.splits
            if sp is not None and len(sp) > 0:
                for sdate, ratio in sp.items():
                    try:
                        r = float(ratio)
                    except (TypeError, ValueError):
                        continue
                    splits.append((sdate.strftime("%Y-%m-%d"), r))
        except Exception:
            splits = []
        adjusted = _adjust_weekly_splits(raw, splits)
        return [
            {"date": p["date"], "close": round(p["close"], 1)}
            for p in adjusted
        ]
    except Exception:
        return []


def get_split_history(ticker_obj, since_year=2019):
    # 株式分割の履歴(2019年以降・日付昇順)を返す。
    # 形式: [{"date": "2026-02-21", "ratio": 3.0}, ...]
    # 用途: アプリ配当カードの「N分割後」注記。
    try:
        splits = ticker_obj.splits
        if splits is None or len(splits) == 0:
            return []
        result = []
        for sdate, ratio in splits.items():
            try:
                r = float(ratio)
            except (TypeError, ValueError):
                continue
            if r <= 0 or sdate.year < since_year:
                continue
            result.append({
                "date": sdate.strftime("%Y-%m-%d"),
                "ratio": round(r, 4),
            })
        result.sort(key=lambda x: x["date"])
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
    div_history, latest_div = get_dividend_history(ticker)
    black_years, total_years = get_profit_stability(ticker)

    stock = {
        "code": code,
        "name": entry["name"],
        "market": entry.get("market", "prime"),
        "sector": entry.get("sector"),
        "price": price,
        "open_price": open_price,
        "close_price": close_price,
        "per": safe_round(info.get("trailingPE"), 2),
        "market_cap": safe_round(info.get("marketCap"), 0),
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
        "dividend_yield": get_dividend_yield(info, price),
        "dividend_history": div_history,
        "latest_dividend": latest_div,
        # 株主優待: 無料APIでは取得不可のため手動管理(tickers.jsonに
        # "yutai": "あり" 等を書いた銘柄のみ値が入る。未記入はnull)
        "shareholder_benefit": entry.get("yutai"),
        # 株式分割履歴(2019年以降・昇順)。配当カードの「N分割後」注記用。
        "split_history": get_split_history(ticker),
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

    # 市場平均(独自指標): 対象銘柄の週次騰落率の単純平均(等加重、初週=100)
    # ※期中上場の銘柄は上場週からの騰落率で参加する
    ratio_by_date = {}
    for code, series in histories.items():
        if len(series) < 2:
            continue
        base = series[0]["close"]
        if not base:
            continue
        for p in series:
            ratio_by_date.setdefault(p["date"], []).append(p["close"] / base)
    universe_avg = [
        {"date": d, "close": round(sum(v) / len(v) * 100, 2)}
        for d, v in sorted(ratio_by_date.items())
    ]
    # 市場平均(時価総額加重・参考出力): 現在の時価総額ウェイトを過去にも
    # 固定適用した近似値。アプリ表示には未使用(将来の切替用の保険)
    cap_by_code = {s["code"]: s["market_cap"] for s in stocks
                   if s.get("market_cap")}
    wsum_by_date = {}
    cap_by_date = {}
    for code, series in histories.items():
        cap = cap_by_code.get(code)
        if not cap or len(series) < 2:
            continue
        base = series[0]["close"]
        if not base:
            continue
        for p in series:
            d = p["date"]
            wsum_by_date[d] = wsum_by_date.get(d, 0.0) + p["close"] / base * cap
            cap_by_date[d] = cap_by_date.get(d, 0.0) + cap
    universe_cap = [
        {"date": d, "close": round(wsum_by_date[d] / cap_by_date[d] * 100, 2)}
        for d in sorted(wsum_by_date) if cap_by_date[d] > 0
    ]

    index_histories = {
        "universe_avg": universe_avg,            # 等加重(アプリ表示用)
        "universe_cap_weighted": universe_cap,   # 時価総額加重(参考)
    }
    print(f"市場平均OK: 等加重{len(universe_avg)}点 / 加重{len(universe_cap)}点")

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
