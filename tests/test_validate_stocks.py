# -*- coding: utf-8 -*-
"""validate_stocks.py のゴールデンテスト（N3・番人の番人）

「validator がバグって常時 pass」を防ぐため、実事故データを固定点として CI で固定する:
  - fb52f6a = price全滅スナップショット(2026-07-07 00:30 JST, price null 300/300) → 必ず FAIL
  - ce966d9 = 正常スナップショット(2026-07-14, price null 1/300) → 必ず OK
  - price充足 89% → FAIL / 91% → PASS の境界固定

fixture は tests/fixtures/<コミットSHA>/*.json.gz。出所は当該コミットの実データで、
`git show <sha>:stocks.json` 等でいつでも再検証できる（gzip化はサイズ削減のため）。

実行: python -m unittest discover -s tests -t .  (リポジトリルートから)
stdlib のみに依存（yfinance 不要・pytest 不要）。
"""

import copy
import datetime
import gzip
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import validate_stocks as v  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures"


def load_snapshot(sha):
    """fixture から (stocks_doc, market_doc, history_doc, tickers) を返す"""
    docs = {}
    for name in ("stocks", "market", "history", "tickers"):
        with gzip.open(FIXTURES / sha / f"{name}.json.gz", "rt",
                       encoding="utf-8") as f:
            docs[name] = json.load(f)
    return (docs["stocks"], docs["market"], docs["history"],
            docs["tickers"]["tickers"])


def synth_universe(n=300, day=0):
    """フロアを全て満たす健全な合成ユニバース（L2/N4の機構テスト用）。

    `day` は「何日目のスナップショットか」。価格が日ごとに動くので、
    prev=synth_universe(day=0) / new=synth_universe(day=1) と組めば
    「通常の営業日どうしの比較」になる（同じ day 同士だと全銘柄の価格が
    前日と完全一致し、ステール検知 stale_price が正しく warn を出す）。
    """
    tickers = [{"code": str(1000 + i), "name": f"T{i}"} for i in range(n)]
    stocks = [{
        "code": t["code"], "name": t["name"],
        "price": 100.0 + i * 0.1 + day,          # 銘柄ごと・日ごとに動く
        "per": 15.0, "market_cap": 1e9, "pbr": 1.0, "dividend_yield": 2.0,
        "roe": 0.08, "roa": 0.05, "psr": 1.2, "peg": 1.5, "eps": 120.0,
        "debt_ratio": 0.2, "equity_ratio": 0.5,
        "profit_status": "black", "revenue_growth": "increase",
    } for i, t in enumerate(tickers)]
    asof_date = datetime.date(2026, 7, 14) + datetime.timedelta(days=day)
    asof = f"{asof_date.isoformat()} 16:30:00"
    stocks_doc = {"updated_at": asof, "stocks": stocks, "errors": []}
    market_doc = {"updated_at": asof, "market_per": 15.0}
    # 週次履歴は updated_at 直近まで（12週分・値は毎週変動）。
    # 最終週が古すぎると stale_history warn が出るため、直近週まで詰める。
    last_week = datetime.date(2026, 7, 13) + datetime.timedelta(days=day)
    series = [{"date": (last_week
                        - datetime.timedelta(weeks=11 - w)).isoformat(),
               "close": 100.0 + w} for w in range(12)]
    history_doc = {
        "updated_at": asof,
        "indexes": {"universe_avg": [{"date": p["date"], "close": p["close"]}
                                     for p in series]},
        "stocks": {t["code"]: list(series) for t in tickers},
    }
    return stocks_doc, market_doc, history_doc, tickers


def checks_of(report, level=None):
    return [f["check"] for f in report["findings"]
            if level is None or f["level"] == level]


