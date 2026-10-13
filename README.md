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

五大聯賽 2024-25 至本季、3,179 場有收盤賠率的比賽，逐週滾動重新擬合：

- 純 Dixon-Coles 的 log-loss 在五個聯賽都**輸給收盤盤口**（差 +0.04 到 +0.08）。
- 對所有 edge ≥ 4pp 的比賽平注，1X2 合計 **ROI −9.9%**（2,020 注），大小 2.5 **−6.4%**；而且 edge 越大虧越多。

結論：目前的訊號是「模型和市場看法不同」，**不是下注建議**。頁面和程式設定（`signals_validated = False`）都這樣標示。Phase 2 要做的是以市場機率為基準、模型只做修正（shrinkage），再回測；通過之前請把這個系統當資訊看板用。完整數字在 `docs/backtest.html`。

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

- Phase 2：市場基準 + 模型修正（shrinkage）的新版機率，走勢回測看 ROI 能否回到 0 以上；通過才把 `signals_validated` 打開。
- Phase 3：MLB 模型（log5 + 先發投手 + 牛棚）、T-10 G 值自動計算、Sporta.AI 風格的棒球卡片。
- Phase 4：帳本累積三個月後檢視命中率與 ROI；決定要不要升級賠率方案拿尾盤資料。

## 授權

MIT
