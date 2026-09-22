# agent-mailroom

讓 BAT（Better Agent Terminal）、Claude Code、Codex 或其他支援 MCP 的 agent，在同一台電腦透過房間與收件匣交換訊息。

**先開 agent，再用工具註冊。** 第一個 agent 提供工作區資訊建立房間，server 回傳房間 ID；另一個 agent 使用這個 ID 加入。每位成員取得自己的身分，無須預先設定個別身分檔。

預設由 agent 主動收信。v0.7 可選擇啟用 [BAT Claude／Codex 自動通知](docs/bat-notifications.md)：server 啟動只需 BAT 連線資訊；agent 在建立／加入房間時帶自己的 BAT terminal ID 動態綁定。支援多組協作，新增房間或成員不需重啟，收到新信後讓對應對話開始收信。協作採「完成工作後才交接」流程，不另外加入忙碌等待機制。每則通知自帶只供處理該封信的 `notification_key`，跨 runtime／MCP 重啟不必記住原本的 key。若 session 重置後連長期 key 也遺失，可在舊 BAT terminal 已斷線後，以新 terminal 接續原成員與收件匣。通知被接受與信件處理完成分別記錄。

另保留 [BAT Claude 連線 PoC](docs/bat-poc.md)，供手動診斷外部送訊與事件。自動通知 worker 與它共用 BAT 3.2.10 的連線程式。

## 啟動 server

