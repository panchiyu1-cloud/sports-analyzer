/**
 * 臺股強勢掃描：即時報價轉播站（Google Apps Script）
 *
 * 雲端程式（GitHub）每 10 秒把最新報價送到這裡，雲端版網頁每 10 秒來讀一次。
 * 網頁也會把「持倉、正在看的股票」送過來，雲端程式下次送報價時帶回去，優先更新這幾檔。
 *
 * 部署：右上角「部署」→「新增部署作業」→ 類型選「網頁應用程式」
 *       執行身分：我；誰可以存取：所有人 → 部署 → 複製「網頁應用程式網址」。
 * 這個網址等同密碼：只貼到 GitHub 的 secret（STOCK_RELAY_URL）和網頁的設定欄，不要公開。
 */
function doPost(e) {
  var cache = CacheService.getScriptCache();
  var reply = { ok: true };
  try {
    var body = (e && e.postData && e.postData.contents) || '';
    if (body && body.length < 95000) {
      var data = JSON.parse(body);
      if (data.watch) {
        // 網頁：請雲端優先更新這些股票（最多 10 檔，保留 1 小時）
        cache.put('watch', JSON.stringify(data.watch.slice(0, 10)), 3600);
      } else if (data.q) {
        // 雲端程式：最新報價（保留 6 小時）
        cache.put('ticks', body, 21600);
        reply.watch = JSON.parse(cache.get('watch') || '[]');
      }
    }
  } catch (err) {
    reply = { ok: false };
  }
  return ContentService.createTextOutput(JSON.stringify(reply)).setMimeType(ContentService.MimeType.JSON);
}

function doGet() {
  var body = CacheService.getScriptCache().get('ticks') || '{}';
  return ContentService.createTextOutput(body).setMimeType(ContentService.MimeType.JSON);
}
