# BAT Claude／Codex 動態註冊與自動通知（v0.7）

server 啟動時只設定 BAT 連線與 token。之後每個 agent 在 `create_room`／`join_room` 時提供自己的 BAT terminal ID，server 就會建立通知綁定。另一組協作可以隨時另開房間，不需更新啟動設定或重啟 server。

本次協作依使用者指定的「完成工作後才通知對方下一個動作」流程執行，不另外加入忙碌等待機制。支援 BAT **3.2.10** 的一般 Claude Agent、Claude Agent Worktree 與一般 Codex Agent。Channel、CLI、remote profile alias 不在支援範圍內。

## 1. 啟動一次 server

在 BAT 開啟 Remote Access，綁定 localhost，核對畫面上的 SHA-256 憑證指紋。先停止舊版 mailroom server，再在**使用者自己的終端機**執行：

```sh
cd /Users/eason_tseng/playground/mcp-server/agent-mailroom
uv sync --locked --extra bat
.venv/bin/agent-mailroom serve \
  --bat-url wss://127.0.0.1:9876 \
  --bat-fingerprint 49c02f7cb746038bd728ac379345cc98d45edf0374ae4adf5ecb9f136bd29f4e
```

指紋若與 BAT 畫面不同，請填核對後的新值。程式隱藏詢問 token，只保留在程序記憶體，不讀 `BAT_REMOTE_TOKEN` 環境變數，不接受 token 命令參數或檔案。agent 不需要取得 token。

不指定房間、成員或 terminal ID。`--bat-url` 可省略，預設是上述 localhost；提供 `--bat-fingerprint` 就啟用 BAT 通知。完全不帶 BAT 參數則只提供主動收送信。若曾自訂 `--database`，請沿用相同檔案；預設為 `~/.local/share/agent-mailroom/mailroom.sqlite3`。

v0.5 已移除 `--bat-room`、`--bat-member`、`--bat-bindings` 等啟動綁定參數。v0.6 將資料庫升級至 schema 5，加入通知憑證的雜湊。v0.7 會先備份既有資料庫，再升級至 schema 6，加入被替換身分的房間範圍撤銷紀錄；原有房間與綁定會保留。使用者重新輸入 token 啟動後，server 會恢復現有綁定的 worker。離開時已刪除的綁定不會恢復。

重新載入兩端的 MCP bridge，確認有 `reconnect_member`，且 `notification_status`、`get_message`、`send_message`、`ack_message` 都有 `notification_key` 參數。已綁定成員不必重新註冊。

## 2. 第一個 agent 建立房間並綁定自己

Claude 呼叫 `create_room`：

```json
{
  "workspace": "/absolute/path/to/your/workspace",
  "member_name": "claude-worker",
  "bat": {
    "runtime": "claude",
    "profile_id": "default",
    "session_id": "REPLACE_WITH_YOUR_CLAUDE_TERMINAL_ID"
  }
}
```

以上 workspace、member_name 與 terminal ID 都是範例，並非目前環境的綁定。請填入目標對話實際的 cwd、自選成員名稱與 BAT terminal ID。terminal ID 不能使用 SDK 歷史 session ID，也不能只靠相同工作目錄猜測是哪個 agent。

server 會檢查 BAT 版本、profile、runtime 與實際 cwd，再於同一筆交易儲存房間、成員和綁定。BAT 未啟用、連不上或目標不符時，註冊會回傳錯誤，不留下部分註冊資料。

成功回傳：

- `room.room_id`：交給另一個 agent 加入。
- `session_key`：自己的長期身分參照，用於主動收信、修改綁定等一般操作。新版通知處理不依賴它。
- `bat_binding`：本次通知綁定，包含 runtime、profile、terminal ID 與 workspace。

agent 再用自己的 key 呼叫 `notification_status`，檢查 `worker_enabled: true`。新房間的註冊驗證不會向 BAT 對話發送 prompt。既有成員若有尚未通知的信件，補上綁定後就會開始通知。

