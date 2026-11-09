# Mailroom 投遞 Phase 1a 實作計畫

日期：2026-11-09；修訂 6（1a 最終驗收、本地 commit 與部署相容條件）  
狀態：Phase 1a 儲存層最終驗收通過，授權於實作分支作本地 commit，包含本計畫；本輪尚未執行 commit。v7 仍為候選，1b、main 合入與日常部署均未開始或授權。  
規劃分支：`feat/mailroom-delivery-phase1a`；實作分支：`feat/mailroom-delivery-phase1a-impl`，基準均為 agent-mailroom `d1c2a5153e4f777a775711f22a2fa53608dc5164`。  
規格基準：BAT `docs/mailroom-delivery-spec` 的 commit `045e964324624533c86e8ee5ec33ac5eca5c9cce`，僅含 [Mailroom 投遞草案 4](../../../../external/better-agent-terminal/plans/mailroom-delivery-plan.md)，包含第 16 項修訂。

本次討論的執行順序是：完成隔離檢查後由 Sonnet 依核定範圍 TDD，主代理在檢查點獨立審查。S／C／O／G 已在指定 worktree 完成，使用者提供最終完整回歸並確認 1a 驗收通過；本次主代理另做定向重跑與 DTO 相容性核對。現在核准本地 commit；本輪只更新計畫與交接條件，不代執行 commit、修改產品程式或重啟任何服務。規格仍在 BAT repo，另一份 untracked issue 文件不納入本分支；所有變更尚未 commit／push。

**本次決定**：採 merge 策略 (a)。1a 可先獨立 review，但不進 main；1b 疊在受審的 1a 上，兩者整合完成後一起合入。v7 在 1b 整合審查通過、使用者核准共同合入版本時才定案。獨立 worktree 與獨立 uv 環境是 Sonnet 開工的必要條件，不是可選建議。

## 開工前的程式碼與環境隔離

日常服務的來源目錄是 `/Users/eason_tseng/playground/mcp-server/agent-mailroom`。本輪只讀核對到該目錄的 `.venv/lib/python3.14/site-packages/_editable_impl_agent_mailroom.pth` 直接指向其 `src/`；使用者指出的日常 serve PID 10459 仍存在。因此即使不碰日常 DB，直接修改這份 working tree 的 Python 檔案，也會影響重啟後的日常服務與 MCP bridge。不能把 branch 當成執行環境隔離。

下列項目全部通過，主代理才可交付 Sonnet：

1. **保護日常來源**：不在上述來源目錄修改產品程式、執行測試／uv sync、切換產品分支或做 checkout／reset／pull／merge；本輪只修改這份尚待審查的 Markdown。不得停止、重啟或向 PID 10459 發訊號；PID 只作本次觀察紀錄，不作日後程序定址。
2. **另建 worktree**：預定路徑 `/Users/eason_tseng/playground/worktrees/agent-mailroom-phase1a`，基準為 `d1c2a5153e4f777a775711f22a2fa53608dc5164`，實作分支為 `feat/mailroom-delivery-phase1a-impl`。目前的 `feat/mailroom-delivery-phase1a` 仍留在日常來源目錄作規劃用途，不強制移走已 checkout 的分支。若目錄或分支已存在，先核對來源與使用狀態，不覆蓋、不強制刪除或重用。
3. **準備受審文件**：規劃文件目前 untracked，不會隨 worktree 自動出現。主代理只複製這一份已核定計畫到實作 worktree 的同一路徑，核對兩份 SHA-256 相同；交接記錄包含計畫 hash、規格 commit、worktree 路徑／分支及實作基準。不能順手複製日常 `.venv`、state、憑證或其他 untracked 文件。
4. **獨立 uv 環境**：在 worktree 內使用自己的 `.venv`，不能 symlink／複用日常 `.venv` 或 `src/`。明確指定 UV_PROJECT_ENVIRONMENT，清除該子程序繼承的 VIRTUAL_ENV、PYTHONPATH、PYTHONHOME；不改 `$HOME`，不使用 `uv --active` 或全域 editable install。
5. **先驗證 import 來源，再跑產品或測試**：確認 cwd、sys.prefix 都在實作 worktree，`agent_mailroom` 與 `agent_mailroom.store.__file__` 指向該 worktree 的 `src/`；其 editable `.pth` 也只能指向該處。只看到 uv 指令成功不算通過。任何一項仍指向日常來源就停止，不透過重啟日常服務解決。
6. **獨立測試資源**：DB、備份、bridge state、log 及 cache 都使用本輪 ledger 記錄的精確暫存位置；真實 Mailroom listener 只用 18765。不得讀／升級日常 DB、連 8765，或藉由日常 bridge 驗證新程式。必要的真實 bridge 子程序也必須由 worktree 自己的 Python 啟動，不能呼叫日常的 `agent-mailroom` console script。

以下保留修訂 2 的 bootstrap 命令，**已由先前執行者完成，不可重跑或覆蓋現存 worktree**。準備時於獨立 shell 執行，任何一步失敗即停止，不能在 cd／worktree 建立失敗後繼續 uv sync：

```sh
set -eu
test ! -e /Users/eason_tseng/playground/worktrees/agent-mailroom-phase1a
if git -C /Users/eason_tseng/playground/mcp-server/agent-mailroom show-ref --verify --quiet refs/heads/feat/mailroom-delivery-phase1a-impl; then
  exit 1
fi
git -C /Users/eason_tseng/playground/mcp-server/agent-mailroom worktree add --detach /Users/eason_tseng/playground/worktrees/agent-mailroom-phase1a d1c2a5153e4f777a775711f22a2fa53608dc5164
git -C /Users/eason_tseng/playground/worktrees/agent-mailroom-phase1a switch -c feat/mailroom-delivery-phase1a-impl
cd /Users/eason_tseng/playground/worktrees/agent-mailroom-phase1a
env -u VIRTUAL_ENV -u PYTHONPATH -u PYTHONHOME UV_PROJECT_ENVIRONMENT="$PWD/.venv" uv sync --frozen
env -u VIRTUAL_ENV -u PYTHONPATH -u PYTHONHOME UV_PROJECT_ENVIRONMENT="$PWD/.venv" uv run --frozen python -c 'from pathlib import Path; import sys, agent_mailroom, agent_mailroom.store; root = Path.cwd().resolve(); assert root == Path("/Users/eason_tseng/playground/worktrees/agent-mailroom-phase1a"); assert Path(sys.prefix).resolve() == root / ".venv"; assert Path(agent_mailroom.__file__).resolve().is_relative_to(root / "src"); assert Path(agent_mailroom.store.__file__).resolve().is_relative_to(root / "src"); print("isolated import checks passed")'
```

此後每次測試都沿用相同 cwd 與環境設定；不能啟動子代理後又回日常來源目錄跑驗證。Sonnet 可寫的範圍限實作 worktree 與本輪測試暫存目錄，日常來源與 BAT repo 只作明確指定的唯讀參考。

## v7 定案與合入順序

- 1a 的 schema 是 **v7 候選**。可以分組 review，取得使用者另行核准的本地 commit 後，1b 再從受審的 1a commit 開分支／worktree 疊接；不能為了疊接自行 commit，也不讓 1a 單獨進 main。
- 1b 整合審查前，v7 候選欄位可調整，且只能升級可丟棄的測試 DB。需要重建時只重建 ledger 內的測試 DB，不維護候選 v7 版本彼此之間的正式升級路徑；仍須保留 v6／既有舊版 fixture 到最終 v7 的升級測試。測試證據記錄當時 DDL／commit hash，不能把所有 user_version=7 的候選 DB 都當成相同結構。
- **定案條件**：1a 儲存／遷移測試、1b 正式 host／綁定／投遞憑證與 HTTP 整合測試、舊模式回歸和暫存 DB 備份／恢復驗證完成；逐項測試歸屬均有結果；主代理及使用者完成整合審查，並核准共同合入版本。此時凍結 v7 DDL、約束與升級程式的 commit／hash。
- 定案後不得原地改寫已發布的 v7 遷移定義。再需要持久化結構變更，就設計下一版 migration（預期 v8），而不是因同名 v7 在開發時曾改過就重用舊測試 DB。
- 兩張票共同合入仍需使用者授權。**合入不等於日常部署**：不得因此在日常來源目錄 checkout／pull 新碼、重啟服務或啟動 DB 升級。日常程式來源更新、DB 升級與回退安排需另行回報並獲准；本計畫不執行它們。

