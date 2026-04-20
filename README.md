# MLB Notify Bot

MLB 球員賽後數據自動通知機器人，透過 LINE 推送比賽結果，並支援聊天查詢。

## 功能

- **自動通知**：比賽結束後自動推送球員當場數據至 LINE
- **聊天查詢**：透過 LINE 對話查詢任意日期的比賽狀態與球員數據，由 Claude AI 解析回應

## 追蹤球員

| 球員 | 球隊 | 位置 |
|------|------|------|
| 大谷翔平 (Shohei Ohtani) | Los Angeles Dodgers | 打者 🏏 |
| James Wood | Washington Nationals | 打者 🏏 |
| Mason Miller | San Diego Padres | 投手 ⚾ |

## 通知格式

**打者**
```
🏏 大谷翔平
Los Angeles Dodgers - 2026-04-20
H: 2 | HR: 1 | RBI: 2 | BB: 1 | SO: 0 | AB: 4
賽季 AVG: .310 | SLG: .620
🎬 全壘打影片：https://...
```

**投手**
```
⚾ Mason Miller
San Diego Padres - 2026-04-20
IP: 1.0 | SO: 2 | BB: 0 | ER: 0
Season ERA: 1.23 | WHIP: 0.85
```

## 數據說明

**打者**
- H：安打 / HR：全壘打 / RBI：打點 / BB：保送 / SO：三振 / AB：打數
- AVG：打擊率 / SLG：長打率

**投手**
- IP：投球局數 / SO：三振 / BB：保送 / ER：自責失分
- ERA：自責失分率 / WHIP：被安打率

## 技術架構

- **後端**：Flask
- **通知**：LINE Messaging API（Broadcast）
- **AI 查詢**：Claude API（claude-opus-4-6），Tool Use 架構
- **數據來源**：MLB Stats API (`statsapi.mlb.com`)
- **部署**：Render（含 keepalive 防休眠）

## 環境變數

建立 `env.env` 並填入以下設定：

```
LINE_CHANNEL_ACCESS_TOKEN=
LINE_CHANNEL_SECRET=
LINE_USER_ID=
ANTHROPIC_API_KEY=
RENDER_EXTERNAL_URL=   # 部署後填入，用於 keepalive
```

## 安裝與執行

```bash
pip install -r requirements.txt
python app.py
```

## 運作方式

`notify_loop()` 在背景執行緒每 60 秒檢查一次各球隊比賽狀態，偵測到 `Final` 後抓取 boxscore 與賽季數據，組合訊息後 Broadcast 推送。每場比賽以 `球員名:日期` 為 key 記錄於 `notified_today.json`，避免重複通知。

## 推上 GitHub 注意事項

- **不要 commit `env.env`**：內含 LINE Token 與 API Key 等敏感資訊，請確認 `.gitignore` 已排除
- **不要 commit `notified_today.json`**：為本地執行狀態，每台機器應各自維護
- Render 部署會自動偵測 `main` 分支的更新並重新部署，推上去後請確認 Render Dashboard 部署成功
- 推送後 `notify_loop` 會重啟，當天已通知過的比賽紀錄（`notified_today.json`）若不存在會重新從空白開始，**可能重複發送通知**，部署時請注意時間點
