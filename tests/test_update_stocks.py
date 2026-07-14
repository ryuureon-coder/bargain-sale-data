# -*- coding: utf-8 -*-
"""update_stocks.py の指標導出ロジックのテスト（NaN列バグの回帰固定）

背景（2026-07-15）:
  Yahoo は新年度の列を EPS・株式数だけ先に作ることがあり、その列の
  Net Income / Total Revenue は欠損する（実測で300銘柄中15件）。yfinance は
  全項目の日付の和集合を列にするため、income_stmt の最新列は NaN になる。
  旧実装は `fin.iloc[:, 0]` だけを見ており、`float(nan) > 0` も
  `float(nan) > prev` も False になるため、
    ・4期連続黒字の三井不動産・三菱重工などを "red"（赤字）
    ・増収の銘柄を "decrease"（減収）
  と無条件に配信していた（profit_status 13件・revenue_growth 12件）。
  profit_status は「収益: 赤字」表示だけでなくスクリーニングの除外条件でもあり、
  黒字の優良銘柄が割安リストから消えていた（present-but-wrong 型の事故）。

ここで固定する不変条件:
  「NaN 列は"赤字"でも"減収"でもない。値が無いだけである」
  および profit_status と profit_stability が同じ母集団を見ること
  （"red" なのに全期黒字、という自己矛盾を二度と出さない）。

yfinance/pandas に依存せず stdlib だけで走るよう、income_stmt を模す最小の
フェイクを使う（実行環境に yfinance を入れずに CI で回すため）。
実行: python -m unittest discover -s tests -t .  （リポジトリルートから）
"""

import math
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# update_stocks は yfinance をトップレベル import する。ここでは指標導出関数の
# ロジックだけを検査するため、import を通すためのスタブを差し込む。
sys.modules.setdefault("yfinance", types.ModuleType("yfinance"))

import update_stocks as u  # noqa: E402

NAN = float("nan")


class _Series:
    """income_stmt の1列（行名 -> 値）。存在しない行は pandas と同じく None"""

    def __init__(self, mapping):
        self._m = mapping

    def get(self, key):
        return self._m.get(key)


class _Iloc:
    def __init__(self, cols):
        self._cols = cols

    def __getitem__(self, key):
        _, col = key  # fin.iloc[:, col]
        return _Series(self._cols[col])


class _Frame:
    """income_stmt / balance_sheet の最小フェイク。
    列は日付降順（0=最新）＝yfinance の並び。index は全列の行名の和集合"""

    def __init__(self, cols):
        self._cols = cols
        self.empty = not cols
        self.shape = (0, len(cols))
        self.iloc = _Iloc(cols)
        seen = []
        for c in cols:
            for k in c:
                if k not in seen:
                    seen.append(k)
        self.index = seen


class _Ticker:
    def __init__(self, frame, balance_sheet=None):
        self.income_stmt = frame
        self.balance_sheet = balance_sheet


def ticker(*cols):
    """cols: 最新列から順に {"Net Income": x, "Total Revenue": y} を渡す"""
    return _Ticker(_Frame(list(cols)))


def ni(*values):
    """Net Income だけを持つ ticker（最新列から順）"""
    return ticker(*[{"Net Income": v} for v in values])


def rev(*values):
    """Total Revenue だけを持つ ticker（最新列から順）"""
    return ticker(*[{"Total Revenue": v} for v in values])


class ProfitStatusNaNColumn(unittest.TestCase):
    """本件の回帰: 最新列が NaN でも「赤字」と断定しない"""

    def test_nan_latest_falls_back_to_last_valid_black(self):
        # 8801 三井不動産の実形。2026/3期の列は EPS だけ先にでき Net Income は NaN。
        # 直近の有効期（2025/3 = +248.8B）は黒字 → "black"。旧実装は "red" を返した。
        self.assertEqual(u.get_profit_status(ni(NAN, 248.8e9, 224.6e9)), "black")

    def test_nan_latest_falls_back_to_last_valid_red(self):
        # 7201 日産の実形。NaN をスキップした先の直近期が赤字なら "red" のまま。
        # ＝NaN スキップが赤字を「黒字」に化けさせないことの担保。
        self.assertEqual(u.get_profit_status(ni(NAN, -670.9e9)), "red")

    def test_real_negative_latest_is_red(self):
        # 最新列に実値の赤字が入っている場合まで NaN 扱いにしない（6758 ソニーの形）
        self.assertEqual(u.get_profit_status(ni(-326.9e9, 1141.6e9)), "red")

    def test_real_positive_latest_is_black(self):
        self.assertEqual(u.get_profit_status(ni(3848.1e9, 4765.1e9)), "black")

    def test_zero_is_red(self):
        # 純利益ゼロは黒字ではない（旧実装の `> 0` の意味論を維持）
        self.assertEqual(u.get_profit_status(ni(0.0)), "red")

    def test_all_nan_is_none(self):
        # 「値が無い」を「赤字」に化けさせない。null はアプリ側で欠損として扱う
        self.assertIsNone(u.get_profit_status(ni(NAN, NAN)))

    def test_missing_row_is_none(self):
        self.assertIsNone(u.get_profit_status(ticker({"Total Revenue": 1.0})))

    def test_empty_frame_is_none(self):
        self.assertIsNone(u.get_profit_status(_Ticker(_Frame([]))))

    def test_none_income_stmt_is_none(self):
        self.assertIsNone(u.get_profit_status(_Ticker(None)))


