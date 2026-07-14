# -*- coding: utf-8 -*-
"""
投資分析アプリ「バーゲンセール」データ更新スクリプト v3.7
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
v3.6.1からの変更点(v3.7):
  ・現在価格 price を get_current_price に分離。従来の
    ticker.history(period="5d") が run毎に空を返し price が虫食い/全滅
    していた不具合を修正。主=fast_info.last_price、副=日次履歴の最終終値
    の2段取得(前日終値は price に混ぜない)。price 依存の dividend_yield も
    連鎖回復。取得内訳を [price] 行でログ出力(silent null をやめる)。
    (全300銘柄を chart API で実測: regularMarketPrice 充足 299/300)
v3.7からの変更点(v3.8):
  ・再発防止 Phase 1(L1受け入れ検査ブレーカ)。main() を
    「生成→検証→合格時のみアトミック書込」に再構成。
    検証は validate_stocks.py(ok/warn/fail の3層)。fail 時はファイルを
    一切上書きせず exit(1) → update.yml の notify ジョブが Issue 起票。
    前回コミット済みの正本が生き残る(凍結の可視化はアプリ側の
    鮮度バナー N1 が担う)。書込は temp→os.replace で torn write を防止。
    背景: price全滅事故(2026-07-06〜09)。docs/recurrence-prevention.md 参照。
"""

import json
import sys
import time
import statistics
from datetime import datetime, timezone, timedelta

import yfinance as yf

import validate_stocks

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


def get_current_price(ticker_obj):
    """現在価格を返す (price, open_price, close_price, source)。

    旧実装の ticker.history(period="5d") は run毎に空DataFrameを返す不具合が
    あり price が虫食い/全滅していた。同じ chart エンドポイントでも
    fast_info(=軽量・最新約定値)と日次履歴は安定して取れる実測結果に基づき、
      主: fast_info.last_price(最も新鮮・場中は当日値、場後は当日終値)
      副: 日次履歴 period="1mo" の最終有効終値(同じ chart 経路・堅い)
    の2段で取得する。前日終値(previous_close)は price には混ぜない
    (「昨日の値が現在値に化ける」事故防止)。close_price 欄にのみ入れる。
    どちらも取れなければ price=None(銘柄自体は落とさない)。
    """
    price = open_price = prev_close = None
    source = "none"

    # 主: fast_info(chartエンドポイント・認証系を通らず throttle に強い)
    try:
        fi = ticker_obj.fast_info
        lp = fi.last_price
        if lp is not None and float(lp) == float(lp) and float(lp) > 0:
            price = float(lp)
            source = "fast_info"
            try:
                op = fi.open
                if op is not None and float(op) == float(op):
                    open_price = float(op)
            except Exception:
                pass
            try:
                pc = fi.previous_close
                if pc is not None and float(pc) == float(pc):
                    prev_close = float(pc)
            except Exception:
                pass
    except Exception:
        pass

    # 副: 日次履歴の最終有効終値(auto_adjust=False=実額)
    if price is None:
        try:
            h = ticker_obj.history(period="1mo", interval="1d",
                                   auto_adjust=False)
            if h is not None and not h.empty and "Close" in h:
                h = h.dropna(subset=["Close"])
                if len(h) > 0:
                    last = h.iloc[-1]  # price/open は同一行から取る(日付ズレ防止)
                    c = float(last["Close"])
                    # 0以下は取引停止・データ源異常とみなし不採用(主経路と同じ
                    # 正値チェック)。0.0 が price として配信されると null と違い
                    # 受け入れ検査もアプリの非表示分岐も素通りするため(監査指摘)。
                    if c == c and c > 0:
                        price = c
                        source = "daily_hist"
                        op = last.get("Open")
                        if op is not None and float(op) == float(op):
                            open_price = float(op)
                        if len(h) > 1:
                            prev_close = float(h["Close"].iloc[-2])
        except Exception:
            pass

    return (
        safe_round(price, 1),
        safe_round(open_price, 1),
        safe_round(prev_close, 1),
        source,
    )