class GoldenFixedPoints(unittest.TestCase):
    """実事故・実正常データの固定点（これが崩れたら validator が壊れている）"""

    def test_collapse_fb52f6a_must_fail(self):
        # price全滅事故の実データ。errors=[] のまま3.3日無検知だった断面。
        s, m, h, t = load_snapshot("fb52f6a")
        report = v.validate(s, m, h, t)
        self.assertEqual(report["level"], v.FAIL)
        self.assertIn("price_coverage", checks_of(report, v.FAIL))
        # 設計書§5の要点: 全滅時もPERは283件生存＝PERゲート単独では見逃す
        self.assertGreaterEqual(report["stats"]["valid_per_count"], 200)

    def test_healthy_ce966d9_must_pass(self):
        s, m, h, t = load_snapshot("ce966d9")
        report = v.validate(s, m, h, t)
        self.assertEqual(report["level"], v.OK)
        self.assertEqual(report["findings"], [])

    def test_consecutive_real_days_stay_ok(self):
        # 実データの連続2営業日（3fc663a=07-13 → ce966d9=07-14）。
        # 回帰・サニティ・ステール検知のいずれも誤発火しないことを固定する
        # （実運用で毎日 warn が出るなら、それはオオカミ少年化＝制度の死）。
        s, m, h, t = load_snapshot("ce966d9")
        prev, _, _, _ = load_snapshot("3fc663a")
        report = v.validate(s, m, h, t, prev_stocks_doc=prev)
        self.assertEqual(report["level"], v.OK, report["findings"])
        self.assertEqual(report["findings"], [])

    def test_collapse_with_healthy_baseline_fails_delta_too(self):
        # 正常ベースライン→全滅: フロアと前日比の両方が fail を出す
        s_bad, m_bad, h_bad, t = load_snapshot("fb52f6a")
        s_ok, _, _, _ = load_snapshot("ce966d9")
        report = v.validate(s_bad, m_bad, h_bad, t, prev_stocks_doc=s_ok)
        self.assertEqual(report["level"], v.FAIL)
        self.assertIn("price_coverage", checks_of(report, v.FAIL))
        self.assertIn("delta_null_price", checks_of(report, v.FAIL))


class CoverageBoundary(unittest.TestCase):
    """price充足率 90% フロアの境界固定（89%=FAIL / 91%=PASS）"""

    def _with_price_nulls(self, null_count):
        """既存の null（1銘柄）も含めて合計 null_count 件に揃えた ce966d9 を検証"""
        s, m, h, t = load_snapshot("ce966d9")
        s = copy.deepcopy(s)
        made = sum(1 for st in s["stocks"] if st["price"] is None)
        for st in s["stocks"]:
            if made >= null_count:
                break
            if st["price"] is not None:
                st["price"] = None
                made += 1
        return v.validate(s, m, h, t)

    def test_89_percent_fails(self):
        # 300銘柄中 null 33件 = 充足 89.0% < 90% → FAIL
        report = self._with_price_nulls(33)
        self.assertEqual(report["level"], v.FAIL)
        self.assertIn("price_coverage", checks_of(report, v.FAIL))

    def test_91_percent_passes(self):
        # 300銘柄中 null 27件 = 充足 91.0% ≥ 90% → priceフロアは通過
        report = self._with_price_nulls(27)
        self.assertNotIn("price_coverage", checks_of(report, v.FAIL))
        self.assertEqual(report["level"], v.OK)


