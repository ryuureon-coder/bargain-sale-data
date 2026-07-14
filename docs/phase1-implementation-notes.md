# Phase 1 実装方針メモ（再発防止 v2 の実装デルタ・2026-07-14）

> **位置づけ**：正本設計書 `recurrence-prevention.md`（v2）が残した要確認事項②〜④への
> **暫定決定**と、設計書に書かれていない実装レベルの選択の明文化。
> レビュー履歴：fact-checker（前提9件すべて✅・2026-07-14）→ critical-reviewer
> **SREペルソナ（レッドチームレビュー・シミュレーション）**（🔴3件・🟡5件・⚪3件、
> 下記§5に反映内容を記録）。
> ⚠️ 「暫定決定」はユーザー最終承認前。コミットゲート（原則5）で承認を得る。

---

## 1. 前提事実（fact-checker 検証済み・全9件✅）

1. 2026-07-14 21:20 JST 時点の本番データは健全：stocks.json は 300銘柄・price null 1/300・
   market_cap null 1/300・有効PER（0<per<200）283件・market_per 18.69・
   history.json は 300銘柄・universe_avg 53点（最終点 154.38）・errors=[]。
2. Phase 1 は完全に未実装：`update.yml` は 生成→無条件 commit/push のまま。検証ステップ・
   通知ステップ・テストジョブは存在しない。`validate_stocks.py`・テストファイルは存在しない。
3. 自動更新は継続稼働中：直近コミットは ce966d9（07-14）・3fc663a（07-13）・486080d（07-10）。
   07-11(土)・07-12(日) が無いのは cron が平日のみのため正常。
4. 全滅スナップショット fb52f6a（07-07 00:30 JST commit）は price null 300/300・
   dividend_yield null 300/300（price依存の連鎖）・有効PER 283件生存・market_per 18.79。
   正常スナップショット ce966d9 は price null 1/300（ゴールデンテストの2固定点）。
5. 実行環境：ローカルWindowsに実働Pythonなし・WSLに Python 3.10.6＋git 2.34.1・node v19。
   CI（GitHub Actions）は python 3.12 + pip install yfinance（バージョン未固定）。
6. アプリ側 `main.dart`（git管理外）は `updated_at` を12pxテキスト表示のみ（比較なし）、
   `price==null` は表示テキストの条件分岐で非表示。データ取得元は `kDataBase` 定数
   （raw.githubusercontent）。
7. cron予定（16:30 JST）と updated_at の差は実測 約4.8h（07-14）・約9.5h（07-13）。
   ※「キュー遅延＋checkout/pip/生成実行（300銘柄×1.5s≒8分）＋push」の合算。
   「GitHub Actions の schedule 遅延は常態」は外部プラットフォームの公知仕様であり
   リポジトリ内一次情報からは再導出不能（fact-checker指摘・一般知識として扱う）。
8. yfinance 1.5.1 は 2026-06-28 リリースで、以降 2026-07-14 現在まで新リリース無し
   （PyPIリリース履歴で確認）。よって 07-09〜07-14 の成功 run（バージョン未固定の
   pip install）はすべて 1.5.1 で解決されていたと導出できる。🟠run ログ直接確認は
   認証が必要なため未実施。リリース時系列からの導出。
9. 週次実機点検（L6b-lite）は `bargain-sale-weekly-check`（毎週月曜09:03・enabled）として
   稼働済み。直近実行 2026-07-13。

## 2. 決定（設計書の要確認事項への回答案・SREレビュー反映後）