## Phase 1a 最終驗收與本地 commit 範圍

**本次決定**：授權於 `feat/mailroom-delivery-phase1a-impl` 作一次本地 commit，包含受審的產品／測試差異及**這一份計畫文件**，讓 1b 從可追溯的程式與計畫版本接手。不 push、不進 main；不在日常來源目錄切 branch 或提交副本。沿用策略 (a)，v7 尚未定案。

預定納入的檔案限定如下；實際 stage／commit 前再逐項比對，不用 git add . 或整個 docs/plans/：

```text
src/agent_mailroom/models.py
src/agent_mailroom/notifications.py
src/agent_mailroom/store.py
src/agent_mailroom/prompt.py
tests/conftest.py
tests/test_migration.py
tests/delivery_helpers.py
tests/test_delivery_claims.py
tests/test_delivery_grants.py
tests/test_delivery_migration.py
tests/test_delivery_status.py
docs/plans/2026-11-09-mailroom-delivery-phase1a.md
```

commit 前應核對 staged diff、文件／測試中沒有憑證值、沒有 DB／備份／log／cache／.venv，並確認 parent 仍是受審的 d1c2a51。若還有清單外產品差異或驗收後程式碼又改了，先停下回報；不能把先前通過的結果套在新差異上。完成後回報 commit SHA、檔案清單及實際驗證結果。commit 訊息可用 `feat(mailroom): add delivery claim storage and legacy migration`，不加 Co-Authored-By 尾註。

| 驗證 | 結果與來源 |
| --- | --- |
| 完整最終回歸 | 使用者獨立重跑最終差異：pytest -q -rw exit 0，414 passed，6 分 51 秒；1 個已知 Starlette BlockingPortal／anyio deprecation。本輪主代理沒有再跑全套。 |
| 本輪定向重跑 | 主代理在隔離 worktree 重跑 claims、grants、status、delivery migration、既有 migration 五檔，exit 0，135 passed，20.48 秒；只有同一個已知 warning。 |
| lint／format | 主代理重跑 ruff check src tests、ruff format --check src tests，皆 exit 0，26 files already formatted。 |
| fixture／noqa | 主代理確認 tests/ 的 Python 檔沒有 noqa，conftest 不 import test_ 模組，使用 delivery_helpers 共用資料。 |
| 相容性反例 | 主代理離線載入 git d1c2a51 的 NotificationAttempt，以當前 Pydantic 比對：舊模式額外 null 欄位可接受；released 與 null client_message_id 分別被舊 DTO 拒絕；新 DTO 可接受。不冒稱已對日常長駐 bridge 本身測試。 |
| 資源與日常環境 | 使用者提供 18765 無殘留 listener、日常來源／.pth 未變、PID 10459 起始時間仍為 09:53:23；主代理在本輪結束前另做唯讀核對，不停止或重啟服務。 |

本輪重跑的完整 src/tests Python 快照指紋為 `83df1181e1d011440792aa140f3fb5eec5c32c6e7fede672191313ae17a9ff8c`：以兩目錄內所有 .py 相對路徑與 SHA-256 排序後的 Python repr 再取 SHA-256。後續只有本計畫文字變動，沒有重新改動產品程式或測試。

**接受的回應差異與邊界**：
- 未轉換舊模式多出 binding_version、unknown_reason、outcome_reported_at 三個 null 欄位，是只新增欄位的相容變更；基準舊 DTO 沒有 extra=forbid，會忽略它們。
- released Literal 與 client_message_id 可為 null 則不是舊 bridge 能讀的格式；必須滿足下節的部署屏障，不能只以新增欄位相容推論整個新模式相容。
- 舊模式 takeover 後，新成員在尚無自己的 claim／notification attempt 時 pending_notification 為 None，符合新身分尚未開始通知嘗試的狀態；訊息未 ack 與後續重新排送由訊息／worker 規則決定，不把 None 當成處理完成。新模式仍拒絕無 key reconnect。
- worker_enabled 的新模式意義、recover_delivery_claims 與 expire_overdue_claims 的生命週期接線仍由 1b 完成；Store helper 通過不代表啟動恢復、運行期間期限觀測或正式授權 HTTP 入口已驗收。

## 1b／4b 的部署相容性門檻（本輪不執行）

**新模式啟用及 legacy 轉換之前，server 與所有長駐 MCP bridge 都須改用相容版本，完成協同更新／重啟與核對。** 不是要求 OS 在同一瞬間重啟，而是在所有對象就緒前保留啟用屏障；不得先轉換再等舊 bridge 自行重啟。不能以 package 版本字串或 health 成功取代來源 commit／DTO 相容性確認。

1. **1b 的 B08 測試**：保留舊 DTO 對舊模式新增 null 欄位可讀、對 released／null client_message_id 拒絕的反例，以及新 bridge 對舊／新 status 格式的序列化測試；把「尚未轉換」與「已有 claim／已轉換」明確分開。
2. **4b 的操作清單**：列出要更新的 server、各 agent/runtime 的長駐 bridge、各自來源／環境與核准版本。不能只重啟 server，或只處理新開的 agent 分頁；也不能靠模型回覆「已更新」證明所有舊 Python model 已卸載。
3. **有授權後才進入維護步驟**：準備核准版本、備份／回退與未完成通知安排，停止舊通知 worker／新 claim 入口；更新並重啟 server 與所有 bridge，逐一確認就緒。server 可在舊模式過渡；所有 bridge 相容性未確認前，不執行 convert_legacy_to_delivery 或發出 normal claim。
4. **確認後才啟用**：核准的切換操作在 server lock 與舊 worker 停止條件下執行，再開放新模式投遞。任何 bridge 版本、來源或重啟狀態不明，就維持未啟用並回報；不把型別錯誤吞掉後假裝沒有待處理通知。
5. **失敗處理與權限邊界**：不能直接把程式退回 d1c2a51 並交給它已升級的 v7 DB。涉及日常 DB 恢復、程式來源切換或服務重啟，均需另外獲准。本次本地 commit 和將來 main 合入，都不等於這些部署操作已獲授權。

本版不新增 bridge 註冊／自動版本協商系統；此門檻由受控部署清單、已測的相容性資料及逐一更新核對落實，不宣稱 server 自動知道所有舊 bridge 都已退出。1b 的相容性測試與 4b 的部署／回退文件必須在共同合入審查列為接手事項；**實際日常重啟不是本地 commit 或 main 合入時要自動執行的測試**。

## 目標與完成條件

Phase 1a 完成 **SQLite 的 claim／outcome 儲存與狀態規則、備份升級、legacy 阻擋轉換，以及對應測試**。它是 Phase 1b 控制面與 HTTP API 的基礎，不能單獨宣告 BAT 已可投遞。

完成時必須能獨立驗證：

1. claim 在同一筆交易重新核對成員 active、綁定版本／confirmed、host 所有權與 epoch、最舊未 ack 訊息及既有阻擋，再持久化 submitting；失敗不留下部分紀錄或通知憑證。
2. outcome 只更新其 claim；released 需要明確的未送入證據，unknown 不因逾時、程序退出或新 epoch 自動釋放。相同結果重試冪等，矛盾結果拒絕。
3. schema 升級先備份再交易提交，舊通知不因切換新表而重送，orphan legacy 與世代基準不因重啟／重跑遷移而消失。
4. 通知憑證的訊息範圍、撤銷及 ack 行為有測試；成員憑證不輪替。現有訊息去重、成員操作及舊模式通知路徑維持相容。
5. 測試有資料庫前後狀態與拒絕情境的斷言，不以回傳成功、測試數量或替代 BAT 模型當成產品驗收。

## Phase 1a／1b 的依賴與範圍

基準 d1c2a51 的 `Store.initialize()` schema 版本為 6，沒有 binding_version、pending／confirmed 或 host_epoch（`src/agent_mailroom/store.py:117-166`）。claim 不能只拿呼叫端傳入的版本／epoch 自行宣告有效，因此 Phase 1a 必須先提供最小的持久化資料契約。