class DeltaRegression(unittest.TestCase):
    """L2 前日比回帰（フラッピング捕捉）の機構テスト"""

    def test_price_null_spike_fails(self):
        prev, _, _, _ = synth_universe(day=0)
        new, m, h, t = synth_universe(day=1)
        for st in new["stocks"][:105]:  # +35ppt
            st["price"] = None
        report = v.validate(new, m, h, t, prev_stocks_doc=prev)
        self.assertIn("delta_null_price", checks_of(report, v.FAIL))

    def test_per_null_spike_warns_only(self):
        # PER の欠損増は fail 権限なし（決算集中期の正当な事象を止めない・SRE反映）。
        # +12ppt: delta warn(>10ppt) には掛かるが、絶対フロア(有効PER≥200)は満たす。
        # ※+35ppt級の壊滅的なPER喪失は有効PER件数フロアが別途 fail で止める。
        prev, _, _, _ = synth_universe(day=0)
        new, m, h, t = synth_universe(day=1)
        for st in new["stocks"][:36]:  # +12ppt
            st["per"] = None
        report = v.validate(new, m, h, t, prev_stocks_doc=prev)
        self.assertIn("delta_null_per", checks_of(report, v.WARN))
        self.assertNotIn("delta_null_per", checks_of(report, v.FAIL))
        self.assertEqual(report["level"], v.WARN)

    def test_small_increase_stays_ok(self):
        prev, _, _, _ = synth_universe(day=0)
        new, m, h, t = synth_universe(day=1)
        for st in new["stocks"][:24]:  # +8ppt < 10ppt
            st["dividend_yield"] = None
        report = v.validate(new, m, h, t, prev_stocks_doc=prev)
        self.assertEqual(report["level"], v.OK)

    def test_universe_change_relaxes_fail_to_warn(self):
        prev, _, _, _ = synth_universe()
        for st in prev["stocks"][:50]:
            st["code"] = "9" + st["code"]  # code集合を変える
        new, m, h, t = synth_universe()
        for st in new["stocks"][:105]:
            st["market_cap"] = None  # +35ppt（フロア90%も割るが delta の級を見る）
        report = v.validate(new, m, h, t, prev_stocks_doc=prev)
        self.assertIn("delta_universe_changed", checks_of(report, v.WARN))
        self.assertIn("delta_null_market_cap", checks_of(report, v.WARN))
        self.assertNotIn("delta_null_market_cap", checks_of(report, v.FAIL))

    def test_missing_baseline_warns(self):
        new, m, h, t = synth_universe()
        report = v.validate(new, m, h, t, prev_stocks_doc=None)
        self.assertIn("delta_baseline", checks_of(report, v.WARN))


class ValueSanity(unittest.TestCase):
    """N4 値サニティ（すべて warn 止まり・fail 権限なし）"""

    def test_market_per_out_of_range_warns(self):
        s, m, h, t = synth_universe()
        m["market_per"] = 100.0
        report = v.validate(s, m, h, t)
        self.assertIn("sanity_market_per", checks_of(report, v.WARN))
        self.assertEqual(report["level"], v.WARN)

    def test_universe_avg_out_of_band_warns(self):
        s, m, h, t = synth_universe()
        h["indexes"]["universe_avg"][-1]["close"] = 1000.0
        report = v.validate(s, m, h, t)
        self.assertIn("sanity_universe_avg", checks_of(report, v.WARN))
        self.assertEqual(report["level"], v.WARN)

    def test_mass_price_jump_warns(self):
        # 分割二重調整型の破損（v3.6.1で実際に発生）を狙った検査
        prev, _, _, _ = synth_universe(day=0)
        new, m, h, t = synth_universe(day=1)
        for st in new["stocks"][:20]:  # 20/300 ≈ 6.7% > 5%
            st["price"] = 250.0  # +150%
        report = v.validate(new, m, h, t, prev_stocks_doc=prev)
        self.assertIn("sanity_price_jump", checks_of(report, v.WARN))

    def test_single_price_jump_tolerated(self):
        # 個別のストップ高・分割は黙認（5%閾値以下）
        prev, _, _, _ = synth_universe(day=0)
        new, m, h, t = synth_universe(day=1)
        new["stocks"][0]["price"] = 250.0  # 1/300
        report = v.validate(new, m, h, t, prev_stocks_doc=prev)
        self.assertEqual(report["level"], v.OK)


