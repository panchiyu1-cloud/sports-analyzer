# 臺股強勢掃描（雲端版）

這個資料夾是使用者自己的「臺股強勢掃描」程式。它和運動分析是不同的專案，只是借用這個存放庫的
GitHub Actions 與 GitHub Pages 在雲端執行，所以不用開電腦，手機也能看。

- 網頁：https://panchiyu1-cloud.github.io/sports-analyzer/stock/
  - 網頁檔案放在 `docs/stock/`。
- 資料：由 `.github/workflows/stock.yml` 產生。
  - 平日盤中每分鐘更新一次，推到 `stock-data` 分支。
  - 網頁直接讀這個分支上的 JSON 檔。
- 程式：
  - `twscan/` 和電腦版完全相同。
  - `cloud.py` 是雲端執行與輸出資料的部分。
  - `config.json` 是設定（手續費折扣等）。
- 手機推播：設定存放庫的 `STOCK_NTFY_TOPIC` secret 後才會推播。
  - 這個 secret 和運動分析的 `NTFY_TOPIC` 分開。
  - 主題名稱等同密碼，不會寫進任何檔案或紀錄。
- 我的持倉與交易紀錄：只存在手機瀏覽器裡（localStorage），雲端主機看不到。
- 10 秒報價（選用）：
  - GitHub 一分鐘只能更新一次，所以另外用使用者自己的 Google Apps Script 轉播站（`relay/Code.gs`）。
  - 雲端每 10 秒送報價過去，網頁每 10 秒去讀。
  - 轉播站網址要設成 GitHub 的 `STOCK_RELAY_URL` secret，也要在網頁「模型與資料 → 10 秒報價」貼一次。
  - 網址等同密碼。

## 開盤前試撮（08:30–09:00）

證交所 08:30 開始接受委託，每 5 秒揭示一次模擬撮合的結果（試算成交價、張數、最佳五檔），09:00 才真的開盤。

- `twscan/preopen.py` 把每檔這 30 分鐘的試撮價量記下來，換算成預估的開盤漲跌，並判讀：
  小幅開高可留意、開太高勿追、開低先觀望、量太少不準、價格忽高忽低。
- 網頁「強勢前十」最上面在平日 08:25–09:40 顯示：昨天收盤後選出的前 10 名今天預估怎麼開，
  以及模型分數較高、試撮轉強的股票。開盤後多一欄「實際開盤」，並算出試撮和實際開盤差多少。
- 每天 09:10 之後存一份對照（`data/live/preopen/日期.json`），之後可以回頭檢查判讀規則準不準。
- 判讀規則還沒有用歷史資料驗證。試撮不是成交，開盤前可以撤單。
- `preopen.json` 裡的 `samples` 是幾檔股票的證交所原始欄位，用來核對 `pz`（試撮價）、`ps`（試撮張數）
  這些欄位在試撮時段的意思。

### 怎麼確保 08:30 以前就在執行

GitHub 的排程常常晚幾個小時才啟動，不能靠它。使用者的手機每天 08:00 會上傳運彩資料（`data/tw/`），
上傳會立刻啟動工作，所以 `stock.yml` 也跟著這個上傳啟動：平日早上才放行，等到 08:18 開始，
一路跑到 14:10。排程仍然保留，當作哪天沒有上傳時的備援。

## 更新程式

電腦版改好以後，依下列方式複製過來：
- `twscan/`、`cloud.py` 複製到 `stock/`。
- `web/` 的 `app.js`、`daytrade.js`、`kchart.js`、`style.css`、`cloud.js` 複製到 `docs/stock/`。
  - `docs/stock/index.html` 是雲端版專用的，不要用電腦版的覆蓋。

## 本機測試

```bash
python stock/cloud.py --demo-live --minutes 2 --publish-dir /tmp/pub --no-push
```

這會用模擬資料寫出 JSON。網頁加上 `?data=` 參數，就可以讀本機的資料夾。

本工具僅供研究參考，不構成投資建議。