| 項目 | Phase 1a | Phase 1b／後續 |
| --- | --- | --- |
| delivery_claims、期限、legacy、交易與恢復 | 實作及測試。 | 使用這些儲存操作。 |
| 綁定世代、狀態、owner／epoch 的儲存形狀 | 從 1b 移入必要 schema、持久世代基準、遞增世代／撤銷舊通知 grant 的原子儲存操作，以及 claim 的 canonical metadata 核對；對應估算已移轉。測試可直接建立合法的 host／confirmed 前置資料。 | 呼叫上述儲存操作，實作正式 host 註冊／epoch 配發、兩段式確認、rebind 授權與模式控制；不能另寫第二套世代規則。 |
| HTTP `/delivery/claims`、outcome 入口 | 不先暴露缺少投遞憑證驗證的 route；以真實 Store 操作驗證 MailroomError 的 status_code。 | 投遞憑證驗證與 host scope 就緒後，才接上正式 HTTP 入口及整合測試。 |
| 成員與通知憑證 | 實作 claim 通知憑證的查詢／撤銷所需儲存支援，驗證原成員憑證仍有效。 | 正式模式回應、控制面授權與工具說明整合。成員憑證輪替另票。 |
| 新舊模式切換 | 提供可重跑的資料轉換操作，測試 legacy 阻擋；不自行改 CLI 的啟動模式。 | 在持有 server lock 且舊 worker 已停止後，呼叫切換操作；完成設定、註冊與對外路由。 |
| BAT runtime／ID 注入、flock、Codex config | 不修改。 | BAT Phase 2／3。 |

測試直接建立的 confirmed／host metadata 只證明儲存規則，不證明註冊授權或真實兩段式綁定完成。缺少這些前置資料時，正常 claim 必須拒絕；不能加入正式環境可用的「測試確認」端點、預設 host 或永遠成功的驗證器。Phase 1b 必須依同一資料契約整合，不能另建平行的版本／epoch 來源。

## 已讀取的程式路徑與預定修改範圍

| 檔案 | 本次依據與預定用途 |
| --- | --- |
| `src/agent_mailroom/store.py` | 每個操作自有 connection／transaction；現有 `BEGIN IMMEDIATE`、delivery_lock、SQLite busy → 503、備份及 schema 升級。新增儲存方法與必要小型 helper，優先沿用此風格，不新增 Repository／Unit of Work 框架。 |
| `src/agent_mailroom/models.py` | 現有共用驗證、MailroomError、NotificationCredential；新增 claim／outcome 的必要型別。不能讓現有 NotificationAttempt 的舊模式回應因新欄位或 released 狀態而驗證失敗。 |
| `src/agent_mailroom/api.py:61-86` | lifespan 先取得單一 server lock，再 initialize／recover。新的 crash recovery 要在服務啟動邊界整合，不能在一般 initialize、查詢或每次開 connection 時誤把正在進行的 claim 當成 crash。不得提前新增無授權的 delivery route。 |
| `src/agent_mailroom/notifications.py` | 舊 worker 呼叫 next_notification／submit_notification／finish_notification；原路徑必須有回歸證據，不在這張票移除或改接 BAT 拉取模式。 |
| `tests/test_migration.py`、`tests/test_notifications.py`、`tests/test_notification_access.py`、`tests/test_api.py` | 延伸既有備份、unknown 不重送、通知授權與 API 相容性測試。明確保留舊版 DB fixture，不能只改預期版本而失去升級路徑測試。 |
| 新增 `tests/test_delivery_claims.py`、`tests/test_delivery_migration.py`，必要時新增通知授權測試檔 | 放置下列情境；避免為單次測試引入額外框架或不必要的產品測試 hook。 |
| `tests/conftest.py` 及相關 live fixture | 只有固定測試 Mailroom port 或明確可重用 fixture 所必需的修改；保留既有先 bind socket、再交 uvicorn 的資源持有方式。 |

不修改 `identity.py` 的成員 key 產生／恢復規則，不輪替成員 key，不修改 BAT repo 或已提交規格。若需要改變本表以外的產品行為，先回報具體原因與差異，不能由子代理自行擴大範圍。

## 資料與狀態規則

### schema、升級與切換

- 預計由 schema 6 升為 7；沿用 SQLite backup、0600 權限、`BEGIN IMMEDIATE` 與 PRAGMA user_version 的既有模式。新 DB 不製造無意義備份；較新版本 DB 應原樣拒絕。升級中斷不得留下半套 schema／user_version。
- `delivery_claims` 保存規格欄位，另有可辨識 normal／legacy 的來源資料、room／member 關聯及判斷首次 outcome 是否已定案所需的持久化資訊。normal 的期限／host epoch 必填；legacy 的這些欄位不適用，不能偽造值或新發通知憑證。
- 為 Phase 1b 定義唯一的 host current_epoch、binding state／version／host_id，以及每 room 成員的最高綁定世代／遷移基準的持久化位置。具體欄位與索引在 TDD 起始差異中列明；不能只把這些值塞進測試參數，繞過資料庫核對。
- claim 歷史不可隨 binding 刪除而 cascade 消失。不能以 `UNIQUE(message_id)` 阻止 released 後的另一個嘗試；也不能以「某 binding 有任何 accepted 歷史」阻止該信 ack 後投遞下一封。正確性依同交易的候選／阻擋檢查，索引只支援查詢。
- **schema 建立與啟用新模式的轉換分開**：共同的 initialize 只準備必要表／欄位，不提前回填通知狀態、凍結遷移基準或把既有 binding 轉 pending，避免仍在舊模式運行的服務改變行為。legacy 回填與初始 pending 放在明確的新模式轉換操作中；正式呼叫時點與模式互斥由 Phase 1b 接上，不能假設升到 7 就已啟用新模式。
- 切換時為舊 notifications 保留 legacy 阻擋；accepted／not_accepted／unknown 保留，submitting 變 unknown。保留通知 hash 與既有撤銷結果，不能復活已撤銷 grant；ack 過的信不重新成為候選。舊模式在 schema 升級後、正式切換前仍可能產生紀錄，切換操作必須涵蓋這些紀錄，不能只回填升級當下的快照。
- legacy 首次回填保存當時綁定與成員世代基準；無版本取 0，舊 binding 無法對映也保存 room／成員／收件人／訊息的阻擋。重跑不得重算基準。既有 binding 的同版本遷移確認不放行；只有原 key 明確建立高於基準的新世代並確認，才可在新世代放行。
- 遞增世代、更新最高版本及撤銷先前通知 grant 的原子儲存操作移入 Phase 1a；觸發它們的正式註冊／rebind／confirm 控制面與授權由 Phase 1b 負責。1b 呼叫已受審的操作，不重新實作世代分配。leave／刪除 binding 不能刪掉最高世代或遷移基準。

### claim 與 outcome

- 候選查詢只讀，不發通知憑證。真正 claim 時在同一交易重新核對 binding／member／host／message 與阻擋條件；**取得 candidates 後才 pause** 必須回 409，不插入 claim，也不呼叫通知憑證產生器。恢復 active 後，同一封信仍可被 claim。
- 核對成功才產生通知憑證、只保存 hash、寫 submitting 並提交，最後才回傳 claim_id、期限與提示。提示不含正文、成員 key 或投遞憑證。並行 claim 用不同 Store／connection 驗證 SQLite 的序列化效果，不能只測同一把 Python RLock。
- outcome 比對的是 claim 建立時的 binding_id／version，不要求目前綁定仍相同。只有 submitting，或尚未接收晚到結果的 report_timeout unknown 可改寫；結果一旦定案，同結果重試冪等，矛盾結果拒絕。legacy 不接受新 outcome。
- released 只接受規格所列 busy／expired／not_invoked 證據，撤銷該次通知 grant 並保留歷史；下次是新 claim_id 與新通知憑證。不能從 deadline、HTTP 例外或程序退出推導 released。
- report_timeout unknown 允許一次晚到結果；若第一次晚到結果本身仍為 unknown，也必須持久標記已回報，不能讓第二次不同結果繼續改寫。相同結果的重試仍須冪等。
- 提供 timeout 與 startup recovery 的明確操作。測試以受控時間推進，不等待真實 10 分鐘；不新增不必要的背景排程器。server 啟動恢復的 unknown、legacy unknown 不接受晚到結果改寫，與 report_timeout 區分。
- 舊 claim 的晚到結果不能恢復已撤銷通知 key 或改變新綁定資格。host_epoch 只管新工作；superseded instance 對其既有 claim 的結果回報不因 epoch 過期被一律拒絕，但不授予新 claim 資格。HTTP 授權整合仍待 Phase 1b。