class StaleDetection(unittest.TestCase):
    """レッドチーム監査(F1/F4)の再発防止: 「壊れているのに正常に見える」凍結型の破損。

    取得が失敗して前日の値が居座ると、充足率100%・null増加ゼロ・跳びゼロで
    全ゲートを素通りする。しかも updated_at だけは新しくなるため、アプリの
    鮮度バナー(N1)もすり抜ける＝price全滅事故の"逆パターン"。
    ※東証の祝日休場では正常に起きるため、検知は warn 止まり（配信は止めない）。
    """

    def test_all_prices_identical_to_prev_warns(self):
        prev, _, _, _ = synth_universe(day=0)
        new, m, h, t = synth_universe(day=0)  # 同じ日＝価格が完全一致
        new["updated_at"] = "2026-07-15 16:30:00"  # 時刻だけ新しい
        report = v.validate(new, m, h, t, prev_stocks_doc=prev)
        self.assertIn("stale_price", checks_of(report, v.WARN))
        self.assertEqual(report["level"], v.WARN)  # 配信は止めない（祝日は正常）

    def test_real_consecutive_days_do_not_trigger_stale(self):
        # 実データの連続2営業日で価格が完全一致する銘柄は 1.3%（4/299）＝誤発火しない
        s, _, _, _ = load_snapshot("ce966d9")
        prev, _, _, _ = load_snapshot("3fc663a")
        m, h, t = load_snapshot("ce966d9")[1:]
        report = v.validate(s, m, h, t, prev_stocks_doc=prev)
        self.assertNotIn("stale_price", checks_of(report))

    def test_flatlined_universe_avg_warns(self):
        s, m, h, t = synth_universe()
        for p in h["indexes"]["universe_avg"][-3:]:
            p["close"] = 123.45  # 300銘柄の等加重平均が3週連続同値＝あり得ない
        report = v.validate(s, m, h, t)
        self.assertIn("stale_universe_avg", checks_of(report, v.WARN))

    def test_stale_history_warns(self):
        s, m, h, t = synth_universe()
        s["updated_at"] = "2026-09-01 16:30:00"  # 履歴だけ7月のまま止まっている
        report = v.validate(s, m, h, t)
        self.assertIn("stale_history", checks_of(report, v.WARN))


class CoverageFloors(unittest.TestCase):
    """レッドチーム監査(F2/F3)の再発防止: 前日比だけでは「じわじわ劣化」を永久に
    見逃す（ベースラインが毎日ずれるため）。ドリフトしない絶対フロアで捕らえる。"""

    def test_gradual_degradation_is_caught_by_absolute_floor(self):
        # 毎日+9ppt（delta warn の10pptを下回る）でも、累積で70%を割れば warn
        prev, _, _, _ = synth_universe(day=0)
        new, m, h, t = synth_universe(day=1)
        for st in new["stocks"][:100]:  # 充足 66.7% < 70%
            st["dividend_yield"] = None
        report = v.validate(new, m, h, t, prev_stocks_doc=prev)
        self.assertIn("coverage_dividend_yield", checks_of(report, v.WARN))

    def test_unwatched_field_total_loss_is_caught(self):
        # 旧実装では debt_ratio/profit_status は L1・L2 どちらの監視対象でもなく、
        # 100%欠損しても level=ok だった（アプリの「割安理由」表示が全滅する）
        s, m, h, t = synth_universe()
        for st in s["stocks"]:
            st["debt_ratio"] = None
            st["profit_status"] = None
        report = v.validate(s, m, h, t)
        self.assertIn("coverage_debt_ratio", checks_of(report, v.WARN))
        self.assertIn("coverage_profit_status", checks_of(report, v.WARN))

    def test_live_data_has_margin_against_floors(self):
        # 実データが日常的にフロアに触れていないこと（＝誤発火しない）を固定する。
        # 2026-07-14 実測の最低は peg 90.0%。フロア70%まで20ppt の余裕がある。
        s, m, h, t = load_snapshot("ce966d9")
        report = v.validate(s, m, h, t)
        for field in v.COVERAGE_WARN_FIELDS:
            self.assertGreaterEqual(
                report["stats"][f"{field}_coverage"],
                v.COVERAGE_WARN_MIN * 100 + 10,
                f"{field} の実データ充足率がフロアに近すぎる（誤発火の恐れ）")


