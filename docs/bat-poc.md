# BAT Claude 連線 PoC

這個腳本用來驗證外部程式能否把訊息送進**已開啟的一般 Claude Agent**。它尚未接上 mailroom 的收件匣，不會持續監聽或自動喚醒成員。

v0.3 的背景通知改由 [BAT Claude 自動通知](bat-notifications.md) 提供；本腳本保留作為獨立診斷工具。

## 相容範圍與已知限制

- 依據 BAT `v3.2.10` tag，commit `478c3ad0cf3f53c3da82e7df7179ad070003e635`；連線時要求 server 回報 `3.2.10`、`bat-remote/v2` 與 `profileContext: 1`。其他版本會停止。
- 僅接受本機型態的 profile，以及 `claude-code`、`claude-code-worktree` preset；不接受 remote profile alias、Claude Channel、CLI 或 Codex。
- `claude:list-sessions` 列歷史 `sdkSessionId`。本腳本改讀指定 profile 的 `workspace:load`，以 terminal 的 `id` 作為 BAT `sessionId`，再核對即時 state、meta 與 cwd。工作區清單可能保留已關閉的對話，因此清單存在不等於可以送出。
- Claude 的 `sendQueue` 會將新訊息排到正在執行的回合後面。回傳 `accepted` 只表示已交給 SDK；`queued` 是 BAT 送入時的判斷。忙碌時 RPC 可能等待較久，server 最多等待 300 秒，腳本等待 330 秒。
- 送出逾時或斷線時，結果視為未知，**不自動重送、不取消 BAT 工作**。先在 BAT 查看是否已收到或仍在排隊。`clientMessageId` 會印在送出前的紀錄中；BAT 的去重紀錄屬於執行中的 session，不能視為跨重啟的永久保證。
- 等待使用者回答／權限確認，或被設為 resting 的對話會拒絕送入。這是 PoC 的保守選擇；請先在 BAT 處理。
- profile、preset、state 的檢查與送出仍是分開的操作。測試時請勿關閉、替換或切換目標 runtime；本腳本沒有提供 BAT 端的原子綁定保證。

來源為 BAT tag 內的 `src-tauri/src/{remote_server,remote_core,profile_context}.rs`、`src-tauri/src/commands/claude.rs`、`node-sidecar/src/handlers/claude-{session,send}.mjs` 與 `renderer/src/types/agent-presets.ts`。Python 連線介面依據 [websockets 同步 client 文件](https://websockets.readthedocs.io/en/stable/reference/sync/client.html)。

## 連線與查詢

在 repo 根目錄執行：

```sh
uv sync --extra bat
uv run --extra bat python scripts/bat_poc.py --help
```

先在 BAT Remote Access 查看服務狀態、連線位址及 SHA-256 fingerprint。腳本不會啟動服務或修改 BAT 設定。以下的 `HOST:PORT`、`FINGERPRINT`、`PROFILE_ID` 與 `BAT_SESSION_ID` 都要換成實際值。

```sh
uv run --extra bat python scripts/bat_poc.py \
  --url wss://HOST:PORT --fingerprint FINGERPRINT profiles

uv run --extra bat python scripts/bat_poc.py \
  --url wss://HOST:PORT --fingerprint FINGERPRINT --profile PROFILE_ID sessions

uv run --extra bat python scripts/bat_poc.py \
  --url wss://HOST:PORT --fingerprint FINGERPRINT --profile PROFILE_ID \
  status --session-id BAT_SESSION_ID
```

每次執行會以隱藏輸入方式詢問 token，也可以從既有的 `BAT_REMOTE_TOKEN` 環境變數讀取。請勿把 token 寫進命令參數、版本控制或對話。腳本會先在同一條 TLS 連線比對憑證指紋，再送 token；沒有略過指紋檢查的模式。Remote Access token 能操作 BAT，應視為私密憑證。

`profiles`、`sessions`、`status` 不送 prompt。連線本身仍可能讓 BAT 顯示 Remote Client 已連入的通知。清單只輸出選定欄位；不輸出完整 profile 設定或歷史對話。

## 指定測試對話後送出

建立一份 UTF-8 訊息檔，例如 `/tmp/mailroom-poc-message.txt`，內容用可辨識的唯一字串：

```text
這是 mailroom PoC 測試。請只回覆 MAILROOM-POC-20260915-A，不使用工具或修改檔案。
```

確認目標是專用測試對話，再執行：

```sh
uv run --extra bat python scripts/bat_poc.py \
  --url wss://HOST:PORT --fingerprint FINGERPRINT --profile PROFILE_ID \
  send --session-id BAT_SESSION_ID \
  --message-file /tmp/mailroom-poc-message.txt --observe-seconds 60
```

腳本會輸出 JSON Lines：送出前的目標與 `isStreaming`、`clientMessageId`、BAT 的 `send_result`，以及該 context／session 的訊息、結果與回合結束事件。事件可能在 RPC 回應之前抵達，也可能屬於原本正在執行的回合；不會自動判定每個事件都由本次訊息觸發。輸出可能含目標對話內容，保存紀錄時請自行控制存取權限。

觀察時間結束會顯示 `completion: not_automatically_verified`；退出碼 0 代表腳本流程成功結束，**不代表 agent 已完成回覆**。請對照唯一字串、BAT 畫面及事件順序確認。

## 真實 BAT 驗收

1. **閒置收訊：** 在一般 Claude 測試對話閒置時送出唯一字串，確認不需手動輸入就開始處理並回覆同一字串。
2. **忙碌排隊：** 讓同一測試對話先執行一個受控、耗時的工作，再送另一個唯一字串。確認 `queued: true`、原工作持續且完成後才處理新訊息。`before_send.isStreaming` 是快照，不能單獨證明有排隊。
3. **排隊取消：** 若要驗證 Esc 行為，在專用測試對話中取消排隊工作，觀察 `cancelled`／錯誤與 BAT 畫面，不要直接重送。

本機自動測試只用模擬 BAT 協定的 TLS WebSocket server；不執行模型，不能證明真實 BAT 的排隊、喚醒、回覆或 Esc 行為。待指定真實測試對話及取得連線設定後，才執行以上驗收。

## 下一階段

PoC 通過後，才把 mailroom 成員綁定到 BAT host／profile／terminal ID，並實作收件通知與暫停／離開狀態。正式 worker 需處理重啟恢復、未確認投遞、重複通知，以及停止協作後不再送出；本腳本沒有這些能力。Codex 仍維持主動收信，直到 BAT 提供不中斷現有回合的送入方式。