## 3. 另一個 agent 加入同一個房間

Codex 呼叫 `join_room`，將 `room_id` 換成上一個步驟回傳的值：

```json
{
  "room_id": "room_替換為實際值",
  "member_name": "codex-worker",
  "workspace": "/absolute/path/to/your/workspace",
  "bat": {
    "runtime": "codex",
    "profile_id": "default",
    "session_id": "REPLACE_WITH_YOUR_CODEX_TERMINAL_ID"
  }
}
```

`profile_id` 省略時為 `default`；`runtime` 與 `bat.session_id` 必填。`workspace` 省略時，新成員沿用房間工作目錄，既有成員沿用自己的工作目錄。最外層原有的 `session_id` 仍只是可選標籤，與 `bat.session_id` 不同。

加入後取得 Codex 自己的 key 和通知綁定。兩端都綁定完成後，mailroom 收到新信就會通知對應的 BAT 對話。每個房間中的同一個 BAT terminal 只能綁定一位成員，避免把兩個不同身分送到同一段對話。不同房間可以各自註冊；每位成員的通知都帶有明確 room_id。

其他 agent 可隨時重複上述流程建立另一組協作，server 不必重啟。

## 已經註冊的成員

**沿用原本 room_id、session_key 與成員資料。** 不要省略 key 再建立新房間。呼叫 `join_room`，帶原本的 `member_name`、原有可選 `session_id` 標籤、自己的 `session_key`，再加上 `bat`，即可補上或更新自己的綁定。

兩端都必須沿用自己實際註冊的 room_id 與 member_name，不要改成範例名稱。已持有 key 時，以 `resume_session`／`notification_status` 回傳的成員與綁定資料確認目前設定；文件範例不代表資料庫中的實際綁定。

相同 key 與相同綁定重試不會建立新身分或新綁定。BAT 對話換了 terminal ID 時，以同一個 key 重新 `join_room` 並更新 `bat`；server 會停止舊綁定的 worker，啟動新綁定。省略 `bat` 不會刪除既有綁定。

## Session 重置且原本的 key 遺失

若新的 agent session 無法取得原本的 `session_key`，呼叫 `reconnect_member`，沿用原本的 `room_id` 與 `member_name`，並提供新 BAT terminal：

```json
{
  "room_id": "room_替換為實際值",
  "member_name": "claude-worker",
  "bat": {
    "runtime": "claude",
    "profile_id": "default",
    "session_id": "REPLACE_WITH_NEW_TERMINAL_ID"
  }
}
```

這條流程不需要舊 key。server 會從資料庫找到原綁定，再透過 BAT 驗證以下條件：

- 舊 terminal 已不在該 local profile 的 terminal 清單中。這是本版對「已斷線」的明確判定；只是不忙碌、停止輸出或等待輸入都不算斷線。
- 新 terminal 存在，terminal ID 與舊值不同，而且 profile、runtime、cwd 都與舊綁定一致。
- 原成員仍是 `active` 或 `paused`，且仍有 BAT 綁定。已 `left` 的成員不能以此方式恢復。

成功後會保留成員名稱、加入時間、協作狀態、歷史信件、回覆關係及未確認處理的收件匣，並回傳新的 `session_key` 與 `bat_binding`。舊 key 會在該房間遭撤銷，舊通知憑證也會失效。原本已送往斷線 terminal、但尚未確認處理的通知會清除送出紀錄，由新綁定重新通知，避免新 session 永遠收不到待辦。

第一次呼叫若結果不明，請使用錯誤訊息附帶的新 `session_key` 與完全相同參數重試。成功後重試同樣具冪等性。若舊 terminal 仍在 BAT 清單中，server 回傳 `409`，不會修改成員或綁定；請先確認確實是 session 重置，而非同一 session 暫時閒置。

如果只是 runtime 或 MCP 重啟、BAT terminal ID 沒變，而且 agent 是被新通知喚醒，直接使用通知內的 `notification_key` 即可，不需要接管身分。

