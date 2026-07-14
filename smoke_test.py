# -*- coding: utf-8 -*-
"""CIスモークテスト（L6a・再発防止 Phase 1）

生成本体とは独立に「このCI環境で yfinance が実データを取れるか」だけを検証する。
price全滅事故の直接原因（CI環境で ticker.history(period="5d") が空を返す）は
コードを読んでも分からない環境固有バグだった。実 fetch して観測するのが唯一の検知手段
（設計書P3:「読む」より「動かして観測する」）。

価格取得は update_stocks.get_current_price を**そのまま呼ぶ**。ここで取得ロジックを
再実装すると、本体だけ改修されたときにスモークが古い基準で「環境は健全」と誤判定し、
このテストの存在意義（本体と同じ取得戦略で環境固有バグを先取りする）が空文化する。

canary 3銘柄中 2銘柄以上で price が取れれば合格（1銘柄の一時不調は許容）。
不合格なら exit(1) → update ジョブは実行されない（needs ゲート）。
update.yml 側で60秒後に1回リトライして一時不調の誤ブロックを減らす。
"""

import sys

import yfinance as yf

from update_stocks import get_current_price

# 流動性が高く上場廃止リスクが実質無い canary（東証プライム大型）
CANARIES = ["7203.T", "6758.T", "8306.T"]  # トヨタ / ソニーG / 三菱UFJ


def main():
    ok = 0
    for symbol in CANARIES:
        price, _open, _close, source = get_current_price(yf.Ticker(symbol))
        print(f"[smoke] {symbol}: price={price} ({source})")
        if price is not None:
            ok += 1
    if ok < 2:
        print(f"[smoke] FAIL: canary {len(CANARIES)}銘柄中 {ok}銘柄しか価格を"
              f"取得できません。CI環境でのデータ取得に問題があります。")
        sys.exit(1)
    print(f"[smoke] OK: {ok}/{len(CANARIES)}")


if __name__ == "__main__":
    main()