def fetch_stock(entry):
    code = entry["code"]
    symbol = f"{code}.T"
    ticker = yf.Ticker(symbol)

    info = {}
    try:
        info = ticker.info or {}
    except Exception:
        pass

    # 現在価格(price は当日/最新約定値のみ。前日終値は close_price 欄止まり)
    price, open_price, close_price, price_source = get_current_price(ticker)

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
    return stock, weekly, price_source


def main():
    with open(INPUT_FILE, encoding="utf-8") as f:
        tickers = json.load(f)["tickers"]

    stocks = []
    histories = {}
    errors = []
    price_sources = {"fast_info": 0, "daily_hist": 0, "none": 0}
    price_missing = []  # price を取得できなかった銘柄(銘柄自体は残す)

    for i, entry in enumerate(tickers, start=1):
        try:
            stock, weekly, price_source = fetch_stock(entry)
            stocks.append(stock)
            histories[entry["code"]] = weekly
            price_sources[price_source] = price_sources.get(price_source, 0) + 1
            if stock["price"] is None:
                price_missing.append(entry["code"])
            print(f"[{i}/{len(tickers)}] OK: {entry['code']} {entry['name']} "
                  f"price={stock['price']}({price_source})")
        except Exception as e:
            errors.append({"code": entry["code"], "error": str(e)})
            print(f"[{i}/{len(tickers)}] NG: {entry['code']} {e}")
        time.sleep(1.5)

    # 価格取得の内訳を可視化(silent null を残さない)
    print(f"[price] fast_info={price_sources['fast_info']} "
          f"daily_hist={price_sources['daily_hist']} "
          f"none={price_sources['none']} / missing={len(price_missing)} "
          f"{price_missing}")

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

    stocks_doc = {"updated_at": now, "stocks": stocks, "errors": errors}
    market_doc = {
        "updated_at": now,
        "market_per": market_per,
        # 公式の日経平均PER/TOPIX PERは無料データ源では取得不可のため
        # 本アプリでは市場PER(対象銘柄の中央値)を基準値として使用する
        "nikkei_per": None,
        "topix_per": None,
    }
    history_doc = {
        "updated_at": now,
        "interval": "1wk",
        "period": "1y",
        "indexes": index_histories,
        "stocks": histories,
    }

    # ---- L1/L2/N4: 配信前の受け入れ検査(再発防止 Phase 1) ----
    # 前日比較のベースラインは「コミット済みの現行 stocks.json」。
    # 初回や破損時は None → validate 側が warn を記録して回帰をスキップする。
    prev_doc = None
    try:
        with open(STOCKS_OUTPUT, encoding="utf-8") as f:
            prev_doc = json.load(f)
    except (OSError, ValueError):
        prev_doc = None

    try:
        report = validate_stocks.validate(
            stocks_doc, market_doc, history_doc, tickers, prev_doc)
    except Exception as e:  # 番人自身が死んでも fail-closed(配信停止)にする
        # 未捕捉例外で落ちると「どのゲートが・何%で」の情報が通知に載らないため、
        # 例外内容を含む fail レポートを組み立ててから exit(1) する(監査指摘)。
        report = {
            "level": validate_stocks.FAIL,
            "findings": [{"level": validate_stocks.FAIL,
                          "check": "validator_crash",
                          "message": f"validator が例外で停止: {e!r}"}],
            "stats": {},
            "quarantine": [],
        }
    validate_stocks.write_report_files(report)
    print(validate_stocks.render_report_md(report))

    # サーキットブレーカ。fail なら1バイトも書かずに異常終了し、前回の正本を
    # 生き残らせる → update.yml の notify ジョブが Issue を起票する。
    # ブレーカは"承認者"ではない——沈黙を破って人間の観測を呼ぶ装置(設計書P1)。
    # 書込みは必ずこの publish() を通す(直接 open(...,"w") を足すと事故前の
    # 「無条件write」に先祖返りする)。
    published = validate_stocks.publish(report, [
        (STOCKS_OUTPUT, stocks_doc),
        (MARKET_OUTPUT, market_doc),
        (HISTORY_OUTPUT, history_doc),
    ])
    if not published:
        print("検証FAIL: 配信を停止しました(ファイルは上書きしていません)")
        sys.exit(1)

    print(f"完了({report['level']}): {len(stocks)}銘柄 / エラー{len(errors)}件"
          f" / 市場PER: {market_per}")


if __name__ == "__main__":
    main()