class ProfitStatusMatchesStability(unittest.TestCase):
    """profit_status と profit_stability が同じ母集団を見る（自己矛盾の禁止）。

    事故の可視的な症状は「"red" なのに profit_years_black == profit_years_total」
    だった。両者が同じ NaN スキップ規則で動く限り、この矛盾は構造的に起きない。
    """

    def _assert_consistent(self, t):
        status = u.get_profit_status(t)
        black, total = u.get_profit_stability(t)
        if status == "red":
            # 赤字を名乗るなら、黒字でない期が最低1つ存在しなければならない
            self.assertIsNotNone(total)
            self.assertLess(black, total,
                            "red なのに全期黒字＝2026-07-14 に検知された自己矛盾")
        elif status == "black":
            self.assertGreater(black, 0)
        else:
            self.assertIsNone(total)

    def test_four_black_years_with_nan_latest(self):
        # 2768 双日の実形（4期連続黒字＋最新列 NaN）
        t = ni(NAN, 110.6e9, 100.8e9, 111.2e9, 82.3e9)
        self._assert_consistent(t)
        self.assertEqual(u.get_profit_status(t), "black")
        self.assertEqual(u.get_profit_stability(t), (4, 4))

    def test_mixed_years_with_nan_latest(self):
        # 6770 アルプスアルパイン（4期中3期黒字＋最新列 NaN・直近有効期は黒字）
        t = ni(NAN, 37.8e9, -29.8e9, 11.5e9, 23.0e9)
        self._assert_consistent(t)
        self.assertEqual(u.get_profit_status(t), "black")
        self.assertEqual(u.get_profit_stability(t), (3, 4))

    def test_genuinely_red_latest(self):
        t = ni(-326.9e9, 1141.6e9, 970.6e9, 1005.3e9)
        self._assert_consistent(t)
        self.assertEqual(u.get_profit_stability(t), (3, 4))

    def test_all_nan_both_none(self):
        t = ni(NAN, NAN)
        self._assert_consistent(t)
        self.assertEqual(u.get_profit_stability(t), (None, None))


class RevenueGrowthNaNColumn(unittest.TestCase):
    """同型の NaN 列バグ: 最新列が NaN でも「減収」と断定しない"""

    def test_nan_latest_compares_two_valid_periods(self):
        # 旧実装は float(nan) > prev = False → 無条件 "decrease"（12銘柄で誤配信）
        self.assertEqual(u.get_revenue_growth(rev(NAN, 200.0, 100.0)), "increase")

    def test_nan_latest_genuine_decrease(self):
        self.assertEqual(u.get_revenue_growth(rev(NAN, 100.0, 200.0)), "decrease")

    def test_real_values_unaffected(self):
        self.assertEqual(u.get_revenue_growth(rev(300.0, 200.0)), "increase")
        self.assertEqual(u.get_revenue_growth(rev(100.0, 200.0)), "decrease")

    def test_flat_revenue_is_decrease(self):
        # 横ばいは増収ではない（旧実装の `>` の意味論を維持）
        self.assertEqual(u.get_revenue_growth(rev(200.0, 200.0)), "decrease")

    def test_only_one_valid_period_is_none(self):
        self.assertIsNone(u.get_revenue_growth(rev(NAN, 100.0)))

    def test_zero_denominator_is_none(self):
        self.assertIsNone(u.get_revenue_growth(rev(100.0, 0.0)))

    def test_all_nan_is_none(self):
        self.assertIsNone(u.get_revenue_growth(rev(NAN, NAN, NAN)))

    def test_empty_frame_is_none(self):
        self.assertIsNone(u.get_revenue_growth(_Ticker(_Frame([]))))


def bs(*cols):
    """balance_sheet を持つ ticker。cols は最新列から順の dict"""
    return _Ticker(None, _Frame(list(cols)))


def row(debt, assets):
    d = {}
    if debt is not None:
        d["Total Debt"] = debt
    if assets is not None:
        d["Total Assets"] = assets
    return d