| # | 設計書の問い | 決定 | Why |
|---|---|---|---|
| ② | N2失敗通知の届け先 | **GitHub Issue 起票＋assignee にリポジトリオーナーを設定**（`issues: write`＋gh CLI。assignee 設定で通知タブ・モバイル通知に確実に乗せる）。さらに **Phase 1 完了条件に「実地ドリル」を含める**：workflow_dispatch の `drill` 入力で test ジョブを意図的に失敗させ、①Issueが実際に立つ ②本人に通知が届く、を目視確認するまでN2は未完了扱い。Discord/LINE webhook はユーザーがURLを用意した時点で追加 🟠最終的に「あなたが実際に読む先」かはドリルで本人確認 | テストしていないアラートは「無い」のと同じ（SRE指摘🔴1）。追加シークレット無しで実装でき、失敗履歴がリポジトリに残る |
| ③ | 鮮度バナー閾値 | **96h**（設計書の提案帯 72–96h の上限）。文言は事実陳述型「データは◯時間前のものです」。**コード内コメントに「cronが祝日を認識しない前提に依存。祝日スキップを追加したら閾値見直し」を明記**。初期数週間は実測遅延の分布を見て §8 の再校正で確定 | 実測遅延（4.8h/9.5h）込みでも、祝日も cron が回る現状なら 96h は超えない。バッファ24hは薄いため前提をコードに足止めする（SRE指摘🟡6） |
| ④ | L1閾値 | 絶対フロア＝設計書§5の提案値をそのまま採用（price充足≥90%・market_cap充足≥90%・有効PER≥200・market_per not None・history≥ticker数×90%・universe_avg≥10・スキーマ検査）。前日比回帰＝null率 **+10ppt超=warn**（主要フィールド全般）／**+30ppt超=fail は price・market_cap の2フィールドに限定**。ユニバース変更（code集合変化）検知時は回帰をwarn止まりに自動緩和 | fail権限を広げすぎると決算集中期のPER欠損増など正当な事象で全配信が止まる（SRE指摘🔴3）。事故の実態（price/market_cap系のnull急増）に絞る |

## 3. 実装レベルの選択（SREレビュー反映後）

1. **N4 値サニティ**（すべて warn 止まり）：`universe_avg` 最終点 ∈ [30, 500]／`market_per` ∈ [5, 60]／
   price 前日比 ±40% 超の銘柄が全体の 5% 超 → warn。fail 権限は与えない（誤検知コスト＞見逃しコスト。
   present-but-wrong の本丸は L6 人間観測）。
2. **quarantine の Phase1 スコープ**：不良銘柄コードの検出・検証レポートへの列挙まで。
   `data_quality` の JSON 出力（L4）とアプリ側除外（L3）は Phase 2（スキーマ変更はアプリ改修とセット）。
3. **ゴールデンテストの fixture**：`git show` で抽出した実スナップショットを **gzip 圧縮してコミット**
   （`tests/fixtures/{fb52f6a,ce966d9}/*.json.gz`、計数百KB）。fetch-depth: 0 は使わない。
   Why: 毎日肥大化する履歴の全cloneコスト・履歴書き換えへの暗黙結合を避ける（SRE指摘🟡5の代替①）。
   出所（コミットSHA）はfixtureディレクトリ名とテスト内コメントに明記し、`git show` で再検証可能にする。
4. **テストランナー**：stdlib `unittest`。ローカル WSL Python 3.10 と CI 両方で追加インストール無し。
5. **L5 ピン止め**：`yfinance==1.5.1`（§1-8 の導出により直近成功runと同一）、
   `actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5` (v4)、
   `actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065` (v5)。
6. **ジョブ構成**：`test`（golden・番人の番人）と `smoke`（L6a・実fetch canary）の**両方を
   `update` のゲートにする**（`needs: [test, smoke]`）。smoke には リトライ（2回・間隔60s）を
   入れて一時不調の誤ブロックを減らす。`notify`＝いずれかのジョブ失敗時に Issue 起票
   （updateジョブの job output 経由で「どのゲートが・何%で落ちたか」を本文に含める）。
   Why: smoke と update は同じ Yahoo API を叩くため「smokeだけ落ちてupdateは健全」は稀で、
   ゲート化の方が「書き込み前に安く止める」というL6aの意図に合う（SRE指摘🟡4の代替①）。
   ※設計書の「独立」の解釈を変えた点はユーザー確認事項（§4）。
7. **warn の通知集約**：fail は都度新規 Issue。warn は「オープン中の `data-warn` ラベル Issue が
   あればコメント追記、なければ新規作成」でアラート疲れを防ぐ（SRE指摘🟡7）。
8. **atomic書込**：3ファイルとも temp（同一ディレクトリ）→ `os.replace` の逐次swap。
   クロスファイル不整合はミリ秒窓のクラッシュ時のみ理論上残る（コード内コメントに明記）。