## TDD 順序與測試矩陣

先寫會因目標規則缺失而失敗的測試，確認失敗原因，再補最少程式，最後做局部整理。每一組先完成正例與相鄰拒絕例，再進下一組。不得先補一個永遠接受／拒絕的替代層，把它的綠燈當成儲存或授權已完成。

以下是 **34 個基礎情境**；參數化可能產生更多案例，數量不是驗收標準。S／C／O／G 分組也是預定實作順序。

| ID | 情境與必要斷言 |
| --- | --- |
| S01 | 新 DB 建立完整 v7 schema，沒有備份或假 legacy grant。 |
| S02 | v6 先備份再升級；備份可讀、0600，房間／訊息 ID、未 ack 狀態、成員憑證識別資料保持一致，不輸出其內容。 |
| S03 | 舊 accepted／not_accepted／unknown／submitting 分別轉換；只有 submitting 轉 unknown，均不發新憑證或重送。 |
| S04 | 現存 binding 以世代 0 匯入 pending；同版本確認不解除 legacy 阻擋。 |
| S05 | orphan legacy 沒有 binding 時仍有基準與阻擋；原 key 建立更新世代且確認前不能 claim，確認後可 claim。 |
| S06 | 現存 binding 另有 orphan legacy，保留成員最高版本；初次確認／host_epoch 更新不放行，明確更新世代才放行。 |
| S07 | 重跑 schema／轉換不重算基準、不複製 legacy 嘗試；正式切換涵蓋升級後由舊模式新增的 notifications。 |
| S08 | 故意中斷升級要 rollback；較新 schema 原樣拒絕。另核對備份保留、沒有半套表或錯誤 user_version。 |
| C01 | 合法 claim 只產生一次 submitting；回傳前，另一個 connection 已可讀到完整紀錄。 |
| C02 | 提示只有必要定址與通知憑證，不含正文、成員憑證或投遞憑證；DB 不保存通知憑證明文。 |
| C03 | 候選查詢排除 pending／rejected；即使手工傳入這些目標或舊版本，claim 仍拒絕，row／grant 與原快照完全相同。 |
| C04 | 其他前置條件皆合法時，只改舊 host_epoch，斷言 MailroomError.status_code == 409 且 detail == "instance_superseded"；只改錯誤 binding host owner、維持呼叫 host 的目前 epoch 時，斷言 409 且 detail == "binding_host_mismatch"。兩者都不寫入 claim、不呼叫憑證產生器；不能只核對傳入數值相等。 |
| C05 | paused／left 成員拒絕；即使綁定仍 confirmed，不能通過。 |
| C06 | 先取得 candidates 再 pause，claim 回 409，沒有 row／grant、產生器未被呼叫；恢復 active 後同訊息可 claim。 |
| C07 | claim 先提交、pause 後到：既有 submitting 保留，不倒推 released；通知憑證依 pause 限制使用。 |
| C08 | 非最舊未 ack、已 ack、錯誤收件人／房間的訊息不可 claim。 |
| C09 | 兩個 Store／connection 同時 claim 同綁定候選，最多一個成功；另一個不產生第二個 grant。 |
| C10 | 各阻擋狀態不放行後續訊息；最舊訊息 ack 後，可處理下一封，舊 claim 歷史仍存在。 |
| O01 | accepted／not_accepted／unknown 的合法結果持久化，accepted 不等於 ack。 |
| O02 | busy 證據可 released；舊通知 key 失效，下次對同訊息建立新 claim 與新 key。 |
| O03 | expired／not_invoked 分別可 released；不重新呼叫 runtime。 |
| O04 | 缺乏合法未送入證據的 released 被拒絕，原 claim 與 grant 不變。 |
| O05 | 相同已定結果重試冪等，沒有第二筆嘗試，也不重寫第一次證據。 |
| O06 | 矛盾結果、錯誤建立時 binding tuple、未知 claim 分別為規格要求的 409／404，其他紀錄不變。 |
| O07 | report_deadline 到期成 report_timeout unknown；仍阻擋，不自動 released 或產生新 claim。 |
| O08 | report_timeout unknown 的晚到 accepted／released 可定案一次；相同重試冪等，後續矛盾結果拒絕。 |
| O09 | 第一次晚到結果仍為 unknown 時也定案；第二次不同結果不可再改寫。 |
| O10 | startup recovery unknown、legacy unknown 不接受 outcome 改寫，期限過去也不改成可重送。 |
| O11 | binding／epoch 已變後仍可回報舊 normal claim 的真實結果；不改新綁定資格、不復活舊通知 grant。 |
| G01 | 新通知 grant 只讀指定訊息、只回覆原寄件人一次、只 ack 指定訊息；跨訊息或跨 room 拒絕。 |
| G02 | 新世代使舊 normal／legacy 通知 grant 回 403，原成員憑證讀信／回信／ack 仍有效；註明前置世代由 fixture 建立，不冒稱正式 rebind HTTP 流程已驗收。 |
| G03 | leave 不撤銷成員 key，同 key 可恢復 active；不因這張票隱式啟用成員憑證輪替或 takeover。 |
| G04 | v7 schema 就緒但未啟用新模式時，舊 Remote worker、原通知憑證、request_id 去重、訊息收送與 ack 的回歸行為不變。 |
| G05 | 沒有 canonical host／binding metadata 就拒絕 normal claim；一般 initialize／讀取不能誤觸 crash recovery。 |

C04 的舊 epoch detail 依規格固定為 `instance_superseded`；owner 不符的 detail 名稱原規格未指定，本計畫補明為 `binding_host_mismatch`，讓 1b 沿用同一契約，不能把仍有效的其他 host 誤判成 instance 已被取代。`api.py:98-100` 的通用 handler 原樣回傳 `{"detail": exc.detail}` 與 status_code；1b 接線後也須驗證該 JSON 及 HTTP 狀態，而非只比對字串包含關鍵字。本輪沒有修改產品錯誤碼。

兩段式確認端點、正式 rebind／新模式 reconnect 的 HTTP 驗證，以及投遞憑證授權由下表逐項指定 1b 責任；本階段不能用 metadata fixture 或注入的授權依賴宣稱它們通過。

## 規格第 336 行測試清單的逐項歸屬

以下逐項轉錄 commit `045e964` 的 **30 項 Mailroom 單元測試要求**。R01–R30 是依規格原順序的固定索引，不取代上面的 34 個實作情境；「兩邊」表示 1a 的真實儲存／交易測試與 1b 的正式入口整合各有責任，不能互相折抵。標成 1a 的項目仍須經 1b 一般路由接線與驗證，但不用將所有儲存組合複製成第二套 HTTP 測試。

1b 的接手測試分組如下，必須列入其計畫與最終 v7 整合審查，不得留成沒有負責票的「等整合情境」：

