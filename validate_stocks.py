# -*- coding: utf-8 -*-
"""受け入れ検査サーキットブレーカ v1.0（再発防止 Phase 1 / L1+L2+N4）

update_stocks.py が生成した3ドキュメント（stocks/market/history）を配信前に検査し、
ok / warn / fail の3層で判定する。fail は「配信停止」（呼び出し側が書込みをスキップ
して exit(1)）、warn は「配信するが通知」、ok は「静かに配信」。

設計の出典: docs/recurrence-prevention.md（v2）§5-6、docs/phase1-implementation-notes.md。
price全滅事故（2026-07-06〜09、fb52f6a で price null 300/300 なのに errors=[] のまま
3.3日間無検知）の再発防止が目的。

重要な設計判断（変更時は設計書も更新すること）:
  - ブレーカは"承認者"ではない。沈黙を破って人間の観測を呼び出す装置（設計書P1）。
  - 絶対フロアは「全滅級」だけを確実に捕らえる緩めの値。微妙な劣化は前日比warnで拾う。
    校正データは「正常1点＋全滅1点」の2スナップショットのみ（過剰適合の自認）。
    数週間の観測分散が溜まったら引き直す（設計書§8）。
  - 前日比の fail 権限は price / market_cap の2フィールドに限定。PER等まで広げると
    決算集中期の欠損増など正当な事象で全配信が止まる（SREレビュー指摘の反映）。
    ※健全なベースラインからの +30ppt は 90% フロアにも同時に引っかかるため冗長だが、
    フロア閾値を将来緩めた場合の防御層として意図的に残す。
  - N4 値サニティは全て warn 止まり（fail 権限なし）。誤検知コスト＞見逃しコスト。
    present-but-wrong の本丸は L6 人間観測（週次点検）が担う。

このモジュールは stdlib のみに依存する（yfinance 不要）。ゴールデンテストは
tests/test_validate_stocks.py（N3・番人の番人）。
"""

import json
import os

# ---- 判定レベル ----
OK = "ok"
WARN = "warn"
FAIL = "fail"

_LEVEL_ORDER = {OK: 0, WARN: 1, FAIL: 2}

# ---- 閾値（設計書§5の提案値。§8の再校正で引き直す）----
PRICE_COVERAGE_MIN = 0.90        # price 充足率フロア（全滅 fb52f6a=0% を確実に fail）
MARKET_CAP_COVERAGE_MIN = 0.90   # market_cap 充足率フロア
VALID_PER_MIN = 200              # 有効PER件数（0<per<200）。全滅時も283件生存した
                                 # ＝PERゲート単独では見逃す（設計書§5）ため price と併用
MARKET_PER_RANGE = (5.0, 60.0)   # N4: 市場PER中央値の常識帯（外れたら warn）
HISTORY_COVERAGE_MIN = 0.90      # 週次履歴が非空の銘柄数 ≥ ticker数×90%
UNIVERSE_AVG_MIN_POINTS = 10     # universe_avg の点数フロア
UNIVERSE_AVG_LAST_RANGE = (30.0, 500.0)  # N4: 初週=100基準の1年窓で±数倍は起きない
STOCKS_COUNT_MIN_RATIO = 0.90    # スキーマ: stocks 件数 ≥ ticker数×90%

DELTA_WARN_PPT = 10.0            # L2: null率が前日比 +10ppt 超で warn
DELTA_FAIL_PPT = 30.0            # L2: +30ppt 超で fail（対象は DELTA_FAIL_FIELDS のみ）
DELTA_FAIL_FIELDS = ("price", "market_cap")
DELTA_WATCH_FIELDS = ("price", "market_cap", "per", "pbr", "dividend_yield", "roe")

PRICE_JUMP_RATIO = 0.40          # N4: 前日比 ±40% 超の価格変動を「跳び」とみなす
PRICE_JUMP_SHARE_MAX = 0.05      # N4: 跳び銘柄が全体の 5% 超なら warn
                                 # （個別の跳びは分割・ストップ高安があり得るため黙認）

REPORT_MD = "validation_report.md"
REPORT_JSON = "validation_report.json"


def _finding(level, check, message):
    return {"level": level, "check": check, "message": message}


