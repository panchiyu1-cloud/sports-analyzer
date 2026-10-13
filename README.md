# sports-analyzer

每天自動執行的比賽分析器：抓賽程與國際賠率 → 跑模型算勝率 → 和市場去水機率比較 → 產出分析卡片與訊號 → 發布成網頁。

- **足球**：英超、西甲、德甲、義甲、法甲、歐冠。模型為 Dixon-Coles（泊松進球 + 低比分修正 + 時間衰減）。
- **棒球（MLB）**：Phase 3 建置中，目前只抓賽程與先發投手。
- **市場**：The Odds API 的國際莊家賠率（去水後取中位數）。這**不是**台灣運彩盤口。
- **帳本**：每個訊號寫進 `data/ledger.csv`，比賽結束後自動結算，網頁上顯示命中率與損益。

全部跑在 GitHub Actions（免費），網頁由 GitHub Pages 提供，不需要自己的電腦開機。

## 第一次設定（只做一次）

1. **放金鑰**：repo 頁面 → Settings → Secrets and variables → Actions → New repository secret，建立兩個：
   - `FOOTBALL_DATA_TOKEN`（football-data.org 寄來的 token）
   - `ODDS_API_KEY`（the-odds-api.com 寄來的 key）
2. **開啟 GitHub Pages**：Settings → Pages → Build and deployment → Source 選 **Deploy from a branch** → Branch 選 `main`、資料夾選 `/docs` → Save。幾分鐘後網址會顯示在同一頁。
3. **先跑一次歷史資料與回測**：Actions → 「History + backtest」→ Run workflow。這會下載近三季的歷史比分和收盤賠率到 `data/history/`，模型需要它們當暖身資料，並產出 `docs/backtest.html`。
4. **手動跑一次每日分析**：Actions → 「Daily analysis」→ Run workflow。成功後 `docs/index.html` 會更新，網頁就有內容了。

之後每天台北時間 08:15 自動跑足球；每週一 11:00 更新歷史資料與回測。

## 看結果

- 分析頁：`https://<你的帳號>.github.io/sports-analyzer/`
- 回測報告：同網址加 `backtest.html`
- 訊號帳本：`data/ledger.csv`（可用 Excel 或 Google Sheets 開）
- 每日原始快照：`data/snapshots/<日期>/football.json`

## 讀卡片的方法

| 欄位 | 意思 |
|---|---|
| 主勝／和局／客勝 條狀圖 | 綠色 = 模型機率；橘色 = 市場去水後的機率。兩者差距就是 edge。 |
| 預期進球 | 模型估的雙方進球期望值（λ、μ）。 |
| 攻／守排名 | 該隊在本聯賽模型裡的攻擊力、防守力排名。 |
| 模型偏向 | 只有 edge ≥ 10 個百分點且模型機率 ≥ 25% 才出現。分歧程度：低（10–15pp）、中（15–20pp）、高（≥20pp）。資料不足的球隊一律降為「低」。 |
| 1/4 Kelly | 若要照凱利公式下注，建議的資金比例的四分之一。參考用。 |

沒有訊號的日子是正常的——市場定價合理時，不動就是正確答案。

## 回測結果（2026-10-13，請先讀這段）

五大聯賽 2024-25 至本季、2,753 場有賽前與收盤賠率的比賽，兩階段逐週走勢（模型和混合權重都只用「當週之前」的資料）：

| | log-loss（越低越好） |
|---|---|
| 純 Dixon-Coles 模型 | 1.0046 |
| 賽前市場（Pinnacle 週五）| 0.9732 |
| **收盤市場** | **0.9713** |
| 混合（模型 + 市場）| 0.9730 |

- **Phase 1（純模型 edge ≥ 4pp 平注）**：ROI −7.8%，CLV −4.7%。
- **Phase 2（市場為基準、模型微調）**：混合權重學到純模型的權重是**負的**（a ≈ −0.19），也就是模型偏離市場的方向平均是錯的；混合的 log-loss 只等於賽前市場，達不到收盤市場。
- 混合 edge ≥ 3pp 平注 982 注 ROI +4.1%（±4.3%），看起來不錯，但 **CLV −3.4%**：收盤市場說這些注每注期望虧 3.4%，正 ROI 是運氣。所有門檻、所有聯賽的 CLV 都是負的；大小 2.5 也一樣。
- 模型偏差和盤口走向的相關係數接近 0：模型看不到市場稍後才納入的資訊（傷兵、陣容、資金）。