- **B01**：無 key reconnect 的新模式固定拒絕；正常請求無 takeover 副作用。
- **B02**：真實投遞者授權後，逐入口的 epoch 檢查、C04 錯誤契約、pause 競態與舊 outcome 例外。record_outcome 不檢查目前 epoch 是刻意保留舊結果回報；HTTP 必須以已驗證投遞憑證解析出的 host_id 比對 claim 建立時保存的 host_id，不能拿 request body 或目前 binding 的 owner 代替。
- **B03**：真正的投遞憑證驗證與角色範圍，不以授權 stub 冒充；包含正反向拒絕測試。另加 outcome 的跨 host 拒絕案例：host A 建立 claim，持合法投遞憑證的 host B 仍不可回報它，拒絕且 DB／evidence／grant 不變；同 host 的舊 epoch 對自己的舊 claim 仍依規則可回報。具體授權錯誤 detail 由 1b 定義並納入 B07 對齊，不把放寬 epoch 誤當成放寬 host 身分。
- **B04**：正式 join_room／state／pending／confirm／reject／candidates 生命週期、冪等及版本。
- **B05**：legacy 啟用、rebind、通知 grant 撤銷、新 claim 與舊 outcome 的完整流程。
- **B06**：模式切換、啟動 recovery、host 註冊／服務重啟，以及 schema 定案前的整合回歸；recover／expire 與 worker_enabled 接線不能以 Store helper 測試替代，啟用前先滿足 B08 的 server／bridge 相容屏障。
- **B07（必做：規格偏離回補）**：在 1b 整合審查回補 BAT 投遞規格並核對實作，至少包含：
  - `binding_host_mismatch` 與 `host_not_registered` 均為 HTTP 409，detail 精確比對；舊 epoch 仍為 409／instance_superseded，不能混淆三者。
  - 實際 SQL 主鍵是 claim_seq，claim_id 是 NOT NULL UNIQUE，對外仍只以 claim_id 定址；claim_seq 僅作穩定的嘗試排序，不得成為對外 claim 身分。
  - 世代識別實際為 `(room_id, member_id)`，使用 member_id 而非 member_name；不能跨 room 共用計數或撤銷。晚到 legacy 以當時最高世代保守記錄。
  - 重跑轉換只補新增 legacy row，不更新已匯入的狀態／基準；正式切換必須先停止舊 worker，不把補救操作當成與舊 worker 同時運作的同步服務。
  - O 組的結果定案欄位／unknown 原因分類，以及 G 組的單一 grant 撤銷權威。
  - 本次 C／O 實作新增的十個 409 detail：`binding_mismatch`、`binding_not_confirmed`、`binding_not_current`、`binding_version_mismatch`、`blocked_by_previous_claim`、`claim_not_accepting_outcome`、`conflicting_outcome`、`member_not_active`、`message_acknowledged`、`message_not_oldest`。
  - 無效 result 為 422／unknown_outcome_result，released 缺少合法未送入 evidence 為 422／released_requires_not_sent_evidence；不是把所有自由文字 evidence 都宣稱已全面驗證。其他 evidence 格式若另有限制，1b 也須逐項訂明並回補。
  - 晚到 accepted／not_accepted／released 結果仍可能保留 unknown_reason=report_timeout；它描述先前未知狀態的成因，並非當前仍是 unknown。當前結果看 state，是否已接收結果看 outcome_reported_at，不能由原因欄位倒推結果或重新開放改寫。
  未完成回補及相容性測試，不得核准 v7 定案或共同合入。本次只更新接手清單，不修改已提交的 BAT 規格。

- **B08（必做：部署相容性）**：依本文件「1b／4b 的部署相容性門檻」，完成舊／新 DTO 比對與狀態序列化測試，並交付 4b 的 server＋所有長駐 bridge 更新／重啟與確認清單。未確認前不得 legacy 轉換或啟用新模式；不在本輪操作日常服務。

本表的 1b 是 Mailroom 的 HTTP／控制面測試責任；不將其 fixture 提交的觀測資料當成 BAT 實機證據。涉及 BAT 缺席／mismatch 判斷與 runtime 的部分，仍須 Phase 2／4 驗收。

| 規格索引 | 規格要求（原文） | 負責票 | 1a 對應 | 1b 必須補足的驗證 |
| --- | --- | --- | --- | --- |
| R01 | 先寫入 claim 才回傳提示 | 1a | C01 | 1b 接 HTTP 時保留回傳前已提交的順序，不另造替代成功回覆。 |
| R02 | `unknown` 不重送 | 1a | C10、O07、O10 | — |
| R03 | 每個綁定只處理最舊的未 ack 訊息，最新 claim 未解除時不往後投遞 | 1a | C08、C10 | — |
| R04 | outcome 的 binding 與 claim 記錄值不符時被拒絕 | 1a | O06 | — |
| R05 | 綁定變更後，舊 claim 的真實結果仍可記錄，且不影響新 claim | 兩邊 | O11 | B05：由真實 rebind 控制面改綁定，再以舊 tuple 回報，不改新 claim。 |
| R06 | 矛盾的回報回 409 | 1a | O06 | — |
| R07 | `released` 沒有允許的證據時被拒絕 | 1a | O04 | — |
| R08 | `released` 保留為終態並撤銷 key，重送冪等，下一次 claim 拿到新的 `claim_id` | 1a | O02、O03、O05 | — |
| R09 | 超過 `report_deadline` 轉為 `unknown` 而不是釋放，之後的晚到回報只能改寫一次 | 1a | O07、O08、O09 | — |
| R10 | 啟動恢復產生的 `unknown` 不接受回報 | 兩邊 | O10 | B06：服務啟動實際呼叫 recovery，HTTP 不接受啟動恢復 unknown 的改寫。 |
| R11 | pause 不撤銷通知憑證，恢復 active 後仍可使用；leave、rebind 後舊 notification_key 回 403 | 兩邊 | C07、G02、G03 | B04：經真實 state／rebind 入口驗證通知 grant 的暫停、恢復與撤銷。 |
| R12 | rebind 後原成員 key 的讀信、回信、ack 仍有效，並核對獨立授權路徑，照實保留現有限制 | 兩邊 | G02 | B04：真實 join_room rebind 後，用同一成員憑證驗證讀信／回信／ack。 |
| R13 | leave 不撤銷成員 key，同 key 可恢復 active 並重新綁定 | 兩邊 | G03、S05 | B04：left → 同 key active → join_room／confirm 的完整順序，沒有隱式恢復。 |
| R14 | 新模式合法的無 key reconnect 一律回 409 liveness_unknown，不建立新綁定或替換成員身分 | 1b | — | B01：新模式合法無 key reconnect 的 HTTP 409／liveness_unknown，且 DB 無 takeover 副作用。 |
| R15 | 舊 epoch 的 pending／confirm／reject／candidates／claim 請求回 instance_superseded；舊 claim 的 outcome 仍依第 3 節處理 | 兩邊 | C04、O11 | B02：pending／confirm／reject／candidates／claim 逐入口驗證 HTTP 409＋detail=instance_superseded；舊 outcome 可依規則回報。 |
| R16 | rebind 後回 pending，完成確認前不能 claim；pending／confirmed 的同目標重試不重建 binding | 兩邊 | C03、世代原子操作 | B04：正式 rebind 先 pending；確認前 claim 拒絕；同目標 pending／confirmed 重試不增版。 |
| R17 | 找不到或無法核對 terminal 時仍為 pending；明確不符才 reject；rejected 後同 key／同目標 join_room 可重新進 pending 並只增加一次版本 | 1b | — | B04：控制面只接受明確 mismatch 的 reject；missing 維持 pending；rejected 同目標重試僅增加一次版本。BAT 的實際觀測另由 Phase 2／4 驗證。 |
| R18 | 遷移 accepted 未 ack 通知經 reject、同 key／同目標 join_room 重新進 pending 並 confirm 後，legacy 通知 key 回 403，新 claim 只有新通知 key 有效，原成員 key 仍有效 | 兩邊 | S04、G02 | B05：遷移後經實際 reject → 同 key join_room → confirm，舊 grant 403、新 grant 可用、成員 key 仍有效。 |
| R19 | orphan legacy 成員以原 key／同目標 join_room 建立高於遷移基準的新版本，confirm 前不能 claim、confirm 後可 claim；原為 left 時先同 key 恢復 active | 兩邊 | S05、G02 | B05：無 binding 的原成員經真實 active／join_room／confirm 建立高於基準的新世代。 |
| R20 | 現存 binding 的成員另有 orphan legacy：遷移轉 pending 的同版本初次 confirm 不放行；透過改目標、先 reject 再重新確認，或 leave 後恢復再 join_room 建立更新世代，並確認後才放行；同目標的 pending／confirmed 重試不增版，不能用 host_epoch 更新替代 | 兩邊 | S06、S07、G02 | B05：現存 binding 另有 orphan 的同版本確認與明確新世代流程；epoch 更新不能放行。 |
| R21 | 遷移基準為首次回填時的最高版本、無版本取 0；schema 重跑不重算，leave／刪 binding 不重設歷來最高版本 | 兩邊 | S05、S06、S07、世代原子操作 | B04／B06：實際 leave、刪 binding、服務重啟均保留最高版本與首次基準。 |
| R22 | confirm／reject 不改版本，成功重試冪等；相反終態或舊版本回報回 409，claim 使用確認回傳的版本 | 1b | — | B04：confirm／reject HTTP 的冪等、相反終態／舊版本 409 及回傳版本供 claim 使用。 |
| R23 | 舊 binding 有 accepted 未 ack claim 時，key rebind 並確認後，新 binding 可 claim 該信；舊 outcome 仍只更新舊 claim | 兩邊 | O11、G02 | B05：舊 accepted 未 ack → 正式 rebind／confirm → 新 claim，再回報舊 outcome。 |
| R24 | 升級保留舊 notifications 的 accepted／not_accepted／unknown 阻擋；submitting 轉 unknown；遷移／confirm／重啟／重跑 schema 都不自動重送，legacy unknown 不接受 outcome 改寫 | 兩邊 | S02、S03、S07、O10 | B05／B06：實際新模式啟用才轉換 legacy；模式確認／重啟不發新 key、不重送。 |
| R25 | BAT 重啟與 epoch 更新不改既有 binding 版本、狀態或通知憑證，也不自動重送已通知而未 ack 的訊息 | 兩邊 | C10、O11、G04 | B06：正式 host 重註冊與服務重啟保留 confirmed／版本／grant，不能重新初始化 pending。 |
| R26 | 同一綁定的最新 claim 是 `unknown`、`not_accepted` 或 `accepted` 未 ack 時，後續訊息不能被 claim；`released` 時最舊的訊息可以重新被 claim | 1a | C10、O02 | — |
| R27 | 晚到的回報不重新啟用已撤銷的 key | 1a | O11、G02 | — |
| R28 | pending 綁定不出現在 candidates | 兩邊 | C03 | B04：GET candidates 真正排除 pending；手工 POST claim 也拒絕。 |
| R29 | 取得 candidates 後成員才轉為 paused：之後的 claim 回 409，不寫入 claim、不產生通知憑證；恢復 active 後同一則訊息可正常 claim | 兩邊 | C06 | B02：經已授權的 claim HTTP 入口重現 candidates → pause，驗證 409、DB 無變更、active 後可 claim。 |
| R30 | 投遞憑證不能讀寫信件 | 1b | — | B03：用真實投遞憑證驗證其不能代替成員／通知憑證讀信或寄信；反向也不能以成員／通知憑證管理 delivery。不得 mock 掉身分解析。 |

