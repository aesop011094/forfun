# Football Intelligence AI

## Windows 即用版下載

[下載 Windows 10／11 64 位元即用版 ZIP（約 92 MB）](https://github.com/aesop011094/forfun/raw/refs/heads/main/downloads/Football-Intelligence-AI-Windows.zip)

1. 下載後，按滑鼠右鍵選擇「全部解壓縮」。
2. 打開解壓後的資料夾，雙擊 **Start.cmd**。
3. 瀏覽器會自動開啟；不用安裝 Python、Node.js 或註冊 Replit。

程式在自己的電腦運行，已有資料可離線查看；更新數據需要上網。關閉時按啟動視窗的「停止並離開」。

此下載包保留網站、模型、資料及授權文件；省去無需使用的開發工具與套件測試檔。已通過 Windows 相容環境的啟動、資料保存及關閉測試；完整模型運算仍未在實體 Windows 電腦全面驗證。來源資料可能滯後，請查看網站的數據日期及缺口提示。

繁體中文足球概率研究網站。FastAPI、React/TypeScript、SQLite，以及在自己取得的歷史賽果上訓練的模型。預測及回測均由本程式計算，沒有使用第三方預測結果，也沒有固定的示範準確率。

## 使用者須知

- 預設使用 OpenFootball 公開 JSON 數據（CC0），毋須帳戶、API Key 或付費。
- 這是社群整理的賽程及賽果，不等同官方即時數據。網站同時顯示下載時間和最近賽果日期；資料可能滯後。
- 模型「預期入球」是估計值，並非射門數據計算的實測 xG。沒有取得 xG、傷停或正選名單時，畫面會顯示數據不足。
- 歷史回測是按已知賽果重建的研究；它不能證明當時原始來源何時公開。真正開賽前儲存的預測另外保留，不會與回測混為一談。
- 概率不是保證，也不是投注建議。未經批准，程式不會購買或啟用付費服務。

## 已準備的開發方式

在同一個服務提供網站及 API，不需要另外維護前後端伺服器。SQLite 適合單一實例的 MVP，必須保留 `data/`。正式多實例服務應先遷移 PostgreSQL 及共用排程／工作佇列。

維護人員可使用：

```sh
bash scripts/setup.sh
.venv/bin/python -m backend.service bootstrap
bash scripts/run.sh
```

這些是維護文件，使用者毋須自行操作終端機。`bootstrap` 會下載真實來源、執行模型比較、儲存研究結果及預測；需要網絡及數分鐘計算時間。網站管理頁也提供同步、訓練及預測按鈕。

測試：

```sh
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
node --experimental-strip-types --test web/tests/probabilities.test.mjs
.venv/bin/python scripts/browser_smoke.py
```

## API Key 與管理保護

API Key 只由後端讀取 `FOOTBALL_DATA_API_KEY`，不會送到前端。不要將 Key 寫入 Git、聊天室或網頁網址。

公開部署時，必須在平台 Secrets 設定隨機且足夠長的 `ADMIN_TOKEN`。管理者在網站設定頁輸入管理憑證，僅留在當前瀏覽器記憶體；重新整理後需再輸入。沒有管理憑證時，修改資料的功能只准本機使用。讀取分析頁不需要管理憑證。

football-data.org 接入是可選功能，只有 `FOOTBALL_DATA_API_KEY` 存在及 `FOOTBALL_DATA_TERMS_ACCEPTED=true` 才啟用。其使用條款及帳戶限制需按實際帳戶核實，預設不會啟用。詳見 [數據來源與授權](DATA_SOURCES.md)。

## Replit 或其他平台發佈

已提供 `.replit`、`Dockerfile` 與可重複安裝／啟動腳本。這些是部署設定，並不表示網站已對外發佈。

1. 使用者到 https://replit.com/ 選 Sign up，以 Google 或電郵註冊／登入；毋須提供密碼給開發助手。使用完整專案 ZIP 上傳檔案，或在原始碼已放到 GitHub 後匯入該儲存庫。可由此儲存庫匯入原始碼；Windows 下載版另見上方連結。
2. 在 Secrets 設定 `ADMIN_TOKEN`；不需要為 OpenFootball 設定 API Key。
3. 啟動專案後，先確認網站「數據狀態」及「模型表現」頁有真實資料；管理頁可重新同步與訓練。
4. 如要公開發佈，先檢查 Replit 顯示的費用。任何付費部署方案必須先獲使用者批准。
5. 必須選擇有持久磁碟、可運行長駐服務的單一實例，保存 `data/`。只有暫存磁碟的部署會失去歷史預測；純靜態網站不能運行 Python 模型。
6. 發佈後另行驗證公開網址、HTTPS、手機版、管理保護及磁碟持久性。此環境的本機驗證不能代替正式部署驗證。

正式營運仍需備份 `data/football.db`、監察來源更新／工作錯誤，並核實所選主機的持久磁碟及排程支援。請勿啟動多個同時訓練／排程的服務副本。