**結論**：用公開比分資料做的模型，打不贏國際盤口。`signals_validated` 維持 `False`，「模型分歧」只是資訊，不是建議。

**這個系統真正有用的一個數字是「公允賠率」**（1 ÷ 市場去水機率，顯示在每張卡片上）。它是全世界最準的價格；你在運彩看到的賠率如果**高於**公允賠率，那張注才有正期望值；低於就是在付水錢。這是業餘者唯一經得起驗證的找價值方法。完整數字在 `docs/backtest.html`。

## 運彩比價（Phase 4）

台灣運彩的賠率資料流只有台灣 IP 連得到（GitHub 的伺服器會被 CloudFront 以國別擋下），所以由 iPhone 的「捷徑」App 每天 08:00 與 20:00 自動抓兩個檔案、上傳到 `data/tw/football/` 與 `data/tw/baseball/`（檔名 `YYYY-MM-DD-HHmm.json`）。設定步驟在另一份文件「運彩賠率自動上傳設定」。

上傳一到 GitHub，`tw.yml` 就會：

1. 解析運彩的格式（`fetch/taiwan.py`：不讓分／讓分／大小；賠率 = 1 + pu ÷ pd）
2. 用開賽時間 + 中文隊名對照表把每場對上我們的卡片
3. 算 **EV = 市場公允機率 × 運彩賠率 − 1**，EV ≥ 2% 的標成「價值注」寫進 `data/tw_ledger.csv`
4. 晚上 20:25 的 `evening.yml` 再抓一次國際獨贏盤（只花 1 點／聯賽），把早上價值注的 **CLV** 填上；比賽結束後自動結算

網頁最上方的「運彩價值注」就是這個結果。多數日子會是空的——運彩的水錢比國際盤高，賠率高於公允價的注本來就少；有就是有，沒有就不要下。

對照不到的隊名會列在快照的 `tw_report.unknown_teams`，補進 `fetch/taiwan.py` 的 `TW_ALIASES` 即可。

## 本機執行（選用）

```bash
pip install -r requirements-dev.txt
cp .env.example .env            # 填入兩把金鑰；.env 已在 .gitignore 裡
python -m unittest discover -s tests -v
python -m sports_analyzer.pipeline football          # 抓資料 + 建模 + 產頁面
python -m sports_analyzer.pipeline render            # 只用上次快照重新產頁面
python scripts/backtest_football.py data/history/*.csv
```

## 專案結構

```
src/sports_analyzer/
  config.py        聯賽清單、模型參數、環境設定
  http.py          共用 HTTP 連線：重試、依回應標頭自動節流
  teams.py         三個資料來源的隊名對照
  fetch/           football.py（football-data.org）、odds.py（The Odds API）、mlb.py
  models/          dixon_coles.py（足球模型）、market.py（去水、edge、T-10 G 值）
  analysis/        football.py — 賽程 + 模型 + 賠率 → 卡片與訊號
  ledger.py        訊號帳本與結算
  render/          HTML 模板
  pipeline.py      每日流程進入點
scripts/backtest_football.py   逐週滾動回測
tests/             48 個測試，不需要網路
.github/workflows/ daily.yml（每日）、backtest.yml（每週）、ci.yml（每次 push 跑測試）
```

## 額度與限制

- football-data.org 免費層：每分鐘 10 次、比分有延遲、沒有陣容名單。程式會讀回應標頭自動等待。
- The Odds API 免費層：每月 500 點，一次呼叫 = 市場數 × 地區數（預設 3 點）。只對視窗內有比賽的聯賽呼叫，每月大約用 200–300 點。
- 每日快照是「早上 08:15 的賠率」，不是開賽前的尾盤。T-10 要求的賽前 10 分鐘 G 值需要付費方案或手動查詢。
- 模型只用比分，不知道傷兵、輪換、天氣。遇到這類資訊請自行調整。

## 接下來（Roadmap）

- ~~Phase 2：市場基準 + 模型修正~~ 做完，未通過（見上）。混合模型保留在 `models/blend.py`，回測每週自動重跑；哪天 CLV 轉正、`passed` 變 true 再談訊號。
- Phase 3：MLB 模型（log5 + 先發投手 + 牛棚）。市場公允賠率與 T-10 G 值已先上線（`analysis/mlb.py`）。棒球模型同樣要過 CLV 這關。
- ~~Phase 4：運彩比價~~ 已上線（見上）。接下來看三個月的運彩帳本：平均 CLV 為正才算真的有價值。

## 授權

MIT