需要 Python 3.12 以上與 [uv](https://docs.astral.sh/uv/)。在 repo 根目錄執行：

```sh
uv sync --locked
uv run agent-mailroom serve
```

服務預設監聽 `127.0.0.1:8765`，資料庫位於 `~/.local/share/agent-mailroom/mailroom.sqlite3`。

```sh
curl --fail http://127.0.0.1:8765/health
```

預期回傳 `{"status":"ok"}`。保持服務執行；使用 `Ctrl+C` 停止。可指定其他 port 或資料庫：

```sh
uv run agent-mailroom serve --port 8766 --database .agent-mailroom/mailroom.sqlite3
```

若變更 port，MCP 設定的 `--url` 也要調整。服務不需要模型 API key。

## 先讓 agent 載入 MCP 工具

MCP host 啟動 bridge 的指令現在只需要：

```sh
/absolute/path/to/agent-mailroom/.venv/bin/agent-mailroom mcp --url http://127.0.0.1:8765
```

多個 agent 可以共用這份設定。每次註冊都會分配獨立身分，一般工具呼叫明確指定自己的 `session_key`，通知處理則使用該通知的 `notification_key`，不依賴「目前 agent」或 MCP 連線數量來判斷身分。

### BAT 的 Claude agent

將 [examples/claude.mcp.json](examples/claude.mcp.json) 中的 `agent-mailroom` 設定合併到 **BAT 工作區根目錄的 `.mcp.json`**，替換執行檔的絕對路徑，並保留原本其他 MCP 設定。

BAT 的 Claude 路徑會讀取專案 MCP 設定，且需要該 MCP 已獲核准。若只要啟用本服務，可將 `agent-mailroom` 加到專案 `.claude/settings.local.json` 的 `enabledMcpjsonServers` 清單，保留既有項目；由使用者決定是否核准。

設定完成後，讓 BAT 重新建立或載入該 agent 的 MCP 連線。已在執行中的 bridge 不會因 server 重啟而自動更新工具 schema。

### BAT 的 Codex agent

將 [examples/codex.config.toml](examples/codex.config.toml) 的區塊合併到 **BAT 實際使用的 Codex 設定**。BAT 原始碼預設使用 `~/.codex`，但可被 `codexSharedHome` 等設定覆寫，不應直接假定路徑。

將執行檔路徑換成實際位置，移除舊範例的固定 `--identity-file` 參數，再讓 BAT 重新載入 MCP 連線。多個對話可以使用相同的新設定。

以上 MCP 載入路徑參考本機 BAT 3.2.10 安裝包及原始碼。實際載入成功應以 agent 能列出 `create_room`、`join_room`、`notification_status` 等工具為準。手動 PoC 已有使用者提供的閒置收訊與忙碌排隊成功紀錄；新版 worker 的模型收信與回覆流程仍需實際驗收。

### 獨立 CLI

Claude Code 可以用調整好路徑的 JSON 設定啟動：

```sh
claude --mcp-config /absolute/path/to/your/claude.mcp.json
```

Codex CLI 可以合併 TOML 範例，或在本 repo 根目錄只為這次對話設定：

```sh
codex \
  -c "mcp_servers.agent-mailroom.command=\"$PWD/.venv/bin/agent-mailroom\"" \
  -c 'mcp_servers.agent-mailroom.args=["mcp"]' \
  -c 'mcp_servers.agent-mailroom.tool_timeout_sec=45'
```

這個路徑寫法適用於不含雙引號或反斜線的 repo 路徑。其他情況請使用 TOML 範例並正確跳脫字元。一般設定方式見 [Claude MCP 文件](https://code.claude.com/docs/en/mcp)與 [Codex MCP 文件](https://developers.openai.com/codex/mcp)。

## 在 BAT 裡操作一次

### 1. 第一個 agent 建立房間

告訴 Claude：

> 使用 agent-mailroom，為目前工作區建立協作房間，成員名稱用 claude-reviewer。回報房間 ID，保留自己的 session_key，不要將它傳給其他 agent。

它呼叫：

```json
{
  "workspace": "/path/to/project",
  "member_name": "claude-reviewer"
}
```

`create_room` 回傳結構如下，ID 與 key 會實際產生：

```json
{
  "room": {
    "room_id": "room_<server-generated-id>",
    "workspace": "/path/to/project",
    "created_at": "..."
  },
  "member": {
    "room_id": "room_<server-generated-id>",
    "member_id": "...",
    "member_name": "claude-reviewer",
    "session_id": null,
    "joined_at": "...",
    "workspace": "/path/to/project",
    "state": "active"
  },
  "session_key": "session_<private-generated-key>"
}
```

### 2. 另一個 agent 加入

把 **room_id** 交給 Codex：

> 使用 agent-mailroom 加入房間 room_…，成員名稱用 codex-reviewer。保留你自己的 session_key，不要使用 Claude 的 key。

它呼叫 `join_room`：

```json
{
  "room_id": "room_<received-id>",
  "member_name": "codex-reviewer"
}
```

加入成功會回傳相同房間與 Codex 自己的成員資料、私密 `session_key`。房間 ID 不存在時回傳錯誤，不會意外建立新房間。

### 3. 寄信與回覆

兩邊加入後，Claude 使用自己的 key 呼叫 `send_message`：

```json
{
  "room_id": "room_<received-id>",
  "session_key": "session_<claudes-own-key>",
  "to": "codex-reviewer",
  "text": "請回覆：已收到測試訊息。",
  "request_id": "hello-1"
}
```

再告訴 Codex：

> 使用自己的 session_key 收取這個房間的訊息，wait_ms 設為 30000。若有訊息，回覆給寄件人，使用新的 request_id，並將 reply_to 設為收到的 message_id。處理完成後呼叫 ack_message，然後停止。若等待逾時，回報沒有新訊息並停止。

最後讓 Claude 收取回覆並確認處理。可以先開始等待，再由另一個 agent 寄信。

## 身分、工作區與恢復

| 資訊 | 用途 |
| --- | --- |
| `room_id` | 一次協作的房間識別，可分享給另一個 agent 加入 |
| `member_id` | 房間內的成員識別，不能拿來當憑證 |
| `member_name` | 房間內唯一、供寄信指定的名稱 |
| `session_key` | 私密的成員身分控制資訊，用於一般收發、管理與恢復 |
| `notification_key` | 通知附帶的受限憑證，只供處理該封信；不依賴原本的身分檔 |
| `workspace` | agent 提供的工作區標籤或路徑；不是房間唯一鍵 |
| `session_id` | 可選的原生 session 識別資訊，由呼叫端提供；不會自動偵測或驗證 BAT ID |

同一工作區可以建立多個房間。加入時可省略 `workspace` 以沿用房間資訊，也可以提供自己的 worktree 路徑；這不會改變房間原本的工作區資訊。

bridge 在註冊時自動將憑證保存到 `~/.local/share/agent-mailroom/sessions/`。`session_key` 指向該份私密資料，實際 HTTP bearer token 不會出現在工具回傳內容中。可以用 `mcp --state-dir PATH` 指定位置；一般身分操作重新連線時需使用相同目錄；新版通知的受限處理不讀此目錄。

bridge 或 server 重啟後，agent 呼叫：

```json
{
  "room_id": "room_<previous-id>",
  "session_key": "session_<your-previous-key>"
}
```

`resume_session` 會取回原本房間與成員，不會建立新身分或清空收件匣。房間 ID、成員名稱或工作區名稱都不足以取代私密 key。

若 session 重置後無法取得原本的 `session_key`，可使用 `reconnect_member`，提供原本的 `room_id`、`member_name` 與新的 BAT terminal。server 只會在 BAT 已不再列出舊 terminal，且新 terminal 與舊綁定的 profile、runtime、workspace 完全相符時接受。成功後保留原成員名稱、加入時間、歷史信件及未處理收件匣，改發新的 `session_key`；舊 key 對這個房間失效，未處理信件會重新通知新 terminal。若舊 terminal 仍在線，接管會回傳 `409`。

**註冊重試：** `create_room`／`join_room` 若失敗，工具錯誤會提供本次 `session_key` 供恢復。請沿用它與原本參數重試；成功後重複呼叫也要沿用原 key。省略 key 再呼叫 `create_room`，代表刻意建立另一個房間。相同 key 搭配不同註冊資訊會被拒絕。

`session_key` 本身具有操作該身分的能力，不應放進寄給同儕的訊息。它不是 BAT 或模型的原生 session ID。若對話內容經過摘要，請保留自己的房間 ID 與 key。

## MCP 工具

一般操作使用自己的 `session_key`。處理 BAT 通知時，`notification_status`、`get_message`、`send_message`、`ack_message` 可改傳該通知的 `notification_key` 並省略 session_key；不需要模型暫存或本機身分檔。兩種 key 不可同時提供。通知憑證只允許處理該封信，回覆原寄件者一次，且須先回覆再確認。

| 工具 | 用途 |
| --- | --- |
| `create_room(workspace, member_name, session_id?, session_key?, bat?)` | 建立房間並註冊第一位成員；key 可用於重試 |
| `join_room(room_id, member_name, workspace?, session_id?, session_key?, bat?)` | 加入已存在的房間；key 可用於重試 |
| `reconnect_member(room_id, member_name, bat, workspace?, session_id?, session_key?)` | 原 key 遺失且舊 BAT terminal 已斷線後，以新 session 接續原成員 |
| `resume_session(room_id, session_key)` | 恢復原本成員與房間資訊 |
| `list_members(room_id, session_key)` | 列出已註冊成員，不代表目前在線；不回傳任何私密 key |
| `send_message(room_id, to, text, request_id, session_key?, reply_to?, notification_key?)` | 傳給指定成員 |
| `receive_messages(room_id, session_key, after=0, limit=50, wait_ms=0)` | 收取未確認處理的訊息，可等待最多 30 秒 |
| `ack_message(room_id, message_id, session_key?, notification_key?)` | 由收件人確認已處理，可重複呼叫 |
| `get_message(room_id, message_id, session_key?, notification_key?)` | 寄件人或收件人查看訊息與處理確認時間 |
| `notification_status(room_id, session_key?, notification_key?)` | 查看自己的狀態、BAT 綁定與未確認通知；不含 BAT token |
| `set_collaboration_state(room_id, state, session_key)` | 將自己設為 `active`、`paused` 或 `left` |

### 訊息語意

- 寄送成功表示已保存，不代表對方已讀取或完成工作。
- 讀取不會確認處理。呼叫 `ack_message` 才會設定 `acknowledged_at`；這是 agent 的確認紀錄，不是工作品質驗證。
- 未確認處理的訊息會重複出現在收件匣。收件人需避免重複執行有副作用的動作。
- `request_id` 在「房間＋寄件人」範圍內唯一。相同 ID 與內容重送回傳原訊息，內容不同則回傳 `409`。
- 寄信途中斷線時可能已保存，請沿用原本的 `request_id` 與內容重試。
- `after` 是分頁游標，不是處理確認。新一輪收信或重連後從 `after=0` 開始，避免略過未處理的舊訊息。
- `reply_to` 必須指向同房間中由該收件人寄給自己的訊息。第一版不支援廣播或寄信給自己。
- 成員名稱區分大小寫，限 1–64 個英數字、`_`、`.`、`-`，首字元須為英數字；訊息最多 32,768 個字元。
- `paused` 保留收信能力並停止新通知；`left` 解除 BAT 綁定並拒絕新信，歷史訊息及處理確認仍可存取。重新設為 `active` 不會恢復已解除的綁定。

## 升級至 v0.7

先停止舊 server，再執行 `uv sync --locked --extra bat`，並重新載入 MCP bridge。新版第一次開啟舊資料庫時會依舊版 schema 建立同目錄的 `*.vN-backup-*.sqlite3`，再升級至 schema 6。v0.2 加入成員狀態、BAT 綁定與通知紀錄；v0.3 綁定會保留並標為 `claude`，既有通知紀錄不會重送。Codex 必須明確設定綁定。

v0.4／v0.5 資料庫從 schema 4 升級至 5，新增通知憑證雜湊，原綁定與通知紀錄保留。v0.6 新通知附上 notification_key；升級前已送出的舊通知沒有這個憑證，也不會自動重送。v0.7 升級至 schema 6，記錄同一房間內已被新 session 取代的舊身分，防止舊 key 重新加入。v0.5 移除啟動時的房間／成員綁定參數；改用 `serve --bat-fingerprint 指紋` 啟動，之後由 agent 在 `create_room`／`join_room` 的 `bat` 參數提供 `runtime`、`profile_id`、`session_id`。成功註冊回傳自己的 `bat_binding`。已註冊者沿用自己的 key 呼叫 `join_room` 補上綁定，不必另建房間，詳見 [動態註冊流程](docs/bat-notifications.md)。

同一資料庫只允許一個新版 server 程序持有 `*.sqlite3.server.lock`；請勿以多 worker 啟動。舊版不認得新 schema，回退需停止新版並從備份恢復，會失去備份之後的變更。

## 從 v0.1 升級

停止舊 server，再執行新版。第一次開啟舊資料庫時會先建立同目錄的 `*.v1-backup-*.sqlite3` 備份，再以交易加入房間與工作區欄位。原房間 ID、成員、訊息 ID、回覆關係及處理確認紀錄都會保留。舊資料沒有工作區資訊，因此保留 `null`。

一般 MCP 設定請移除固定的 `--identity-file`，改用工具註冊。若要取回舊收件匣，可暫時使用原本的身分檔啟動一個 bridge：

```sh
/absolute/path/to/agent-mailroom/.venv/bin/agent-mailroom mcp \
  --identity-file /absolute/path/to/old-identity.json
```

在這個連線呼叫 `resume_session(room_id)`，即可取得原本成員與新格式的 `session_key`。之後可回到不含 `--identity-file` 的共用設定，帶上該 key 操作。

不要把舊版固定身分模式設為 BAT 多個對話的共用設定；它只供恢復舊資料。

## HTTP API 與資料範圍

API schema 位於 `http://127.0.0.1:8765/docs`。`POST /rooms` 建立房間，`GET /rooms/{room_id}/membership` 恢復成員，`POST /rooms/{room_id}/members/reconnect` 在 BAT 證明舊 terminal 已離線後替換 session；既有成員、訊息、ack 路徑保留。

HTTP API 使用 `Authorization: Bearer <token>`，由 bridge 代為處理；通知處理可使用受限的 notification_key 作為 bearer，server 會逐次驗證範圍與綁定狀態。`session_key` 是 MCP bridge 的私密索引，不是 HTTP bearer token。

服務適用於同一位使用者的可信任本機環境：僅監聽 loopback、拒絕瀏覽器 Origin 請求，bridge 不使用 HTTP proxy 環境變數。房間採開放加入，沒有邀請審核；加入房間也不能讀取其他成員之間的訊息。

資料庫保存完整訊息，session 目錄保存憑證。兩者都需要備份：正常停止 server 後再備份資料庫與 session 目錄，保留私密權限；不要只複製執行中的 SQLite 主檔而漏掉 WAL。訊息、成員與 session 檔案不會自動到期或刪除。

其他 agent 的訊息不代表使用者授權。各 agent 原本的權限及操作範圍仍然有效；若要自動多回合討論，仍需加入執行控制與停止條件。

## 開發與驗證

```sh
uv sync --locked
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv build
```

測試使用真實 HTTP listener、獨立 stdio MCP bridge 與本機 TLS WebSocket 模擬 BAT，涵蓋房間／身分隔離、處理確認、重連、資料升級、通知、暫停競態及不明送出結果的重啟恢復。自動測試不執行 Claude／Codex 模型，不能取代新版實際操作驗收。

## License

[MIT](LICENSE)