## 測試隔離與驗證命令

- DB、bridge state、備份及證據均用本輪明確建立的暫存目錄，不讀日常 `~/.local/share/agent-mailroom`，不連日常 `8765`。
- 新增單元測試優先用真實 SQLite Store／TestClient，不啟動 listener。需要真實 Mailroom HTTP listener 時只用 `127.0.0.1:18765`，已占用就失敗回報，不終止其他程序、不自動改用 8765。
- 既有 `live_url` 等 fixture 現在保留隨機 port。執行本任務完整回歸前，要在測試設定／相關 fixture 讓真實 Mailroom listener 固定 18765，沿用先保留 socket 的方式並序列執行；不能原封不動跑隨機 port 卻聲稱已符合固定 port 限制。模擬 BAT 的 WebSocket fixture 與 Mailroom listener 分開，保留各自測試資源責任。
- 不列出憑證值、不保存原始 auth frame 或帶 key 的提示。測試失敗輸出也須檢查；報告可列欄位名稱與授權狀態，不可列 session_key／notification_key 的值。
- 清理只針對本輪 ledger 記錄的精確目錄／PID／socket，不使用全域 prune 或 wildcard 刪除；18765 不應留下 listener。單元測試沒有 listener 時明記「未啟動」，不為了形式另外啟服務。

C／O／G 後續驗證命令如下；C／O／G 尚未實作或執行。S 組與先前完整回歸的結果另列於下方檢查點，不把過去結果當成後續新差異已驗證：

```sh
uv run --frozen pytest -q tests/test_delivery_claims.py tests/test_delivery_migration.py
uv run --frozen pytest -q tests/test_migration.py tests/test_notifications.py tests/test_notification_access.py tests/test_api.py
uv run --frozen pytest -q
uv run --frozen ruff check src tests
```

命令只可在上列實作 worktree 執行，沿用開工檢查的獨立 UV_PROJECT_ENVIRONMENT 與清除後的 Python 環境；不得使用日常 repo／.venv。依 frozen lockfile 安裝，不升級依賴。所有需要 listener 的測試先完成上述 18765 隔離設定；測試不得平行搶同一 port。回報包含新增與既有測試的 exit code、失敗原因、未執行項目及資源清理結果。若只能完成部分回歸，明列未執行項目，不能寫成全套通過。

## 實作交接與停止條件

使用者審查通過且 worktree／import 來源檢查全部成功後，Sonnet 子代理只能在 `feat/mailroom-delivery-phase1a-impl` 的隔離 worktree 執行上述 TDD 與驗證。主代理先審查 schema／資料契約的第一組差異，再審查 claim、outcome 與授權結果；不以子代理宣告通過替代獨立 readback。本次新增的本地 commit 授權僅限前述 12 個受審路徑；仍不得 push／merge、修改 BAT 或日常來源的產品程式。

執行規則（2026-11-09 使用者核准）：

- Sonnet 的每個 Bash 命令都以 `cd /Users/eason_tseng/playground/worktrees/agent-mailroom-phase1a && env -u VIRTUAL_ENV -u PYTHONPATH -u PYTHONHOME UV_PROJECT_ENVIRONMENT=/Users/eason_tseng/playground/worktrees/agent-mailroom-phase1a/.venv …` 開頭；不使用 Agent tool 的 `isolation: "worktree"`，也不自行另建 worktree。
- **檢查點**：S 組已完成審查與重估，1a 維持 1.4–1.9 pt，未觸發拆票。核准先進 C，之後依下列 O／G 條件順序續行；G 第一組授權差異完成即停止，交主代理審查撤銷一致性並重估。任何時點上界超過 2.0 pt 就拆票並回報。

### S 組檢查點：接受範圍與驗證來源

本次審查的是 `d1c2a51` 上尚未提交的 S 組差異：store.py、conftest.py、test_migration.py 及新增 test_delivery_migration.py。不是對 C／O／G 實作的驗收，也不代表 v7 已定案。

| 檢查 | 證據來源與結果 |
| --- | --- |
| import | 本次主代理工具實測，sys.prefix 為 worktree/.venv，store.__file__ 為 worktree/src/agent_mailroom/store.py。 |
| migration 測試 | 本次主代理在隔離環境重跑兩個 migration 測試檔，exit 0，27 passed，1.93 秒。測試與 cache 均位於本輪 TemporaryDirectory，結束後精確移除。 |
| ruff | 本次主代理重跑 ruff check src tests，exit 0。 |
| 完整回歸 | 使用者本次交接提供：pytest -q exit 0，306 passed，5 分 34 秒。本次主代理未再重跑完整套件，不將它描述為新的全套重跑證據。 |
| warning | 本次觀測到 Starlette 的 BlockingPortal／anyio deprecation，未見本次差異引入的新 warning。 |
| 測試 port | 本次只讀檢查 18765 未見 listener；本次 migration 測試沒有另啟 listener。 |
| 日常來源與服務 | 本次只讀核對日常來源只有 docs/plans/ untracked、無產品差異；PID 10459 的啟動時間仍為 2026-11-09 09:53:23。未停止、重啟或連線至日常服務／DB。 |

本次重跑所對應的 S 組產品／測試快照：

| 檔案 | SHA-256 |
| --- | --- |
| src/agent_mailroom/store.py | e6297f84ce543b31068d0f1be67450a7a7ccdcc1b15e7d9ad5f0d9ea41715506 |
| tests/test_delivery_migration.py | 339723954868b691a649442b6aabd5082af6f29bea9a6956d5528bc81d9bda7b |
| tests/conftest.py | 86e2ca01d08e4567f3b786b39d65f172cbf72d6d946506a75ff62449bdeecf14 |
| tests/test_migration.py | 9c03ca1829a3e5b84ae2c43890a9819b86e609033b7fa3dfc692a327784ed1da |