class DebtRatioNaNColumn(unittest.TestCase):
    """欠損を「無借金」に化けさせない（欠損が"良い評価"になる分だけ危険）。

    「Total Debt が NaN → 0.0」は、借金のある企業に「無借金」の財務ラベルを与える。
    アプリは無借金銘柄を優先ソートする（main.dart の passed.sort が isDebtFree を
    最優先）ため、割安条件を満たせば一覧の最上位に押し上げられる経路でもある。
    7012 川崎重工（自己資本比率26.4%・2022/3期に有利子負債5,010億円の記録）が
    実際にこれで「無借金＋黒字」と配信されていた（2026-07-15 監査）。
    """

    def test_no_debt_row_at_all_is_debt_free(self):
        # Total Debt が1期も無い＝Yahoo が値0の項目を落としたケース。
        # キーエンス・任天堂・ファナック等が該当し、実際に実質無借金。従来動作を維持する
        # （ここを None にすると本物の無借金企業がスクリーニングから消える）
        self.assertEqual(u.get_debt_ratio(bs(row(None, 3671e9),
                                             row(None, 3000e9))), 0.0)

    def test_all_nan_debt_is_debt_free(self):
        self.assertEqual(u.get_debt_ratio(bs(row(NAN, 3671e9),
                                             row(NAN, 3000e9))), 0.0)

    def test_nan_latest_falls_back_to_last_valid_period(self):
        # 6920 レーザーテックの実形。最新2期が NaN、2023/6期に 5B/272B = 1.8%。
        # 旧実装は 0.0（無借金）を返していた。1.8% は DEBT_FREE_THRESHOLD(1%) を
        # 超えるので、アプリの表示も「無借金」から「財務健全」に是正される
        t = bs(row(NAN, 330e9), row(NAN, 271e9), row(5e9, 272e9), row(10e9, 179e9))
        self.assertEqual(u.get_debt_ratio(t), 0.018)

    def test_debt_without_matching_assets_is_none(self):
        # 7012 川崎重工の実形。最新4期は Total Debt が NaN、値のある 2022/3期は
        # 逆に Total Assets が欠損している＝同一時点で比率を作れない。
        # 「2022年の負債 ÷ 2023年の総資産」をでっち上げず None（データなし）にする。
        # 0.0 を返して"無借金"を名乗らせないことがここの主眼。
        t = bs(row(NAN, 3325e9), row(NAN, 3017e9), row(NAN, 2680e9),
               row(NAN, 2458e9), row(501e9, None))
        self.assertIsNone(u.get_debt_ratio(t))

    def test_normal_latest_value_unaffected(self):
        self.assertEqual(u.get_debt_ratio(bs(row(300e9, 1000e9))), 0.3)

    def test_negative_ratio_is_none(self):
        self.assertIsNone(u.get_debt_ratio(bs(row(-1e9, 1000e9))))

    def test_zero_assets_is_none(self):
        self.assertIsNone(u.get_debt_ratio(bs(row(1e9, 0))))

    def test_no_assets_at_all_is_none(self):
        # 総資産すら取れない＝データ品質不明。無借金(0.0)と断言しない
        self.assertIsNone(u.get_debt_ratio(bs(row(None, None))))

    def test_empty_frame_is_none(self):
        self.assertIsNone(u.get_debt_ratio(_Ticker(None, _Frame([]))))

    def test_none_balance_sheet_is_none(self):
        self.assertIsNone(u.get_debt_ratio(_Ticker(None, None)))

    def test_below_debt_free_threshold_stays_debt_free(self):
        # 6526 ソシオネクストの形（最新列 NaN・前期にごく小さな有利子負債）。
        # 是正後の値が build_reason の「無借金」閾値(1%)を下回る限り表示は変わらない。
        # ＝この修正は「借金があるのに無借金」だけを剥がし、実質無借金は温存する
        ratio = u.get_debt_ratio(bs(row(NAN, 168e9), row(1e9, 170e9)))
        self.assertEqual(ratio, 0.006)
        self.assertLess(ratio, u.DEBT_FREE_THRESHOLD)


class BuildReasonBlackLabel(unittest.TestCase):
    """reason 文字列の「黒字」付与は profit_status に従属する（誤りの伝播経路）"""

    def test_black_appends_label(self):
        # per 10.0 < market_per 15.0 × 0.7 = 10.5 → 「低PER」の側
        self.assertEqual(u.build_reason(10.0, 15.0, 0.2, "black"),
                         "低PER＋財務健全＋黒字")

    def test_red_omits_label(self):
        # NaN列バグ時の実際の出力（8801 三井不動産＝"市場平均よりやや低PER"）
        self.assertEqual(u.build_reason(14.0, 15.0, None, "red"),
                         "市場平均よりやや低PER")

    def test_none_status_omits_label(self):
        self.assertEqual(u.build_reason(None, 15.0, None, None), "データ不足")


class SafeRound(unittest.TestCase):
    """NaN が JSON に漏れない（json.dump は既定で NaN を素通しさせる）"""

    def test_nan_becomes_none(self):
        self.assertIsNone(u.safe_round(NAN))

    def test_none_stays_none(self):
        self.assertIsNone(u.safe_round(None))

    def test_normal_value_rounds(self):
        self.assertEqual(u.safe_round(1.234, 2), 1.23)

    def test_non_numeric_is_none(self):
        self.assertIsNone(u.safe_round("abc"))

    def test_infinity_survives_as_is(self):
        # 現状の仕様を明示的に固定する（inf は素通りし JSON に Infinity が出る）。
        # price/market_cap は validate_stocks._num() が inf を無効値として弾くが、
        # per/pbr 等は素通りする。実データでの発生は未観測のため現状維持とし、
        # 発生したらここを赤くして気付けるようにしておく。
        self.assertEqual(u.safe_round(math.inf), math.inf)


if __name__ == "__main__":
    unittest.main()