9. **main.dart の編集手順**：git管理外のため、編集前にスクラッチパッドへバックアップ退避。
10. **updated_at のタイムゾーン**：「パース→9h引いてUTC扱い→now(UTC)と比較」。フォーマットが
    JST naive である前提をコメントに明記（変更時は要見直し）。
11. **ドリル機構**：`workflow_dispatch` に `drill` 入力（boolean）。true なら test ジョブを
    意図的に失敗させて通知経路全体（test fail → update skip → notify → Issue＋assignee通知）を
    発火させる。データには一切触れない安全な訓練経路。

## 4. 完了した検証 / 🟠 残る事項

**完了（2026-07-15 実地ドリルで実証）**：
- コミット・push 済み（ecf4392）。ユーザー承認取得済み。
- **通知経路ドリル実施済み**（run 29363349107）：`test`=意図的失敗 → `update`=**スキップ**
  （データに触れない）→ `notify`=成功 → **[Issue #1](https://github.com/ryuureon-coder/bargain-sale-data/issues/1) が起票**（data-fail ラベル・assignee 設定）
  → **ユーザーに通知が届いたことを本人が確認**。SRE指摘🔴1「テストしていないアラートは
  アラートが無いのと同じ」への回答が実物で出た。**N2 完了**。
- 同 run で `smoke`（L6a canary 実fetch）が**本番CI環境で成功** ＝ 事故の直接原因だった
  「CI環境でだけ price が取れない」現象が現在は起きていないことを実測で確認。

**🟠 残る事項**：
- smoke をゲート化した解釈変更（設計書「独立」→「独立に検知しつつゲートもする」）の追認
- L1 閾値・鮮度バナー96h の再校正（数週間の観測分散が溜まったら設計書§8どおり引き直す。
  現状の校正データは実測遅延2点（4.8h／9.5h）のみ）
- ステール検知（F1）の閾値95%は「連続2営業日の実測1.3%」1サンプルにしか校正できていない。
  祝日を挟んだ週の実測が溜まったら見直す。
- Phase 2（L4 `data_quality` 出力 → L3 アプリ欠損可視化 → L6b 週1実機観測の正式化）は未着手。

## 5. 監査記録（debugger通常モード＋data-auditor・2026-07-14）

**debugger（実行ベース検証）**：ユニットテスト・実データ3シナリオ（現行=ok／fb52f6a=fail／
現行+前日ベースライン=ok）・write_json_atomic単体・flutter analyze/test すべて通過。
指摘2件を同日修正済み：
- **Critical: price=0.0 が全ゲートを素通り**（nullでないため充足率・前日比・跳び検知の
  すべてが素通り。しかも get_current_price の副経路にだけ正値チェックが無く現実に到達可能）
  → ①副経路に主経路と同じ `>0` チェック追加 ②validator側で price/market_cap は
  「0以下・NaN・非数値も取得失敗」として充足率にカウント（`_is_valid`）③回帰テスト追加。
- **Medium: 型不正入力で validator 自身がクラッシュ**（番人が死ぬとN2通知から
  「どのゲートが・何%で」が消える）→ ①tickers/stocks要素/market_per/per の型不正を
  明示的に fail 判定化 ②呼び出し側で未捕捉例外を fail レポートに変換（fail-closed）
  ③堅牢性テスト追加。修正後 27テスト全通過。

**data-auditor（データ検分）**：ゲート閾値と直近5営業日の実測マージン=十分
（price充足99.3-99.7% vs フロア90%等、正常日の誤発火リスク低）。fixture 8ファイルは
`git show` とバイト単位で完全一致。前日比null率変動は実測最大0.34ppt（warn閾値10pptに
余裕）。±40%跳びは全ペア0件。
- 🟡 **スコープ外の別バグ疑いを検出**: profit_status と eps の矛盾 16/300銘柄
  （4期連続黒字なのに"red"が12件）。NaN処理の非対称が原因仮説（未検証）。
  → 別タスクとして切り出し済み（本Phase 1では触らない）。
- 🟡 6976太陽誘電の1年で約8倍の推移は内部整合するが外部アンカー未突合
  → 次回L6週次点検の確認項目へ。

## 5.5 レッドチーム監査（debugger 監査モード・2026-07-15、コミット ecf4392 に対して）

**「この安全網をすり抜けて壊れたデータを配信できるか？」**を問うた結果、**🔴3件のすり抜け経路**が
見つかった。いずれも「price全滅事故の"逆パターン"＝壊れているのに正常に見える」型。修正済み。

| # | すり抜け経路 | なぜ素通りしたか | 対策（実装済み） |
|---|---|---|---|
| **F1** | **ステール（凍結）**：全銘柄の価格が前日と完全同一でも `ok`。しかも `updated_at` だけ新しくなるため**アプリの鮮度バナー(N1)も同時にすり抜ける** | 充足率100%・null増加ゼロ・跳びゼロ＝既存の全ゲートが「壊れていない」と判定 | `stale_price`：価格が前日と完全一致する銘柄が95%超で **warn**。実測では連続2営業日で一致するのは**1.3%（4/299）**のみ。※cronは祝日も走り東証は休場＝正常に~100%一致するため **fail にはしない**（正常な休場日に全配信を止めない） |
| **F2** | **じわじわ劣化**：pbr/dividend_yield/roe を毎日+9ppt（delta warn の10ppt未満）ずつ欠損させると、**10日で90%欠損しても10日間 `ok` のまま** | L2の前日比はベースラインが毎日ずれるため累積劣化を追えない。この3項目にはL1絶対フロアが無い | `coverage_*`：**絶対充足率フロア70%（warn）**を12指標に追加。ドリフトしない網。実測の最低は peg 90.0% で20ppt の余裕 |
| **F3** | **無監視フィールド**：debt_ratio・profit_status 等は L1/L2 どちらの監視対象でもなく、**100%欠損しても `ok`**（アプリの「割安理由」表示が全滅する） | `DELTA_WATCH_FIELDS` に含まれず、フロアも無い | 同上（12指標に拡大）＋ `DELTA_WATCH_FIELDS` に eps/debt_ratio/profit_status を追加 |
| **F4** | market.json / history.json には**前日比較の機構が構造的に存在しない**（universe_avg が数週間フラットラインでも `ok`） | `validate()` は `prev_stocks_doc` しか受け取らない | `stale_universe_avg`（末尾3点が同値＝300銘柄の等加重平均が3週連続一致は実質あり得ない）＋ `stale_history`（履歴の最終週が `updated_at` から14日以上前）。いずれも絶対値で判定するため prev 不要 |
| **F5** | 週次履歴が「非空」判定だけで、**裸のfloatリストやdictでもカバレッジを満たす**（アプリ側で `p["close"]` が落ちる破損を素通り） | truthy 判定のみ | `_series_is_valid`：末尾要素が `{date:str, close:num}` であることを検査 → fail |
| **F6** | **番人が死ぬ**：`code` 欠落（`sorted()` が None<str で TypeError）／`code` が list（集合演算で unhashable）で validate() 自身がクラッシュ | code の型を検査していなかった | `_code_of()` で正規化し、不正コードは `schema_code` で **fail**。※クラッシュ時も update_stocks 側の fail-closed で配信は止まる（実地確認済み）ことは確認できていた |

**修正後の再実測**（同じ攻撃6件を再実行）：F1=warn / F2=warn / F3=warn / F4=warn / F5=fail / F6=fail、
**実データは ok のまま（誤発火なし）**。テストは 83件全通過（うち validate 用 46件）。

**レッドチームが破れなかったもの**（＝設計どおり機能している）：
- `publish()` のバイパス経路は存在しない（`open(...,"w")` の全数確認）。fail 時は1バイトも書かれない。
- 境界値の実装とテストの主張が一致（price充足率ちょうど90.0%=ok／89.7%=fail、前日比ちょうど+30.0ppt=warn／+30.33ppt=fail）。`<` と `<=` の取り違えなし。

## 6. 最終レビュー記録（senior-code-reviewer＋security-compliance-reviewer・2026-07-14）

両者とも **🔴コミットブロッカー 0件**。指摘のうち安価かつ本Phaseの目的に直結するものを反映：

**senior-code-reviewer（コード品質）**
- 🟡 **ゲート自体に自動テストが無い**（`main()`が一枚岩で、fail時に書込みを止める配線が
  回帰テストされていない＝「生成→無条件write」への先祖返りを検知できない）
  → **反映**：書込ゲートを `validate_stocks.publish(report, documents)` として
  stdlib のみの純粋関数に切り出し（`write_json_atomic` も同モジュールへ移動）、
  `PublishGate` テスト6件を追加（fail=1バイトも書かない／ok・warn=書く／
  全滅スナップショットのend-to-end／temp残留なし）。update_stocks は publish 経由のみ書込。
- 🟡 **`_is_valid` が per/pbr 等の型不正を素通し** → **反映**：`NUMERIC_FIELDS` を定義し
  数値であるべきフィールドは `_num` で検査。
- 🟡 **smoke_test が get_current_price を再実装**（本体だけ改修されるとスモークが
  古い基準で「健全」と誤判定し空文化）→ **反映**：`from update_stocks import
  get_current_price` に変更し実装を一本化。
- 🟡 鮮度閾値96hの前提が2リポジトリ間の手書きコメント同期に依存（機械的強制力なし）
  → 記録のみ。cron祝日対応を入れる際の見落としポイント。
- ⚪ drill実行時もsmokeは実APIを叩く（3コール・データには触れない）・N4跳び検知は
  ユニバース変化時に緩和されない（warn止まりのため実害小）・stock要素の`code`キー未検証 → 記録のみ。

**security-compliance-reviewer（セキュリティ・第8原則）**
- **式インジェクション：問題なし**（`${{ }}` を run 本文に直接埋めず `env:` 経由の
  `"$REPORT"` 参照という定石。`pull_request`/`pull_request_target` トリガも無く、
  フォークPR経由という最重大クラスの入口が存在しない）。
- **actions SHAピン：正当**（GitHub APIで実測。checkout=v4/v4.3.1、setup-python=v5/v5.6.0 と一致）。
- **第8原則：問題なし**（fixtureをemail/電話/ローカルパスのパターンで全件機械走査し0件。
  上場企業の公開指標のみ。`validation_report` は .gitignore 済み。GH_TOKENは短命トークン）。
- **アプリ側：問題なし**（新規の外部送信先なし・`INTERNET` 以外の権限追加なし）。
- 🟡 **heredocデリミタが固定文字列**（環境ファイルインジェクション対策としてGitHub公式が
  ランダム化を推奨）→ **反映**：`EOF_$(openssl rand -hex 8)` に変更。
- 🟡 **permissions が全ジョブ一律**（yfinance を実行する smoke/update に不要な
  `issues:write` が乗る＝依存汚染時の被害が広がる）→ **反映**：既定を `contents: read` にし、
  `update` のみ `contents: write`、`notify`/`warn-notify` のみ `issues: write`。
- ⚪ yfinance の推移的依存（pandas/numpy等）は未固定。lockファイル導入は将来検討 → 記録のみ。

## 7. SREペルソナレビュー（シミュレーション）の反映記録

- 🔴1 通知経路未検証 → ドリル機構＋assignee＋完了条件化で反映（§2-②・§3-11）
- 🔴2 yfinanceピン版未確認 → PyPIリリース時系列から直近成功run=1.5.1 を導出（§1-8）
- 🔴3 delta-fail対象曖昧 → price/market_cap に限定（§2-④）
- 🟡4 smoke非ゲート化 → ゲート化＋リトライに変更、ユーザー確認事項に明示（§3-6・§4）
- 🟡5 fetch-depth:0 の恒久コスト → gzip fixture コミット方式に変更（§3-3）
- 🟡6 鮮度96hの暗黙前提 → コード内コメント＋初期観測で再校正（§2-③）
- 🟡7 Issue重複によるアラート疲れ → warn集約ルール（§3-7）
- 🟡8 L6b空白期間 → 週次点検 `bargain-sale-weekly-check` が稼働済みであることを確認（§1-9）
- ⚪ 60日無活動でscheduled trigger自動無効化（L1連続failでcommitが止まり続けると複合障害になり得る）
  ・逐次swapのクロスファイル不整合・JST naiveフォーマット依存 → コード内コメント／本メモに記録