接受 S 組的最小 confirm_binding 作儲存支援，不把它當成 BAT 觀測或投遞者授權已完成；正式 preset／cwd／profile 核對及呼叫授權仍屬 1b／BAT 後續工作。接受 require_bindable 的局部抽取，前提為舊行為不變。claim_seq 主鍵、member_id 識別、晚到 legacy 與重跑語意依 B07 回補，不藉此擴大成新 HTTP 入口或成員憑證輪替。

### C／O／G 的續行指示

**C 組**：
- 核准現在依 C01–C10 用 TDD 實作 Store 層 claim／candidates，不新增 delivery HTTP route。C04 同時測精確 status_code 與 detail，未註冊 host 的 409／host_not_registered 也加入拒絕及無副作用測試。
- C10 的最新嘗試以 claim_seq 排序，按 room／member／message 範圍查核，不只查目前 binding_id。最新 claim 的 binding_version **大於等於**目前版本且仍為阻擋狀態時拒絕；較舊世代不可永久阻擋已明確 rebind 並確認的新世代。正向必測「舊 accepted 未 ack → 新世代 confirmed → 可 claim」，反向必測「同世代仍阻擋」及「新世代仍 pending 不可 claim」；released／acked 分支仍依原規格。
- C 組可用合法的 timeout unknown 前置資料測阻擋，不要為了 fixture 使用 reported 假原因。完成後提交定向測試、DB 前後斷言及差異，主代理讀回確認後按此計畫續做 O，不把 C 通過當作 O／G 通過。

**O 組**：
- `outcome_reported_at` 是 normal claim **是否已接收並定案 runtime 結果**的唯一標記；不以 unknown_reason='reported' 表示第二套定案狀態。O 組開工時從候選 v7 的原因集合移除 reported，寫測試防止再產生它。
- unknown_reason 保留成因：report_timeout、startup_recovery、legacy；正常首次回報 unknown 的一般原因採 runtime_unknown，不偽裝成 server 的 report_timeout。這是候選 schema 的原因分類補明，列入 B07，沒有新增 released 或自動重試資格。
- O09 首次晚到 unknown 後，outcome_reported_at 必須非空，原 report_timeout 原因保留；第二次不同結果不得改寫，相同重試仍冪等。正常 accepted／not_accepted 的首次回報同樣只記錄一次。
- 是否允許結果改寫還須符合 origin／state／原因規則：legacy 與 startup_recovery unknown 即使 outcome_reported_at 為空也不能接受新結果。它們不是已收到 runtime 結果，不能為了定案欄位偽造回報時間。

**G 組：單一撤銷權威及第一組審查點**：
- **權威選擇**：某通知 hash 有 delivery_claims 紀錄後，normal 與已轉換 legacy 的撤銷狀態只由該紀錄的 key_revoked_at 決定；grant 仍須符合既有 member 狀態與單訊息授權。只有沒有對應 claim 的未轉換通知，才沿用原 notifications JOIN bat_bindings 路徑。找到已撤銷／不合法 claim 後必須拒絕，不能再 fallback 舊 lookup。
- 遷移使用舊 binding 是否存在，將當時的既有失效結果寫入 key_revoked_at；這是一次性狀態轉換，不是之後每次授權各讀一半。legacy 一經轉換，授權查詢不再靠舊 JOIN 補出第二個撤銷結論。
- 為實際改目標的 bind_member、advance_binding_generation、leave 及 normal released 建立共用撤銷儲存操作。換世代／leave 以該 room／member 的先前世代為範圍，在刪除／取代 binding 的**同一交易**設定尚未撤銷 grant 的 key_revoked_at；released 只撤銷指定 claim 的 grant，不能擴成整位成員或其他訊息。保留原撤銷時間與 claim 歷史。不得因同目標冪等重試或 pause 就撤銷；未轉換舊模式沒有 claim row 時不改其既有行為。
- 第一組差異必測：舊模式未轉換仍走原路徑；轉換後有效／失效 grant 與原結果一致；rebind／leave 後舊 grant 永久失效、恢復 active 不復活；pause／同目標重試不撤銷；原成員憑證仍有效，released 不影響別的訊息／成員／room。另刻意建立「舊 JOIN 仍可找到，但 claim 已撤銷」的反例，證明不能靠 fallback 繞過撤銷。
- 用真實 Store 操作及另一條 connection 的交易前後快照，證明產品流程不會出現 binding 已刪但 legacy 撤銷欄位未更新的中間提交。不能只各測一條路徑就宣稱一致；不為人工直接破壞 DB 的情境新增整套修復框架。
- G 第一組授權差異與上述證據完成即停止，先由主代理檢查唯一權威、原子撤銷、G04 相容性及估算，再繼續其餘 G 工作；此檢查點已完成，結果與後續授權見下方修訂 5。若上界超過 2.0 pt，或不得不改變成員授權模型，依停止條件拆票／回報。

### G 第一組檢查點：核准剩餘工作

**本次決定**：接受 reconnect 呼叫共用撤銷儲存操作，也接受將共用 fixture 移入 conftest；核准 Sonnet 續做下列剩餘 G 工作與最終回歸。這不授權 1b、新 HTTP route、main 合入、日常部署或成員憑證輪替。

| 檢查 | 本次結果與證據範圍 |
| --- | --- |
| import | 主代理以隔離 UV_PROJECT_ENVIRONMENT 實測，sys.prefix 與 store.__file__ 都位於實作 worktree。 |
| C／O 與 G1 定向測試 | 主代理重跑 test_delivery_claims.py 與 test_delivery_grants.py，exit 0，88 passed，10.01 秒；TemporaryDirectory 中的 DB／cache 結束後精確清理。 |
| ruff | 主代理重跑 ruff check src tests，exit 0。尚有共用 fixture import 所需的 11 個 F811 抑制，後續整理需移除。 |
| 完整回歸 | 使用者提供最後一個 noqa 修改後的完整重跑：exit 0，394 passed，4 分 53 秒。主代理本輪未重跑全套，不把該結果當成新的完整重跑證據。 |
| prompt 抽取 | 主代理比對基準 d1c2a51 的 notification_prompt 與 prompt.py 的函式 AST，內容相同；notifications.py 保留匯入，舊呼叫端不改介面。 |
| 單一權威 | claim lookup 命中後已撤銷就直接 403，不 fallback；查無 claim 才走舊 JOIN。主代理已讀過反例與交易中斷測試。 |
| 四處刪除 | 主代理檢查 reconnect、leave、bind_member、advance_binding_generation 的四個 DELETE；皆先在同交易呼叫 revoke_claim_grants。normal released 另以指定 claim_id 撤銷。 |
| 日常環境／測試資源 | 使用者提供：日常來源只有 untracked docs/plans、editable .pth 未變、PID 10459 啟動時間未變、18765 無 listener。本輪未操作日常服務／DB；完成檢查後另做只讀來源與 port 核對。 |
| 估算 | S／C／O／G1 後仍為 1.4–1.9 pt，未見超過 2.0 pt 的證據；剩餘狀態序列化與回歸尚不能算完成。 |

本次定向測試的工作快照指紋為 `699d5a793be3e6fe9789779cc3510424488bd6b6af024b64135d3339e8bea4e8`：依序由 store.py、notifications.py、prompt.py、test_delivery_claims.py、test_delivery_grants.py、conftest.py 的相對路徑與 SHA-256 排序後，以其 Python repr 再取 SHA-256。這是未提交差異的讀回指紋，不是新 commit。

**接受 reconnect 撤銷呼叫的範圍**：
- 這是維持現有舊模式 takeover 後舊通知憑證永久失效的契約，並讓 claim 撤銷欄位與身分／binding 變更保持一致。它不是在新模式開放無 key reconnect；1b 仍須固定回 409／liveness_unknown。
- 保留房間／舊成員範圍、先撤銷再刪除或更換身分、同交易完成及 rollback 證據。不能只靠之後 member_id JOIN 不到舊成員，當成已維護 canonical 撤銷狀態。

**共用 fixture 整理**：
- 將跨 C/O/G 共用的 fixture 放入 tests/conftest.py；不再從另一個 test_ 模組 import env。若 Keys／Clock 等純 helper 也需共用，可放一個小型 tests/delivery_helpers.py，不能讓 conftest 反向 import 測試模組造成測試收集副作用。
- 移除這個 import 導致的 11 個 noqa: F811 及不再需要的 fixture F401 抑制。只移 fixture 與實際共用 helper，不順手重寫斷言、降低案例或大範圍整理其他測試。
- 單純搬移前後比較收集結果與定向測試，確認沒有案例遺失；功能新增的案例另列。不得藉著放入 conftest 改掉 18765、序列 listener 或 tmp_path 隔離規則。