## 收到通知與交接：不依賴原本的 key

每則新版通知包含 `room_id`、`message_id` 與隨機的 `notification_key`。直接把通知中的 `notification_key` 傳給下列工具，省略 `session_key`：

1. `notification_status`：確認仍為 active，否則停止。
2. `get_message`：讀通知指定的 message_id。已確認處理就停止，不重做工作。
3. 完成工作後，有必要才 `send_message` 回覆原寄件者，`reply_to` 指向通知的 message_id，`request_id` 固定用 `notice-reply-<message_id>`。
4. 有必要的回覆送出後，再 `ack_message`。不需要回覆則完成處理後直接確認。

例如通知的 message_id 是 11，讀信參數為：

```json
{
  "room_id": "room_REPLACE_WITH_ACTUAL_ID",
  "message_id": 11,
  "notification_key": "notice_REPLACE_WITH_VALUE_FROM_THIS_NOTIFICATION"
}
```

server 直接驗證通知憑證，取得該封信的接收身分；bridge 不搜尋憑證檔、不換回長期 token、不建立新身分。即使 Codex code-mode 暫存消失、MCP 重啟，或新 bridge 的憑證目錄為空，都能從本次通知直接處理信件。Claude 同樣使用這條路徑。

### 權限與重試

- 憑證綁定房間、接收成員、BAT 綁定及單封信。只能查自己的通知狀態、讀取並確認該信，以及回覆原寄件者一次。
- 不允許查整個收件匣、讀其他信、列出成員、修改協作狀態、變更綁定或建立／加入房間。這些操作仍使用自己的 `session_key`。
- 回覆與確認可安全重試。回覆重試要使用相同 request_id 與內容，不可另換 request_id 再寄一次。
- 確認處理後只能讀取該信、重複確認，或重試既有回覆；不能新增回覆。兩個程序同時回覆也只會新增一封。
- 暫停時只允許查狀態，其他操作停止；離開或更換綁定後，舊通知憑證失效。恢復原本的 paused 綁定後，未處理的通知可繼續。

有效期依信件及綁定狀態控制，不設短時間兌換期限，以免 BAT 排隊或長任務讓通知在開始處理前失效。憑證可在同一封信的讀取、回覆與確認過程中重用；不需要一次性兌換，也不會把原本的長期 key 交回模型。

server 只保存通知憑證的 SHA-256 雜湊；原值僅放進對應的 BAT 通知，不出現在成員清單、通知狀態或同儕信件。它本身是範圍受限的 bearer 憑證，持有人可執行上述操作，所以不要把它轉寄給同儕。這仍是同一位使用者的本機信任環境，不宣稱隔離同一使用者下的其他程序。

通知不含信件本文、長期身分 key 或 BAT token。若工具缺少 notification_key 參數，需重新載入 MCP；若綁定已撤銷，依停止協作處理。收到同儕來信不代表新增操作權限。

### 升級前已送出的通知

v0.5 以前的通知沒有 notification_key。升級不會自動重送 accepted／unknown 的舊通知，也不會重新執行舊工作；這類未完成信件仍須使用原本身分完成收信與確認。保護跨 runtime 重啟的是升級後送出的新通知。

## 暫停、恢復、離開

用自己的 key 呼叫 `set_collaboration_state`：

| state | 新信 | 新通知 | 綁定 |
| --- | --- | --- | --- |
| active | 接受 | worker 可送出 | 保留 |
| paused | 接受並保留 | 停止 | 保留 |
| left | 拒絕，回傳 409 | 停止 | 刪除並停止該 worker |

`paused` 期間重新綁定不會自動恢復 active。離開後先設回 `active`，再用同一 key 呼叫 `join_room` 並提供 `bat`，即可恢復綁定，不需重啟 server。

暫停／離開與最後的 socket 送出共用鎖。**已送進 BAT 的通知可能仍在排隊，mailroom 不使用 Esc 或 abort 撤回。** 接收端仍應先檢查協作狀態。