class PublishGate(unittest.TestCase):
    """事故の再発そのもの＝「検証を素通りして書き込む」ことの回帰テスト。

    validate() のロジックだけでなく、その結果で**書込みが実際に止まるか**を固定する。
    ここが無いと、publish() の呼び出し順を入れ替える・fail判定を無視するといった
    些細な編集で「生成→無条件write」（事故前の姿）に静かに先祖返りできてしまう。
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.paths = [os.path.join(self.dir, n) for n in
                      ("stocks.json", "market.json", "history.json")]
        for p in self.paths:
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"sentinel": "前回の正本"}, f)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _docs(self):
        return [(p, {"sentinel": "新データ", "n": i})
                for i, p in enumerate(self.paths)]

    def _read(self, path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def test_fail_writes_nothing(self):
        report = {"level": v.FAIL, "findings": [], "stats": {},
                  "quarantine": []}
        self.assertFalse(v.publish(report, self._docs()))
        for p in self.paths:
            self.assertEqual(self._read(p)["sentinel"], "前回の正本")
            self.assertFalse(os.path.exists(p + ".tmp"))

    def test_ok_writes_all(self):
        report = {"level": v.OK, "findings": [], "stats": {}, "quarantine": []}
        self.assertTrue(v.publish(report, self._docs()))
        for p in self.paths:
            self.assertEqual(self._read(p)["sentinel"], "新データ")
            self.assertFalse(os.path.exists(p + ".tmp"))

    def test_warn_still_writes(self):
        # warn は「配信するが通知」（全凍結を避ける・設計書§5）
        report = {"level": v.WARN, "findings": [], "stats": {},
                  "quarantine": []}
        self.assertTrue(v.publish(report, self._docs()))
        for p in self.paths:
            self.assertEqual(self._read(p)["sentinel"], "新データ")

    def test_collapse_snapshot_is_never_published(self):
        # end-to-end: 全滅データを validate → publish に流しても書かれない
        s, m, h, t = load_snapshot("fb52f6a")
        report = v.validate(s, m, h, t)
        self.assertEqual(report["level"], v.FAIL)
        self.assertFalse(v.publish(report, self._docs()))
        for p in self.paths:
            self.assertEqual(self._read(p)["sentinel"], "前回の正本")

    def test_healthy_snapshot_is_published(self):
        s, m, h, t = load_snapshot("ce966d9")
        report = v.validate(s, m, h, t)
        self.assertTrue(v.publish(report, self._docs()))
        for p in self.paths:
            self.assertEqual(self._read(p)["sentinel"], "新データ")

    def test_atomic_write_leaves_no_temp_and_preserves_content(self):
        target = os.path.join(self.dir, "x.json")
        v.write_json_atomic(target, {"a": 1})
        v.write_json_atomic(target, {"a": 2})
        self.assertEqual(self._read(target), {"a": 2})
        self.assertFalse(os.path.exists(target + ".tmp"))


class InvalidValueDetection(unittest.TestCase):
    """監査(debugger)指摘の再発防止: price=0.0 は null と同じ「取得失敗」として扱う。
    0.0 は null と違いアプリの非表示分岐も素通りするため、素通りすると
    「全銘柄0円表示」が errors=[] のまま配信される（事故と同型の障害モード）"""

    def test_all_zero_price_fails(self):
        prev, _, _, _ = synth_universe(day=0)
        new, m, h, t = synth_universe(day=1)
        for st in new["stocks"]:
            st["price"] = 0.0
        report = v.validate(new, m, h, t, prev_stocks_doc=prev)
        self.assertEqual(report["level"], v.FAIL)
        self.assertIn("price_coverage", checks_of(report, v.FAIL))

    def test_partial_zero_price_quarantined(self):
        new, m, h, t = synth_universe()
        for st in new["stocks"][:5]:
            st["price"] = 0.0
        report = v.validate(new, m, h, t)
        for st in new["stocks"][:5]:
            self.assertIn(st["code"], report["quarantine"])

    def test_negative_market_cap_counts_as_invalid(self):
        new, m, h, t = synth_universe()
        for st in new["stocks"][:40]:  # 40/300 → 充足86.7% < 90%
            st["market_cap"] = -1
        report = v.validate(new, m, h, t)
        self.assertIn("market_cap_coverage", checks_of(report, v.FAIL))


class ValidatorRobustness(unittest.TestCase):
    """番人自身が死なない（型不正はクラッシュではなく fail 判定を返す）。
    監査(debugger)指摘: validator の未捕捉例外は「どのゲートが・何%で」を
    含まない劣化通知になるため、既知の型不正は明示的に判定する"""

    def test_market_per_string_fails_not_crash(self):
        s, m, h, t = synth_universe()
        m["market_per"] = "abc"
        report = v.validate(s, m, h, t)
        self.assertEqual(report["level"], v.FAIL)
        self.assertIn("market_per", checks_of(report, v.FAIL))

    def test_tickers_none_fails_not_crash(self):
        s, m, h, _ = synth_universe()
        report = v.validate(s, m, h, None)
        self.assertEqual(report["level"], v.FAIL)
        self.assertIn("schema_tickers", checks_of(report, v.FAIL))

    def test_non_dict_stock_entries_fail_not_crash(self):
        s, m, h, t = synth_universe()
        s["stocks"] = ["not_a_dict"] * 300
        report = v.validate(s, m, h, t)
        self.assertEqual(report["level"], v.FAIL)

    def test_string_per_excluded_not_crash(self):
        s, m, h, t = synth_universe()
        s["stocks"][0]["per"] = "15.0"
        report = v.validate(s, m, h, t)
        self.assertEqual(report["stats"]["valid_per_count"], 299)
        self.assertEqual(report["level"], v.OK)

    def test_nan_price_counts_as_invalid_not_crash(self):
        s, m, h, t = synth_universe()
        s["stocks"][0]["price"] = float("nan")
        report = v.validate(s, m, h, t)
        self.assertIn(s["stocks"][0]["code"], report["quarantine"])

    def test_missing_code_fails_not_crash(self):
        # レッドチーム(F6): code欠落＋price欠落で quarantine の sorted() が
        # TypeError（None < str）でクラッシュしていた＝番人が死ぬ
        s, m, h, t = synth_universe()
        for st in s["stocks"][:5]:
            del st["code"]
            st["price"] = None
        report = v.validate(s, m, h, t)
        self.assertEqual(report["level"], v.FAIL)
        self.assertIn("schema_code", checks_of(report, v.FAIL))

    def test_unhashable_code_fails_not_crash(self):
        # レッドチーム(F6): code が list だと集合演算で TypeError（unhashable）
        prev, _, _, _ = synth_universe(day=0)
        s, m, h, t = synth_universe(day=1)
        s["stocks"][0]["code"] = ["1000"]
        report = v.validate(s, m, h, t, prev_stocks_doc=prev)
        self.assertEqual(report["level"], v.FAIL)
        self.assertIn("schema_code", checks_of(report, v.FAIL))

    def test_malformed_history_series_fails_not_crash(self):
        # レッドチーム(F5): 週次履歴が「非空」判定だけだったため、floatの裸リストや
        # dict でもカバレッジを満たし、アプリ側で p["close"] が落ちる破損を素通りした
        s, m, h, t = synth_universe()
        for code in list(h["stocks"])[:200]:
            h["stocks"][code] = [100.0, 101.0]  # dictでなく裸のfloat
        report = v.validate(s, m, h, t)
        self.assertEqual(report["level"], v.FAIL)
        self.assertIn("history_coverage", checks_of(report, v.FAIL))


class SchemaAndQuarantine(unittest.TestCase):

    def test_missing_key_fails(self):
        s, m, h, t = synth_universe()
        del s["errors"]
        report = v.validate(s, m, h, t)
        self.assertEqual(report["level"], v.FAIL)
        self.assertIn("schema", checks_of(report, v.FAIL))

    def test_stock_count_collapse_fails(self):
        s, m, h, t = synth_universe()
        s["stocks"] = s["stocks"][:200]  # 200/300 < 90%
        report = v.validate(s, m, h, t)
        self.assertIn("schema_count", checks_of(report, v.FAIL))

    def test_quarantine_lists_missing_codes(self):
        s, m, h, t = synth_universe()
        s["stocks"][0]["price"] = None
        s["stocks"][1]["market_cap"] = None
        report = v.validate(s, m, h, t)
        self.assertEqual(report["level"], v.OK)  # フロアは満たす → 配信は続行
        self.assertIn(s["stocks"][0]["code"], report["quarantine"])
        self.assertIn(s["stocks"][1]["code"], report["quarantine"])

    def test_report_md_contains_gate_and_percent(self):
        # N2要件: 通知本文に「どのゲートが・何%で落ちたか」が入ること
        s, m, h, t = load_snapshot("fb52f6a")
        report = v.validate(s, m, h, t)
        md = v.render_report_md(report)
        self.assertIn("price_coverage", md)
        self.assertIn("%", md)
        self.assertIn("FAIL", md)


if __name__ == "__main__":
    unittest.main()