**剩餘 G 工作與驗證**：
1. **notification_status 接入 claims**：已轉換／normal 的 pending_notification 不能仍只查 notifications。以實際 room／member、最舊未 ack 訊息及最新相關 claim 產生狀態；有 normal claim 而舊表沒有紀錄也要顯示。未轉換舊模式保留原路徑與必要欄位，不回報通知 hash 或其他憑證。
2. **回應型別與序列化**：檢查 models.NotificationStatus／NotificationAttempt、API response model 與 bridge model_validate 的消費端。released 狀態、runtime_unknown 原因、晚到 accepted 加 report_timeout 的歷史原因須能正確讀回；不能用假的 client_message_id／queued 填滿舊 DTO。若需要新型別／可選欄位，保持舊模式相容並加實際序列化測試，不能只驗證 Store dict。舊 worker_enabled 也不能被當成 BAT 模組存活證明；1b 的新模式診斷意義需再對齊。
3. **統一撤銷時鐘**：reconnect、leave、bind_member、advance_binding_generation 與 released 每處都明確傳入 self.stamp()；共用撤銷 helper 不再自行以模組 now() 代替。可將 at 改為必要 keyword，避免新增呼叫點漏傳。只整理撤銷時間，不全面改動既有訊息／成員的其他時間欄位。測試用受控時鐘斷言精確撤銷時間、先前時間不重寫，以及 released 只影響自己的 grant。
4. **維持唯一權威與 G04**：上述整理不得重新引入舊 JOIN fallback 繞過已撤銷 claim，也不能因 pause／相同目標重試就撤銷。四個刪除點與 released 都要保留同交易、同 room／member 範圍及舊模式回歸測試。
5. **最後驗收**：先跑受影響的定向測試與 lint，再對最終差異跑完整 pytest（不能把修改前的 394 當成修改後證據），檢查唯一已知 warning、18765、精確暫存資源清理與日常來源未變。主代理獨立讀回後再給 1a 最終結論；估算若超過 2.0 pt 仍須停止拆票。

B02／B03 的 host_id 邊界和 B07 的 detail／422／歷史 unknown_reason 說明是 1b 必做責任；1a 的 Store.record_outcome 刻意不要求目前 epoch，不能因此宣告其對外授權已驗收。

### 修訂估算與範圍移轉

原規格 `045e964` 的 1a 為 0.9–1.2 pt，1b 為 0.8–1.1 pt。本計畫不再把移入的工作藏在原估算裡：

| 移入 1a、從 1b 扣回的儲存工作 | 移轉工程量 |
| --- | --- |
| host_id／current_epoch、binding state／version／owner 及成員世代／遷移基準的 canonical schema 與資料契約 | 0.2–0.3 pt |
| 世代分配、持久保存最高版本、撤銷舊通知 grant 的原子操作與儲存測試 | 0.2 pt |
| 移轉合計 | **0.4–0.5 pt（4–5 小時）** |

正式 host 註冊、epoch 配發、投遞憑證驗證、confirm／reject／join_room 控制面、delivery HTTP routes 及端到端授權測試仍在 1b；這些不是移入 1a 的工作。HTTP 接線延至 1b 是上一輪已接受的切分，本表不把它再當成一次額外移轉。

| 票 | 原估算 | 儲存工作移轉後 | 新增隔離／凍結前驗證 | 修訂估算 |
| --- | --- | --- | --- | --- |
| Phase 1a | 0.9–1.2 pt | 加 0.4–0.5 → 1.3–1.7 pt | worktree／editable import 檢查及隔離驗證 0.1–0.2 pt | **1.4–1.9 pt（14–19 小時）** |
| Phase 1b | 0.8–1.1 pt | 減 0.4–0.5 → 0.4–0.6 pt | 最終 v7 與 1a／1b 整合回歸核對 0.1–0.2 pt | **0.5–0.8 pt（5–8 小時）** |

低／高兩端是對應的估算情境；同一情境的移轉在兩票等額對沖，移轉本身不增加總量。新增隔離與凍結前驗證才增加合計 0.2–0.4 pt。Phase 1 合計由原 1.7–2.3 修訂為 **1.9–2.7 pt**，兩票各自仍不超過 2.0 pt。其他階段不變時，整體待辦估算相應為 6.4–9.1 pt；這是待核定的計畫更新，本輪不改 BAT 的已提交規格。

這是未來實作與驗證的工程量，不含等候審查，不重複計算本次寫計畫，也不是以 34 個情境數量推算的保證。若 1a／1b 介面審查後任何一票的估算上界超過 2.0 pt，先拆票並回報，不能壓縮測試或自行擴大為完整控制面實作。

以下情況須先回報，不繼續依猜測實作：需要變更成員授權模型；需要取消 legacy 阻擋才能讓測試通過；需要借用日常 DB／8765；需要停止或重啟日常 server／bridge；import／editable 路徑指回日常來源或環境；需要在保護中的來源目錄執行 checkout／merge／uv sync 或修改產品程式；依賴真實 BAT 或模型回合才能驗證本階段儲存規則；schema／模式切換無法維持舊模式相容；需要讓非測試 DB 接受未定案 v7。Sonnet 的實作授權仍待本計畫審查與隔離檢查，不因規劃分支已建立而自動成立。

## 計畫修訂紀錄

- 修訂 1：建立 1a Store／遷移／outcome 範圍及 34 個基礎情境，正式 HTTP 授權留待 1b。
- 修訂 2（2026-11-09 本次討論結果）：將 worktree／獨立 editable 環境及日常服務保護列為 Sonnet 開工條件；選 merge 策略 (a)，1a 先 review、1b 疊接，共同合入前 v7 才定案。明列從 1b 移入的儲存工作與估算對沖、新增隔離／凍結前驗證成本、規格 30 項測試逐項責任，以及 C04 的 409／detail 契約。本輪仍只改計畫，沒有建立實作 worktree、安裝其環境或開始產品實作。
- 修訂 3（2026-11-09 本次討論結果）：使用者核准開工順序；新增 Sonnet 命令前綴規則、S 組完成後的審查與重估檢查點，以及 1b 必做項 B07（`binding_host_mismatch` 回補 BAT 規格）。
- 修訂 4（2026-11-09 本次討論結果）：記錄 S 組檢查點通過及 1a 重估仍為 1.4–1.9 pt；區分使用者提供的 306 項回歸與本次主代理重跑的 27 項 migration／ruff 結果。核准先進 C，明訂 C10 世代正反案例、O 的唯一結果定案欄位及原因保留、G 的已轉換 grant 單一撤銷權威與第一組差異停下審查。擴充 B07 收錄 claim_seq／claim_id、host_not_registered 等規格偏離；本次只更新計畫紀錄與交接條件，不修改產品程式或啟動子代理。
- 修訂 5（2026-11-09 本次討論結果）：G 第一組檢查點通過，1a 維持 1.4–1.9 pt；接受舊模式 reconnect 撤銷 claim grant 與共用 fixture 移入 conftest，核准剩餘 G 工作及最終回歸。列明 claims 狀態回應／DTO、撤銷時間來源、B02／B03 的 caller host_id 對 claim host_id 授權邊界，並擴充 B07 的十個 409 detail、兩個 422、晚到結果保留 unknown_reason 的意義。本輪主代理只更新計畫，不修改產品程式或啟動 Sonnet。
- 修訂 6（2026-11-09 本次討論結果）：1a 儲存層最終驗收通過，授權只在實作分支作本地 commit，包含受審計畫及明列的 12 個產品／測試／文件路徑，不 push、不進 main。記錄使用者的 414 項完整回歸與本輪主代理 135 項定向／lint／format 結果；接受舊模式三個 null 欄位及 takeover 後 pending_notification=None。新增 B08 與 4b 部署屏障：server／全部長駐 bridge 須先更新至相容版本、協同重啟並確認，之後才轉換或啟用新模式。v7 仍待 1b 整合後定案；本輪不執行 commit 或日常部署。
