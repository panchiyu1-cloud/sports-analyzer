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