## 通知狀態與恢復

`notification_status` 只回傳自己的狀態、綁定、`worker_enabled` 與 `pending_notification`。`worker_enabled` 表示本程序的 worker 已啟用，不保證 BAT 在線或模型已處理信件。`last_error` 記錄最近一次送出前檢查或通知問題。

worker 每兩秒檢查信箱。同一成員最多有一則尚未確認處理的通知；後面的信等待最舊信件被確認。

| 通知狀態 | 意義 |
| --- | --- |
| 尚無紀錄 | 未嘗試送出；可能尚無新信、暫停、BAT 不可用、resting、等待使用者或目標不符 |
| submitting | 已保存送出意圖，正在送出或等待 BAT 回應 |
| accepted | Claude 回傳 `ok: true, accepted: true`，或 Codex 回傳 `ok: true`；仍需處理並確認信件 |
| unknown | 送出逾時、不明回應或 submitting 時重啟；不自動重送 |
| not_accepted | BAT 回傳 `ok: false`；不自動重送 |

`pending_notification: null` 不代表信箱一定為空。讀信與 BAT 接受通知都不會自動 `ack_message`。

送出前連線失敗可再檢查；一旦保存送出意圖，就不因一般重啟、恢復協作或持有舊 key 的換綁定而自動重送同一封信。唯一例外是 `reconnect_member` 已確認舊 terminal 不存在：未完成通知會撤銷並交給新 terminal。`invoke-error` 也可能來自 sidecar 等待逾時，歸入 unknown。若接收端實際已收到 notification_key，仍可用它處理及確認該信，不需重送通知。Codex 路由不使用 `clientMessageId`，防止自動重送由 mailroom 的持久化紀錄負責。

恢復時先確認 BAT 狀況，再主動 `receive_messages`／`get_message`，完成工作後才 `ack_message`。不要為了清除阻塞而確認未處理的信件。

## 驗證範圍與實際驗收

使用者已在 BAT 3.2.10 完成手動 Claude PoC：閒置送訊成功；忙碌時先完成原工作，再處理排隊訊息。Esc 測試依使用者決定跳過。

自動測試使用 HTTP、stdio MCP 與 TLS WebSocket 模擬 BAT，涵蓋動態多房間註冊、目標驗證、身分隔離、交易回滾、重綁、離開、重啟恢復與通知。另以全新 stdio 程序與空白憑證目錄，驗證兩端只憑通知完成讀信、回覆及確認，並驗證 server 重啟及回覆／確認重試。不執行真實模型。新版實際驗收：

1. 啟動新版 server 並重新載入 MCP；已有綁定者沿用，確認各自 `worker_enabled: true`。
2. 工作完成後寄一封要求指定回覆的信，確認另一端自動讀信、處理、確認及回覆。
3. 確認原寄件端也自動收到回覆，完成處理確認。
4. 在已完成工作的情況下重啟接收端 runtime，再送新信。確認它使用本次通知的 notification_key，直接讀信、回覆及確認，沒有詢問原本的 key 或搜尋憑證檔。
5. 建立一封尚未確認處理的測試信，關閉舊 BAT terminal，建立同 runtime、profile、cwd 的新 terminal；在新 session 呼叫 `reconnect_member`，確認取得新 key、收到原待辦，而舊 key 無法再恢復或加入該房間。

這是單機、單一使用者信任環境。BAT target 驗證確認指定 terminal 的 runtime 與 cwd，不是證明 agent 程序身分；請依使用者指示填自己的 terminal ID。token 不交給 agent，server 不修改 BAT 權限或核准工具。

BAT 3.2.10 的 Codex 在仍有 active turn 時收到訊息會取代該回合。本版依完成後交接的操作前提使用，不修改 BAT 的排隊行為。停止協作可暫停／離開；停止整個通知服務則停止 server 並關閉 BAT Remote Access。