def _num(value):
    """数値なら float を返す。NaN/±inf/bool/型不正は None（＝無効値扱い）"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v != v or v == float("inf") or v == float("-inf"):
            return None
        return v
    return None


# price/market_cap は「0以下も取得失敗」とみなす。0.0 は null と違って
# アプリの非表示分岐も素通りし、全滅が0円表示として配信され得る（監査指摘）。
POSITIVE_FIELDS = ("price", "market_cap")


# 数値であるべきフィールド。非数値(文字列 "N/A" 等)が来たら「取得失敗」とみなす。
# ここに無いフィールド(shareholder_benefit 等の文字列系)は None かどうかだけを見る。
NUMERIC_FIELDS = ("price", "market_cap", "per", "pbr", "dividend_yield", "roe",
                  "eps", "roa", "psr", "peg", "debt_ratio", "equity_ratio")


def _is_valid(field, value):
    """field の値が「取得できている」とみなせるか。

    - POSITIVE_FIELDS（price/market_cap）: 数値かつ正であること。0以下・NaNは失敗扱い。
    - NUMERIC_FIELDS: 数値であること（型不正・NaNは失敗扱い）。
    - それ以外: None でなければ有効。
    """
    if field in POSITIVE_FIELDS:
        v = _num(value)
        return v is not None and v > 0
    if field in NUMERIC_FIELDS:
        return _num(value) is not None
    return value is not None


def _coverage(stocks, field):
    """field が有効値の割合と、無効な銘柄コード一覧を返す"""
    if not stocks:
        return 0.0, []
    missing = [s.get("code") for s in stocks
               if not _is_valid(field, s.get(field))]
    return 1.0 - len(missing) / len(stocks), missing


# validate() の prev_stocks_doc 省略時の番兵。
# 「省略＝回帰検査の対象外として呼ばれた（テスト・アドホック検証）」と
# 「None/不正＝本番でベースラインを読めなかった（warnで記録すべき異常）」を区別する。
_UNSET = object()


def validate(stocks_doc, market_doc, history_doc, tickers,
             prev_stocks_doc=_UNSET):
    """生成された3ドキュメントを検査し report dict を返す。

    report = {
      "level": "ok"|"warn"|"fail",
      "findings": [{"level","check","message"}, ...],
      "stats": {...主要数値...},
      "quarantine": [price/market_cap欠落の銘柄コード, ...],
    }
    """
    findings = []
    stats = {}

    # tickers 自体の不正は以降の全検査の母数を壊すため最初に fail で打ち切る
    # （番人自身が len(None) 等でクラッシュする「番人が死ぬ」障害モードの防止）
    if not isinstance(tickers, list) or not tickers:
        findings.append(_finding(FAIL, "schema_tickers",
                                 "tickers が空/リストでない"))
        return _build_report(findings, stats, [])
    n_tickers = len(tickers)

    # ---- スキーマ検査（構造が壊れていたら以降の検査は無意味なので先に fail）----
    schema_ok = True
    for key in ("updated_at", "stocks", "errors"):
        if not isinstance(stocks_doc, dict) or key not in stocks_doc:
            findings.append(_finding(FAIL, "schema",
                                     f"stocks.json に必須キー '{key}' がない"))
            schema_ok = False
    if schema_ok and not isinstance(stocks_doc["stocks"], list):
        findings.append(_finding(FAIL, "schema", "stocks.json の 'stocks' が配列でない"))
        schema_ok = False
    if not isinstance(market_doc, dict) or "market_per" not in market_doc:
        findings.append(_finding(FAIL, "schema", "market.json に 'market_per' がない"))
        schema_ok = False
    if (not isinstance(history_doc, dict)
            or not isinstance(history_doc.get("stocks"), dict)
            or not isinstance(history_doc.get("indexes"), dict)
            or not isinstance(
                history_doc["indexes"].get("universe_avg")
                if isinstance(history_doc.get("indexes"), dict) else None,
                list)):
        findings.append(_finding(FAIL, "schema",
                                 "history.json に 'stocks'(dict)/"
                                 "'indexes.universe_avg'(list) がない"))
        schema_ok = False
    if not schema_ok:
        return _build_report(findings, stats, [])

    # 辞書でない要素が混入した stocks は、それ自体を fail としつつ
    # 残りの検査は正常要素だけで続行する（クラッシュさせない）
    stocks = stocks_doc["stocks"]
    non_dict = sum(1 for s in stocks if not isinstance(s, dict))
    if non_dict:
        findings.append(_finding(
            FAIL, "schema",
            f"stocks 配列に辞書でない要素が {non_dict} 件混入"))
        stocks = [s for s in stocks if isinstance(s, dict)]
    stats["stocks_count"] = len(stocks)
    stats["tickers_count"] = n_tickers

    if len(stocks) < n_tickers * STOCKS_COUNT_MIN_RATIO:
        findings.append(_finding(
            FAIL, "schema_count",
            f"stocks 件数 {len(stocks)} がticker数 {n_tickers} の90%未満"))

    # ---- L1 絶対フロア ----
    price_cov, price_missing = _coverage(stocks, "price")
    stats["price_coverage"] = round(price_cov * 100, 1)
    if price_cov < PRICE_COVERAGE_MIN:
        findings.append(_finding(
            FAIL, "price_coverage",
            f"price 充足率 {price_cov*100:.1f}% < フロア {PRICE_COVERAGE_MIN*100:.0f}%"
            f"（欠落 {len(price_missing)}/{len(stocks)}件）"))

    cap_cov, cap_missing = _coverage(stocks, "market_cap")
    stats["market_cap_coverage"] = round(cap_cov * 100, 1)
    if cap_cov < MARKET_CAP_COVERAGE_MIN:
        findings.append(_finding(
            FAIL, "market_cap_coverage",
            f"market_cap 充足率 {cap_cov*100:.1f}% < フロア"
            f" {MARKET_CAP_COVERAGE_MIN*100:.0f}%（欠落 {len(cap_missing)}件）"))

    valid_pers = [p for p in (_num(s.get("per")) for s in stocks)
                  if p is not None and 0 < p < 200]
    stats["valid_per_count"] = len(valid_pers)
    if len(valid_pers) < VALID_PER_MIN:
        findings.append(_finding(
            FAIL, "valid_per_count",
            f"有効PER件数 {len(valid_pers)} < フロア {VALID_PER_MIN}"))

    market_per = market_doc.get("market_per")
    stats["market_per"] = market_per if _num(market_per) is not None else None
    if market_per is None:
        findings.append(_finding(FAIL, "market_per", "market_per が None"))
    elif _num(market_per) is None:
        findings.append(_finding(
            FAIL, "market_per", f"market_per が数値でない: {market_per!r}"))
    elif not (MARKET_PER_RANGE[0] <= _num(market_per) <= MARKET_PER_RANGE[1]):
        findings.append(_finding(
            WARN, "sanity_market_per",
            f"market_per {market_per} が常識帯 {MARKET_PER_RANGE} の外"))

    hist_stocks = history_doc["stocks"]
    non_empty = sum(1 for v in hist_stocks.values() if v)
    stats["history_non_empty"] = non_empty
    if non_empty < n_tickers * HISTORY_COVERAGE_MIN:
        findings.append(_finding(
            FAIL, "history_coverage",
            f"週次履歴が非空の銘柄 {non_empty} < ticker数{n_tickers}×90%"))

    universe_avg = history_doc["indexes"]["universe_avg"]
    stats["universe_avg_points"] = len(universe_avg)
    if len(universe_avg) < UNIVERSE_AVG_MIN_POINTS:
        findings.append(_finding(
            FAIL, "universe_avg_points",
            f"universe_avg 点数 {len(universe_avg)} < フロア {UNIVERSE_AVG_MIN_POINTS}"))
    elif universe_avg:
        last_point = universe_avg[-1]
        last = _num(last_point.get("close")) if isinstance(
            last_point, dict) else None
        stats["universe_avg_last"] = last
        if last is None or not (UNIVERSE_AVG_LAST_RANGE[0] <= last
                                <= UNIVERSE_AVG_LAST_RANGE[1]):
            findings.append(_finding(
                WARN, "sanity_universe_avg",
                f"universe_avg 最終点 {last} が正気の帯域"
                f" {UNIVERSE_AVG_LAST_RANGE} の外"))

    # ---- L2 前日比回帰（フラッピング捕捉）----
    prev_stocks = None
    if isinstance(prev_stocks_doc, dict) and isinstance(
            prev_stocks_doc.get("stocks"), list):
        prev_stocks = [s for s in prev_stocks_doc["stocks"]
                       if isinstance(s, dict)] or None
    if prev_stocks_doc is _UNSET:
        pass  # 回帰対象外として呼ばれた（テスト・アドホック検証）
    elif prev_stocks is None:
        findings.append(_finding(
            WARN, "delta_baseline",
            "前日データが無い/読めないため前日比回帰をスキップ（初回 or 破損）"))
    else:
        new_codes = {s.get("code") for s in stocks}
        prev_codes = {s.get("code") for s in prev_stocks}
        universe_changed = new_codes != prev_codes
        if universe_changed:
            findings.append(_finding(
                WARN, "delta_universe_changed",
                f"ユニバース変化を検知（+{len(new_codes-prev_codes)}"
                f"/-{len(prev_codes-new_codes)}銘柄）→ 前日比failをwarnに緩和"))
        for field in DELTA_WATCH_FIELDS:
            if not stocks:
                break
            new_null = sum(1 for s in stocks
                           if not _is_valid(field, s.get(field)))
            prev_null = sum(1 for s in prev_stocks
                            if not _is_valid(field, s.get(field)))
            new_rate = new_null / len(stocks) * 100
            prev_rate = prev_null / len(prev_stocks) * 100
            delta_ppt = new_rate - prev_rate
            if delta_ppt > DELTA_WARN_PPT:
                level = WARN
                if (field in DELTA_FAIL_FIELDS and delta_ppt > DELTA_FAIL_PPT
                        and not universe_changed):
                    level = FAIL
                findings.append(_finding(
                    level, f"delta_null_{field}",
                    f"{field} の null/無効率 {prev_rate:.1f}%→{new_rate:.1f}%"
                    f"（+{delta_ppt:.1f}ppt）"))

        # ---- N4 値サニティ: price の前日比跳び（分割二重調整・ステールキャッシュ狙い）----
        prev_price = {s.get("code"): _num(s.get("price"))
                      for s in prev_stocks}
        jumped = []
        compared = 0
        for s in stocks:
            p_new = _num(s.get("price"))
            p_prev = prev_price.get(s.get("code"))
            if p_new is not None and p_new > 0 \
                    and p_prev is not None and p_prev > 0:
                compared += 1
                if abs(p_new / p_prev - 1) > PRICE_JUMP_RATIO:
                    jumped.append(s.get("code"))
        stats["price_jumped"] = len(jumped)
        if compared > 0 and len(jumped) / compared > PRICE_JUMP_SHARE_MAX:
            findings.append(_finding(
                WARN, "sanity_price_jump",
                f"前日比±{PRICE_JUMP_RATIO*100:.0f}%超の価格変動が"
                f" {len(jumped)}/{compared}銘柄（>5%）: {jumped[:20]}"))

    # ---- quarantine（Phase1はレポート列挙のみ。data_quality出力=L4はPhase2）----
    quarantine = sorted(set(price_missing) | set(cap_missing))

    return _build_report(findings, stats, quarantine)


def _build_report(findings, stats, quarantine):
    level = OK
    for f in findings:
        if _LEVEL_ORDER[f["level"]] > _LEVEL_ORDER[level]:
            level = f["level"]
    return {"level": level, "findings": findings,
            "stats": stats, "quarantine": quarantine}


def render_report_md(report):
    """Issue本文・CIサマリ用のMarkdownを返す（どのゲートが・何%で落ちたかを必ず含める）"""
    icon = {OK: "🟢", WARN: "🟡", FAIL: "🔴"}[report["level"]]
    lines = [f"## {icon} データ検証結果: **{report['level'].upper()}**", ""]
    if report["findings"]:
        lines.append("| レベル | 検査 | 内容 |")
        lines.append("|---|---|---|")
        for f in report["findings"]:
            fi = {OK: "🟢", WARN: "🟡", FAIL: "🔴"}[f["level"]]
            lines.append(f"| {fi} {f['level']} | `{f['check']}` | {f['message']} |")
    else:
        lines.append("指摘なし（全ゲート通過）")
    lines.append("")
    lines.append("### 主要数値")
    for k, v in report["stats"].items():
        lines.append(f"- {k}: {v}")
    if report["quarantine"]:
        lines.append("")
        lines.append(f"### quarantine（price/market_cap欠落・{len(report['quarantine'])}件）")
        lines.append(", ".join(report["quarantine"][:30])
                     + ("…" if len(report["quarantine"]) > 30 else ""))
    return "\n".join(lines)


def write_report_files(report):
    """CI が拾う検証レポートを書き出す（コミット対象外・.gitignore済み）"""
    with open(REPORT_JSON, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write(render_report_md(report) + "\n")


def write_json_atomic(path, obj):
    # torn write 防止: 同一ディレクトリの temp に書いて fsync → os.replace。
    # 3ファイル間の完全同時性までは保証しない（逐次swap）。クラッシュがミリ秒窓に
    # 当たった場合のみクロスファイル不整合が残り得るが、次回runで解消される。
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def publish(report, documents):
    """サーキットブレーカ本体。fail なら1バイトも書かず False を返す。

    documents: [(path, obj), ...] を「合格時のみ」アトミック書込する。
    この関数が「生成→検証→合格時のみ書込」ゲートの唯一の出口であり、
    ここを通さずに書き込むコードを足すと事故前の「無条件write」に先祖返りする。
    そのため update_stocks.py 本体（yfinance依存でテストしにくい）ではなく
    stdlibのみの本モジュールに置き、tests/ で回帰テストしている。
    """
    if report.get("level") == FAIL:
        return False
    for path, obj in documents:
        write_json_atomic(path, obj)
    return True
