# Mailroom 投遞 Phase 1b 實作計畫

日期：2026-11-10；修訂 5（1b-A checkpoint C 接收與 1b-B 授權）  
狀態：1b-A checkpoint C 接收，核定 1b-A 估算上界調整為 1.45 pt；可依跨 repo 隔離規則準備 1b-B worktree 並交由 Sonnet 做 B07。未具名回歸失敗仍未釐清；共同合入及 v7 定案尚未核准。本輪只審查及更新計畫，不修改產品程式或啟動 Sonnet。  
基準：agent-mailroom `6a43b4b750d6307200d219674110fcaf33abca88`；parent `d1c2a5153e4f777a775711f22a2fa53608dc5164`。  
分支：`feat/mailroom-delivery-phase1b-impl`。  
worktree：`/Users/eason_tseng/playground/worktrees/agent-mailroom-phase1b`。  
規格：BAT commit `045e964324624533c86e8ee5ec33ac5eca5c9cce` 的[草案 4](../../../../external/better-agent-terminal/plans/mailroom-delivery-plan.md)，以及本基準內受審的 [1a 計畫修訂 6](2026-11-09-mailroom-delivery-phase1a.md)。1a 文件保留當時紀錄，不回頭改寫成新的工作狀態。

## 1a 接收結果與範圍

- 已核對 1a parent、12 個路徑、3457 行新增／53 行刪除、commit 訊息沒有 Co-Authored-By，以及 clean worktree。src/tests 的 26 個 Python 檔指紋仍為 `83df1181e1d011440792aa140f3fb5eec5c32c6e7fede672191313ae17a9ff8c`，與已驗收結果相符；本輪沒有重跑 414 項回歸。
- 日常來源中的 untracked 1a 計畫副本，已先和 6a43b4b 的 blob 逐 byte 比對相同，再只刪除該精確檔案。以本地 commit 及各 worktree 的受版控文件為準，不把 auto memory 或已刪副本當成實作／授權依據。
- 本分支直接從 6a43b4b 疊接。1a 不單獨進 main，1a／1b 共同整合審查後才能核准合入；**v7 仍是候選**，只能操作可丟棄測試 DB，定案後才要求下一版 migration。
- 本次核定的是方向、分票、估算與下列技術條件；不等於授權日常部署。checkpoint A 的 CLI symlink 補正已核對，B 的四項決定依本修訂核定，可進 C。不得 push、merge、更新日常來源、讀日常 DB／credentials，或停止／重啟 server、bridge。真正的 BAT 投遞、ID 注入與原子 runtime 送出仍是 Phase 2／3；實機恢復與部署文件另有 Phase 4。

## 隔離環境與 Sonnet 條件

已用 `uv sync --frozen` 建立本 worktree 的 `.venv`，並實測 cwd、sys.prefix、agent_mailroom 與 store.__file__ 都指向本 worktree。沒有複用 1a 或日常來源的 `.venv`，沒有呼叫 initialize／serve 升級日常 DB，也沒有啟動測試 listener。

審查核定後，Sonnet 的每個 Bash 命令必須從這個前綴開始：

```sh
cd /Users/eason_tseng/playground/worktrees/agent-mailroom-phase1b && env -u VIRTUAL_ENV -u PYTHONPATH -u PYTHONHOME UV_PROJECT_ENVIRONMENT=/Users/eason_tseng/playground/worktrees/agent-mailroom-phase1b/.venv <command>
```

`<command>` 是待替換的命令，不原樣執行。不得使用 Agent 的 isolation: worktree 或自行另開 worktree。日常來源 `/Users/eason_tseng/playground/mcp-server/agent-mailroom`、1a worktree 與 BAT repo 是唯讀參考；BAT 的 B07 文件回補須另依本計畫的 1b-B 文件票核准後處理，不能先改規格來配合未審實作。

測試只用 tmp_path／本輪 ledger 精確記錄的暫存位置。真實 Mailroom HTTP listener 只用 `127.0.0.1:18765`，占用就停止回報，不改用 8765、不終止其他程序。預設優先 TestClient；真實 bridge 用 1b 自己的 Python、明確 URL 及暫存 state dir，不能呼叫日常 console script。輸出與證據不包含任何 credential 值、原始 Authorization frame 或含通知 key 的 prompt。

## 目標與最終驗收

1. 新模式有真正的 delivery credential 驗證與唯一 host 身分；成員／通知 credential 不能管理 delivery，delivery credential 不能直接充當成員身分。
2. 正式 host 註冊、pending／confirm／reject、candidates、claim、outcome 經 HTTP 使用 1a 的同一組儲存操作，不能另寫平行世代或阻擋規則。
3. outcome 對照 claim 保存的 host_id；不以目前 epoch 取代 host 身分核對。同 host 舊 epoch 的合法結果可回報，其他 host 不可改寫，即使它持有合法的另一張投遞憑證。
4. 新模式保留原成員 key rebind 與兩段式確認，無 key reconnect 固定回 409／liveness_unknown，不做存活證明或成員 key 輪替。
5. lifespan 接上啟動 recovery、運行期間 deadline 觀測與模式互斥；舊模式與正常成員收送信保持回歸相容。
6. B01–B08 各有對應測試與證據；舊／新 bridge 格式相容性及部署屏障明訂。這些測試不冒充日常部署、BAT 實機或模型行為驗收。

## 兩張票、依賴與核定估算

重新讀取 6a43b4b 後，不能把剩餘工作估成只有 HTTP 接線：目前沒有憑證 registry／rotation、new-mode CLI、控制面 DTO 或 lifespan 整合，既有 join 也仍要求舊 Remote adapter 驗證。先前 0.5–0.8 pt 的 1b 估算不足以完整涵蓋這些必做項目。

| 獨立票 | repo／交付與審查範圍 | 核定估算 |
| --- | --- | --- |
| 1b-A Mailroom 控制面與 HTTP | agent-mailroom repo：credential／mode／epoch、正式綁定控制面、HTTP 授權與 B01–B06／B08 整合測試、啟動／逾時／相容性回歸。獨立產品差異與驗收。 | 0.9–1.45 pt |
| 1b-B BAT 規格與部署條件回補 | BAT repo：B07 回補已審契約、B08 的 server／bridge 相容矩陣與啟用／失敗前置條件，作為 4b 輸入。獨立文件差異與驗收，不改 BAT 產品程式或操作日常環境。 | 0.3–0.5 pt |
| 合計 | 兩張票，不是同票的兩個工作包；不擴充到 BAT runtime 或實際 rollout。 | **1.2–1.95 pt（12–19.5 小時）** |

修訂 5 核定 1b 合計 1.2–1.95 pt；1a 維持 1.4–1.9 pt，因此 **Phase 1 合計為 2.6–3.85 pt**，比規格原估 1.7–2.3 pt 增加 0.9–1.55 pt；移轉的工作在兩票等額抵消，不增加總量；差額來自補足既有 1b 需求低估與隔離／整合驗證，不是擴充新功能。其他階段不變時，整體為 7.1–10.25 pt。Sonnet 自報 B、symlink 修正與 C 合計約 1.45 pt，未經獨立量測，記為自報工時，不寫成已驗證的實際工時；共同合入前若還有額外工作，再更新估算。

兩票分別有負責 repo、交付、review 與驗收；先審 1b-A 的實際契約，再由 1b-B 回補，兩票都通過才可共同合入與 v7 定案。1b-B 開始前由主代理另外準備 BAT 的文件分支／隔離 worktree，使用審查指定的規格 ref；不能把兩個 repo 當成同一分支，也不由 Sonnet 自行建立 worktree。本輪只在計畫列為兩張票，未在外部 tracker 建單。1b-B 只交付 API 契約與上述部署前置條件；完整遷移操作文件／演練仍是 4b，不把同一份文件編寫重複計價。

任一單票上界超過 2.0 pt 就先拆票，不能刪測試或隱藏授權工作。1b-A 上界本輪由 1.4 調整至 1.45 pt，不需拆票；此門檻適用單票，不是拿 Phase 1 合計與 2.0 pt 比較。既定單 DB 單 host 範圍不變。

## 控制面與儲存接線規則

### A. credential 與 mode

- 新增真正的 delivery credential principal，host_id 必須來自已驗證憑證對應的持久化資料，不能信任 request body 的 host_id。app 預設仍是既有模式；新 mode settings 是明確的選用參數，保留現有 create_app 呼叫方式。
- delivery token 只存 hash；檔案新建為 0600，既存檔的 ownership／權限／格式不符就明確拒絕。API、CLI、例外與 debug log 不輸出 token。產生／保留 host_id 與 current_epoch 的責任分開：首次正式 register 配發 epoch；credential rotation 保留 host_id，不重設 epoch 或 binding 版本。
- **已核定的分類策略**：delivery bearer 為 delivery_ 加 64 位小寫 hex；delivery 路由仍須核對完整格式及 registry 中 current hash。新增成員時，bearer hash 只要曾列入 delivery registry（含 revoked）就回 403／delivery_credential_reserved；既有成員不因此失效，未曾發行的同 prefix bearer 仍可註冊。這是 registry 分類，不能宣稱成員路由一律拒絕 delivery_。保留歷來 revoked hash，不改原成員 key／recover_session 規則。
- **單 DB 單 host 發行**：正式 CLI／首次發行只建立一個 host；已有 registry 但檔案遺失時明確拒絕，不另建 host。輪替只更新該 host 的 token，不改 host_id。這是本版正式支援範圍，不新增多 host 發行命令；授權層仍須防範不同 host 的交叉操作，不能因發行限制而移除 host_id 核對。
- 未識別／已撤銷的 delivery token 不得進入 delivery 儲存操作；相反方向的 member／notification token 也不准管理 delivery。錯誤 status／detail 在 DTO 與測試一起定案並列入 B07，不能讓沒有 auth 的入口先存在。
- serve 的 `--delivery-host` 與舊 BAT fingerprint 模式互斥，錯誤參數先拒絕，不先做 DB／檔案副作用。新的 credential 輪替命令只處理投遞憑證，不處理成員 key；測試必帶暫存 DB／file，不能用日常預設路徑。
- **本次選定的輪替順序**：先準備同目錄的私有暫存檔，DB 交易更新／提交有效 hash 後，再以 atomic rename 換入檔案。原規格第 8 節只定義輪替與 BAT 的 401／重讀行為，沒有指定 DB／rename 順序；本順序是本次實作決定，1b-B 要回補，不當成已有原文。
- DB 已提交而 rename 失敗時，不自動恢復舊 hash／舊 token、不換 host_id；本機 rotate 命令回報失敗，修正外部檔案問題後重跑同一 host 的 rotate。重跑不能要求檔案內的舊 token 仍有效，否則無法修復這個已知中斷狀態；沿用受控本機維運權限與安全檔案／host 定址，不新增網路恢復 API。檔案／DB 故障狀態依下表測，不再設計另一套自動回退機制。

### 四項簡化的決定與反例

| 項目 | 本次決定 | 檢查點／必要限制 |
| --- | --- | --- |
| delivery_ 格式分類 | checkpoint A 採 registry 分類；不把 prefix 單獨當作角色或成員保留字。 | current／revoked registry hash 阻止新註冊；保留既有同格式成員及未發行同 prefix bearer。delivery 授權另查完整格式及 current hash。 |
| DB → 檔案 rotation | 現在採用前述單向順序，失敗後以明確 rotate 重跑修復，不新增自動回退。 | 必須區分 DB 未提交、DB 已提交但檔案舊、檔案已換新等不同結果，不宣稱所有中斷必然 credential_rejected。 |
| request-time lazy expiry | 現在採用，candidates／claim／outcome／status 的合法入口觸發。 | 不建 expiry background task，不寫其取消測試；保留既有 server／listener／舊 worker cleanup 測試。啟動 recovery 仍是獨立必做。 |
| 舊 DTO 固定小型 fixture | 現在採用。 | 在測試內固定 d1c2a51 的 NotificationAttempt 定義並註記來源 SHA；測試執行不呼叫 git、不要求歷史物件／網路。只複製必要 model，不整份複製舊 models.py。 |

**checkpoint A 的 prefix 相容性決定**：主代理已用目前 API 與暫存 TestClient 證明：刻意選成 delivery_ 開頭的合法 bearer 可成功 create_room（HTTP 200），已測 43 字元的現行自動 token 長度，以及 delivery_ 加 64 位 hex、共 73 字元的候選完整格式，沒有讀日常 token，也沒有保存該測試值。它不代表日常已有這種 token，但證明舊契約不是只允許 bridge 隨機產生的值。

本輪讀取並重跑 tests/test_delivery_auth.py 的既有成員跨 mode、未發行同 prefix 新成員、current／revoked delivery bearer 拒絕註冊案例，據此核定 registry 策略，不以隨機碰撞機率取代相容性證據。日後若要把 prefix 改成保留字並使既有成員 key 失效，仍須另獲明確核准。本輪不讀取、盤點或輪替日常成員憑證。

**rotation 的故障分支**：

| 時點 | 應有結果 |
| --- | --- |
| 暫存檔準備失敗，或 DB 更新未提交 | 不變更有效 hash／既有檔案；舊憑證仍可用，不應預期 BAT 必然收到 401。清理本次精確暫存檔。 |
| DB 新 hash 已提交，但 rename 未成功／檔案仍舊 | 舊 token 不再有效；BAT 401 後重讀仍失敗才進 credential_rejected。rotate 回報失敗，重跑產生另一個新 token 並更新同一 host；不自動復活舊 token。 |
| 新 hash 與檔案都成功，但 BAT 還拿舊 token | BAT 首次請求可能 401，重讀新檔後可恢復；不是必然進 credential_rejected。 |
| 檔案遺失、不安全或 host 定址不明 | 明確停下回報，依既有 unavailable／insecure 規則處理；不能自動生成新 host 冒稱修復原身分。 |

### B. host／binding／claim／outcome

- host register 在同一交易把 current_epoch 單調增加；其他需要目前投遞者的操作檢查 host、epoch 和 binding owner。沒有註冊的 host 為 409／host_not_registered，舊 epoch 為 409／instance_superseded。
- confirm／reject 只改同一世代的 pending 狀態，不遞增版本，結果重試冪等，相反終態或舊版本回 409。host 需提供所觀測的 profile／runtime／preset／cwd，server 核對宣告與觀測的一致性；這是信任已授權 BAT 的觀測，不能宣稱 server 自己證明了程序存活。
- 缺少／找不到 terminal 不做死亡推論，維持 pending。只有明確 mismatch 才 reject。未確認 binding 不出現在 candidates，也不能手工繞過名單直接 claim。
- new-mode join_room 不再依賴舊 Remote adapter 才能登記 target，而是在 server 交易內驗證原成員 metadata／workspace／目標占用並建立 pending。新世代的三種入口、同目標重試、left 先恢復 active 等完全沿用 1a 計畫，不重新設計授權。
- 現有 Store.join／create_room 自己持有交易，advance_binding_generation 也會另開交易。不能在前一個交易內直接呼叫後者形成第二條寫連線。必要時抽出接受同一 conn 的最小內部操作，讓既有 public wrapper 與正式 registration 共用，維持「成員＋pending binding」同交易成功或 rollback；不加泛用交易框架。
- claims 使用已驗證 principal 的 host_id，並由 1a 交易再次核對 active／版本／confirmed／owner／epoch／最舊未 ack／阻擋。不得在 HTTP 層把 candidates 的舊結果當成已驗證。
- outcome 先核對合法 delivery principal 與**該 claim 保存的 host_id**，不改看目前 binding 的新 owner，也不以 body 提交的 host_id 作身分。有效的同 host 憑證可回報自己舊 epoch／舊 binding 的 claim；有效的其他 host 憑證仍須拒絕且沒有 DB／evidence／grant 副作用。Store 不檢查目前 epoch 的例外不等於跨 host 放行。
- 新模式合法無 key reconnect 一律 409／liveness_unknown，不進 takeover、不替換成員身分。原 key rebind 不撤銷成員 key，舊 runtime 的並行操作限制繼續明列。

### C. lifespan 與切換

- 在既有 server FileLock 下依序做 schema 初始化、啟動恢復與 mode 整合；不能在一般查詢或每次開 conn 時做 crash recovery。啟動的 normal submitting 先按 startup_recovery 處理，不能因它剛好逾 report_deadline 就誤變成可接受晚到結果的 report_timeout。
- **lazy expiry**：candidates、claim、outcome、status 在處理合法請求時呼叫 expire_overdue_claims，再執行原操作；不新增 expiry background task。先完成憑證、必要的 current host 檢查，以及目標 claim／room 的權限核對；跨 host outcome 或未授權請求不得藉此觸發 DB 維護寫入。
- HTTP candidates／status 因此可有到期維護的副作用，不能再宣稱完全唯讀；它們仍不建立 claim 或產生通知憑證。1a 的候選查詢本身與新增的請求前維護應分開驗證；測試區分既有到期列更新與禁止的新 claim／grant 副作用。
- expiry 在授權及必要的資源歸屬檢查後，以獨立交易提交；後續業務操作即使回 409，已提交的到期維護仍保留。這不表示 claim／outcome 的業務寫入也可部分提交，且未授權／跨 host 請求仍不得觸發維護。
- 受控時間驗證：長時間沒有請求後，第一個合法入口將逾時 submitting 轉 report_timeout；outcome 可接收一次晚到結果，原因保留、定案時間不重置。啟動 recovery 必須先於首次 request-time expiry，避免放寬 startup_recovery。沒有新 expiry task，就不加入其取消／清理測試；既有 server、listener 與舊 worker 的 cleanup 仍須保留。
- legacy 轉換只在明確啟用新模式時執行，轉換紀錄要持久化；轉換已開始但尚未完成時也不能放行舊模式。checkpoint A 列明 marker 與資料轉換的同交易或可恢復次序，故障重啟不能落入「資料已轉、marker 卻不存在」而放行舊 worker 的狀態。普通重啟不能重置 confirmed、世代、通知 grant 或重新排信；中途失敗的補救只使用 1a 已測的可重跑語意。
- **已核定的第一道保護**：新版程式發現 DB 有持久化的轉換紀錄，就拒絕以舊 Remote 模式啟動，在建立／refresh worker 前報錯；不自動反向轉換、不刪 marker 放行。這是「支援 v7 的新版程式選了舊模式」的風險；d1c2a51 本身在 user_version > 6 時已拒絕開啟，不會直接讀 v7 重送。
- **已核定的第二道保護（修訂 4 更正原 NOT EXISTS 寫法）**：先取該成員最舊未 ack 訊息；只要它已有任一 delivery_claims 紀錄就回 None，不往後挑選更新訊息。涵蓋 normal／legacy、released／unknown／accepted 等所有狀態，重試交回 delivery 路徑。保留 submit_notification 的送出前重查。原本先排除已有 claim 再排序的寫法可能跳過最舊訊息，故採此較保守的阻擋規則。
- 第二道測試必含：有 accepted normal claim 但沒有 notifications row 時舊路徑仍無候選；released 也不交回舊 worker；先取舊 candidates 再建立 claim，送出前重查不寫通知、不送 frame；沒有任何 delivery claim 的未轉換舊模式維持原行為。這是對「已有 claim 訊息」的額外防線，不取代啟動 mode guard，也不宣稱因此支援新舊 dispatcher 並行。
- 主代理先前只在暫存 DB 重現舊 next_notification 仍選到 accepted normal 訊息，未執行實際送出、BAT 或日常操作。本輪只讀確認 d1c2a51 的版本拒絕條件，沒有把日常舊 binary 的行為與新版舊模式混為一談。
- worker_enabled 保留舊 worker 語意；host register 成功不代表 BAT 在線，不能拿它假裝新模式 worker 存活。若需新診斷欄位，要標明它只表示 mode／binding 狀態並在 B07 對齊，保留舊 response model 相容。

## B01–B08：不得遺漏的驗證責任

1a 計畫的 R01–R30 歸屬表仍有效；1a 的 10 項純儲存責任、1b 的 4 項及兩邊共 16 項不能互相抵銷。1b 的測試必須通過真實 dependency／principal 驗證，不能以永遠成功的 auth stub 或直接 seed confirmed 狀態冒充已測完整控制面。

**B03 的限定 fixture 例外（本次討論結果，選項 a）**：允許測試在可丟棄 DB 的 delivery registry 插入第二個 host 及符合正式格式的測試 token hash。這只證明授權隔離，不證明支援多 host 發行／部署。第一個 host 的 register、binding／claim 要走正式 HTTP；第二個 host 的 register／outcome 也須通過真實 delivery dependency 及有效 hash 驗證，不直接塞入 confirmed／claim 或替換 principal。斷言同 host 舊 epoch 可回報，而另一個有效 host 回報相同 claim 被拒絕，且沒有 outcome／grant／expiry 副作用。

| 組別 | 1b 正反案例與必要斷言 |
| --- | --- |
| B01 無 key reconnect | 新模式合法請求固定 409／liveness_unknown，成員／binding／claims 不變；舊模式仍走原行為，不由此宣稱其存活判定可靠。 |
| B02 epoch／owner／claim | register 配發的真實 epoch；未註冊／舊 epoch／其他 binding owner 的精確錯誤；pending／confirm／reject／candidates／claim 逐入口核對；candidates → pause 後 claim 409、key factory 不呼叫，active 後同訊息可 claim。 |
| B03 credential 授權 | 缺 token、錯 token、撤銷 token、角色錯誤拒絕；同 host 舊 epoch outcome 可回報，另一個持有效 credential 的 host 不可回報。delivery token 不能直接作 member／notice 憑證讀寫，也不能先普通註冊繞過；member／notice token 不能管理 delivery。合法 claim 回傳訊息範圍 notice capability 是設計內行為，不把「不能直接使用 delivery token」誇大成 BAT 永遠拿不到訊息。 |
| B04 正式 binding 生命週期 | 真實 member registration／key rebind、pending／confirm／reject、同目標重試、rejected 重試、left／active、metadata／workspace／占用拒絕；失敗不能留下半個成員或 binding。 |
| B05 legacy 與跨世代 | 首次啟用、同版本確認不放行、orphan 世代、新 key rebind 確認後放行、原成員 key 仍有效；舊 normal outcome 不改新資格、不復活 grant，legacy outcome 不可改。 |
| B06 lifecycle／mode | 單一 server lock、啟動 recovery 先於 lazy expiry；四入口的受控時間到期觀測及授權前不寫 DB；模式 marker 與 next_notification NOT EXISTS 雙防線、送出前重查；重啟不重排、轉換故障／重跑不重送、未轉換舊模式回歸。保留既有 worker／listener 清理，不新增 expiry task。 |
| B07 契約回補 | 對照實作列精確 status／detail／DTO／CLI／schema 變動，依下節更新 BAT 文件，不改使用者已選方向；不靠散落註解代替規格。 |
| B08 bridge 相容與部署屏障 | 使用固定、註記 d1c2a51 來源的小型舊 DTO fixture，不在測試執行時讀 git 歷史：三個額外 null 欄位可讀、released／null client_message_id 拒絕；新 bridge 可讀新舊格式。server 與所有長駐 bridge 完成相容更新／重啟前，不得先 legacy 轉換或開新模式；本版不新增自動 bridge inventory／版本協商。 |

## B07 回補與 4b 部署交接

1b-B 文件票須以受審的 1b 實作為準，至少列明：

- claim_seq 是 SQL 主鍵、claim_id 為 NOT NULL UNIQUE 對外定址；世代識別為 room_id／member_id，晚到 legacy 的保守世代與可重跑限制。
- 409 的 host_not_registered、binding_host_mismatch 及 1a 新增十個 detail；422 的 unknown_outcome_result、released_requires_not_sent_evidence；其他 HTTP 驗證／授權錯誤也要明列，不能只宣告「沿用 Store 狀態碼」。
- outcome_reported_at 是是否接收結果的標記，unknown_reason 是歷史成因，accepted 也可能保留 report_timeout；列出跨 host 授權與同 host 舊 epoch 的區分。
- credential 發行／輪替／檔案故障、mode settings、正式 API DTO、worker_enabled 的實際意義，以及 startup／expire 的呼叫責任。
- registry 分類的既有成員相容性、單 DB 單 host 正式發行範圍。並行 rotate 的 rename 位於 DB lock 外，交錯時可能使檔案持有已撤銷 token；接受此可用性限制，維運不得並行輪替，等待其他 rotate 結束後明確重跑修復，不自動復活舊 token。此交錯由原始碼推得，本輪未實測並行 rotate。
- auth 的 notice_ 格式回 403／delivery_credential_required；其他格式錯誤依驗證階段回 401／delivery_credential_required 或 delivery_credential_rejected；revoked 與不存在同回後者，避免透露是否曾有效。
- --delivery-host 是布林旗標；--delivery-credential-file 預設在 DB 同目錄；rotate 不建立不存在的 DB。由 1a 候選 v7 已轉換、卻沒有 marker 的可丟棄測試 DB 需明確重跑 convert；不把這件事當成正式資料的自動修復功能。
- 明確的新模式啟用屏障：受控部署清單核對 server＋所有長駐 bridge 的來源及相容版本，協同更新／重啟完成後才轉換。1b 只交付測試與說明，日常部署及 v7 DB 回退需另獲授權。

auto memory 可以當索引，但規格、受審計畫、實際 commit／測試證據才是接手依據。本輪不修改 auto memory 或 1a 的已提交紀錄。

## 執行檢查點與停止條件

- **checkpoint A**：registry 分類、TDD 偏離的替代證據及單 DB 單 host 發行已核定；CLI symlink 補正已於修訂 4 核對，下列修訂 3 的反例保留為歷史證據。
- **checkpoint B**：正式 binding／claim／outcome、host 身分隔離、lifespan 與 mode 交易完成，交付 B01–B06 的實際 HTTP／DB 證據；不得用 auth fixture 繞過角色驗證。
- **checkpoint C**：B07 文件回補、B08 相容性、v6／舊版到最終 v7 升級、1a＋1b 全套回歸通過，再由主代理與使用者審查共同合入版本及 v7 定案；實際 merge／push 仍須授權。
- 需要使既有成員 key 失效的新授權規則、存活證明、BAT runtime 改動、多 host 發行、自動 client 註冊／協商或未審自動修復／回退設計時，先回報；需要日常 DB／8765／重啟、進 main 或任一票上界超過 2.0 pt 時也停止。兩道已核定 mode 防線不得因簡化而移除。

核定後的驗證：新 B 組定向測試、既有基準的受影響回歸，最後對最終差異跑 `pytest -q -rw`、`ruff check src tests`、`ruff format --check src tests`；全部命令使用上述 worktree／env 前綴。報告分開記錄親跑與他人提供的證據、測試／format／lint exit code、唯一已知 warning 或新 warning、18765、精確暫存資源清理及日常來源未變。checkpoint A 的本輪證據如下，不宣稱 B／日常部署已通過。

### checkpoint A 審查證據與補正（修訂 3 當時紀錄）

- 使用者提供：完整 pytest -q -rw 為 487 passed、4 分鐘，只有已知 Starlette warning；lint／format 成功，日常環境未改。本輪 Codex 沒有重跑完整 487 項。
- Codex 親跑（使用上述 env 前綴）：uv run --frozen pytest -q -rw tests/test_delivery_auth.py tests/test_delivery_credentials.py tests/test_delivery_mode_marker.py，exit 0，73 passed，3.81 秒，只有 Starlette BlockingPortal deprecation。ruff check src tests 及 ruff format --check src tests 均 exit 0，30 個檔案格式已符合。
- **TDD 偏離選 a**：marker 有先失敗再通過紀錄；credentials／auth 先寫測試、緊接實作，缺少實作前的逐項失敗紀錄。接受使用者提供的四組 mutation 檢查（刻意植入錯誤，確認測試能抓到）：只看 prefix、rename 提前至 DB commit 前、移除 guard、marker 改至別筆交易，作為本次替代證據。不把它寫成補回了歷史 TDD 順序，也不為追補紀錄重做實作；本輪未取得或重跑四組 mutation 的原始產物。後續改動仍先跑出意圖對應的失敗測試再實作。
- **進 B 前須修正**：src/agent_mailroom/cli.py 的 rotate／serve 都先對 credential 路徑呼叫 resolve()，已解析掉最後一段 symlink；delivery.py 的 O_NOFOLLOW 因此看不到原來的 symlink。Codex 在 TemporaryDirectory 內建立測試 DB、0600 真實憑證檔與指向它的 alias；直接 rotate_credential(store, alias) 拒絕，但帶同一 alias 呼叫 cli.main 的 rotate 命令成功，且目標檔已變更。這違反既有「憑證路徑是 symlink 就拒絕」規則，不能只記成部署限制。

```text
direct_rotate_symlink=rejected
cli_rotate_symlink=accepted
target_file_changed=True
alias_remains_symlink=True
scratch_cleanup=complete
```

先為 serve 與 rotate 的 CLI 入口補上 symlink 反例測試；指向既存檔的案例須先在目前程式因接受 symlink 而失敗，另補 dangling symlink 的拒絕回歸。再用最小修正保留最終 credential 路徑供安全開啟檢查。拒絕時不得發行／輪替憑證、變更目標檔或 registry；正常路徑及 DB-first 故障分支維持。A 組與相關 CLI 回歸、lint／format 通過並提供差異核對後，依本次條件授權進 B，不擴大檔案系統安全模型。本輪 probe 的暫存目錄已清理，未留下憑證或 log。

本輪唯讀檢查：日常 repo 與 1a worktree 的 git status --short 無輸出；日常 .pth 仍指向原 src；PID 10459 啟動時間仍是 2026-11-09 09:53:23；18765 的 lsof 沒有 listener（exit 1）。未連線 8765、未讀日常資料或重啟程序。

### checkpoint B 決定、驗證與 C 條件（2026-11-10 本次討論結果）

四項均核定：

1. Guard 2 採「最舊訊息有 claim 就阻擋」，不跳過它送更新訊息。測試涵蓋多訊息、released 與送出前競態，不能宣稱支援新舊 worker 並行。
2. lazy expiry 可獨立提交，後續 409 不回滾維護；業務交易及權限限制維持。Codex 暫存 TestClient 探針重現：先建立合法 claim，將 deadline 設為過期，再以有效 host 對同 binding 送錯版本 claim，回 409，原 claim 已持久化為 unknown／report_timeout。暫存資源已清理，未輸出憑證。
3. delivery 模式略過 recover_notifications：先 recover_delivery_claims 將原生 submitting 設為 unknown／startup_recovery，再 conversion 將舊 notifications 的 submitting 匯入 legacy unknown，之後才發行憑證。舊 notifications 表可仍保留 submitting 原值；轉換後以 claim 為準、舊模式受 marker 阻擋，因此不必為了投遞再改舊表。舊模式仍保留原 recovery。
4. 接受修正 6a43b4b 的 conversion：binding 輸入限 binding_state IS NULL，避免原生 delivery binding 在重啟時被凍結 migration baseline；notifications 的 legacy／orphan 輸入仍保留。這是疊加於 1a commit 的修正，不改寫 1a 歷史、不修改已定案 migration；v7 仍是候選。重啟快照與原 1a 遷移測試均需維持。

profile／session 不符回 observation_not_for_binding 並維持 pending，符合原規格的「已存在的同一 terminal 才能以明確 mismatch reject」，不視為新增偏離。RUNTIME_PRESETS 的兩份定義列為待清理；本輪不擴大重構，但 C 的 B07 要列出重複位置、依賴原因、尚無自動一致性保證。新增 detail 及 last_error 格式也須回補。

驗證分層記錄：使用者提供完整 549 passed 兩次（4:14／3:57）及回改 resolve() 後三項失敗、修正後五項通過的替代反例證據；Codex 本輪沒有重跑完整套件或該 mutation。Codex 親跑 credentials／HTTP／lifecycle／mode marker／migration 五檔，126 passed、13.93 秒；另跑現行 auth 模組，33 passed、5.66 秒，合計 159 項通過。ruff check 及 format --check 均 exit 0，33 個檔案，只有已知 Starlette warning。

有一次完整回歸失敗未具名，原因仍未知，不能歸因於既有測試不穩定。當前 lastfailed 只有已更名的舊 A 測試節點 test_delivery_mode_adds_exactly_one_route，無法證明它就是該次失敗；以此舊節點定向執行會 exit 4（找不到測試），已改跑現行 auth 模組，不能當作產品測試失敗或根因已定位。

C 可開始 B08 與最終整合驗證；1b-B 仍依既定跨 repo 隔離規則另行準備 BAT 文件 worktree。每次完整回歸保留去除憑證後的完整輸出、精確命令與退出碼，包含 -rfE 的失敗名稱／堆疊，不只擷取尾端。若再失敗，先定位並記錄，不以重跑成功取代失敗證據；若無法重現，最終報告仍須保留本次未定位失敗的限制。

1b-A 維持 0.9–1.4 pt；約 1.2 pt 是 Sonnet 自報，未經獨立驗證，不當成已量測工時。依本次承諾，預期超過 1.4 pt 就先回報；2.0 pt 的拆票上限及混合 scope 分票規則不變。

本輪日常 repo git status --short 無輸出，18765 沒有 listener。與使用者提供的程序觀測不同，本輪 ps -p 10459 查無該 PID（exit 1），因此不能重新確認其啟動時間或服務連續性；未查讀日常 DB、連線 8765 或停止／重啟服務。本輪只改本計畫，未 commit、push 或啟動 Sonnet。

### 1b-A checkpoint C 接收與 1b-B 授權（2026-11-10 本次討論結果）

- 接收本次 1b-A 的 B08 與最終候選 v7 回歸；不等於 1b-B 完成、共同合入、日常部署或 v7 已定案。C 新增九項測試保護既有行為，記為回歸測試，不補寫成歷史 TDD。四組 mutation 是 Sonnet 回報，本輪未獨立重跑。
- Codex 親讀 tests/test_bridge_compat.py 與 tests/test_delivery_upgrade.py；親跑 uv run --frozen pytest -q -rw -rfE tests/test_bridge_compat.py tests/test_delivery_upgrade.py，exit 0，9 passed、1.78 秒，只有已知 Starlette warning。ruff check／format --check 均 exit 0，35 個檔案。
- 親讀 /tmp/mailroom-1b-c/main-1.log、full-1.log、full-2.log，摘要分別為 558 passed（314.46／298.72／268.68 秒）；指定的 notice／delivery／session／Bearer 憑證格式掃描皆 0 筆。這是讀取既存日誌，本輪沒有重跑完整 558 項；格式掃描不代表能偵測任意形狀的秘密。
- 親自將 6a43b4b 與目前 SCHEMA 分別載入記憶體 SQLite，比對 sqlite_master：僅新增 delivery_credentials、delivery_mode、delivery_credentials_current_host，沒有既有物件刪除或 SQL 定義變更，因此刪除這些物件的候選 fixture 在目前版本成立。日後 schema 有變動須重新比對，不能把模擬 fixture 說成由真實舊版程式產生的 DB。
- 舊 DTO fixture 的欄位／預設值已對照 d1c2a51；NotificationStatus 的 CollaborationState alias 展開後為相同的 active／paused／left Literal。測試執行不讀 git 歷史；本輪比對是在測試之外進行。
- B 那次未具名失敗仍未定位，保留使用者提供的後續七次完整回歸成功紀錄，不宣稱原因已排除或既有測試不穩定。後續仍保留完整且去除憑證的日誌；若再失敗，先保留證據再定位。
- **核准準備 1b-B 並交由 Sonnet 做 B07**：由主代理依第 52 行既定規則，在 BAT 另建文件分支與 worktree，以完整 ref 045e964324624533c86e8ee5ec33ac5eca5c9cce 為規格基準，先驗證 ref／cwd；分支或路徑已存在時停下核對，不覆蓋。Sonnet 不另建 worktree、不修改產品程式，僅在該文件 worktree 編修規格與部署前置條件。原 BAT 的 untracked issue 文件不帶入、不刪除；不 commit／push／merge／stash，也不操作日常服務。
- B07 以本輪受審的 Mailroom 程式與前述已核定決定為準，須回補精確 detail／DTO、confirm／reject 檢查順序、Guard 2、expiry 的交易副作用、registry／單 host／輪替限制、RUNTIME_PRESETS 重複位置與依賴原因，以及 server＋所有長駐 bridge 更新／重啟後才切換的部署屏障。只記錄已審契約及限制，不擴充產品需求或在文件中預先宣稱部署驗收。
- Mailroom 仍無 1b commit：本輪 src/tests 共 35 個 .py 的快照指紋為 f3ec500349b908743e73bfe0ea4661438e4b8c7b1d72cd7a2e4f81e88d5cfbe0；算法為各相對路徑對應 SHA-256，排序後 repr 再計算 SHA-256。B07 交付時核對此指紋，若產品內容改動則重新審受影響契約；不能只靠 HEAD 6a43b4b 識別未提交的 1b 實作。
- 本輪日常 repo 乾淨；PID 10205 為 2026-11-09 23:31:03 啟動；18765 無 listener。BAT HEAD 為上述 045e964，只有既有 untracked issue 文件。只進行唯讀檢查及本計畫編修，沒有建立 BAT worktree、啟動 Sonnet、commit、push、stash 或操作服務。

## 修訂紀錄

- 修訂 1：從受審 6a43b4b 準備 1b worktree／獨立 uv，列 B01–B08 與初估 1.2–1.9 pt；以暫存 DB 確認新版舊 worker 對 normal claim 的選信風險。
- 修訂 2（2026-11-09 本次討論結果）：核定 1.2–1.9 pt，明確拆成跨 repo 的 1b-A、1b-B 兩張票，Phase 1 合計為 2.6–3.8 pt；增加啟動 marker＋舊候選 NOT EXISTS 雙防線。現在採用 DB-first／atomic rename 輪替、合法入口 lazy expiry 與固定小型舊 DTO fixture；prefix 及既有同格式成員身分的相容策略留 checkpoint A。補正 rotation 不同中斷狀態、expiry 的授權順序與維護副作用。本輪只改計畫、作暫存 TestClient 相容性探針，不改產品程式或啟動 Sonnet。
- 修訂 3（2026-11-09 本次討論結果）：核定 registry 分類、接受 mutation 作為本次 TDD 偏離的替代證據，以及單 DB 單 host 發行；明列 B03 第二個 registry host 的限定 fixture 例外，仍走真實 HTTP 授權。1b-A 維持 0.9–1.4 pt，B07 增列並行 rotate、錯誤碼、CLI 預設與舊候選 v7 marker 條件。分開記錄使用者完整 487 項與 Codex 親跑 73 項證據；新增已重現的 CLI symlink 缺口及進 B 前補正條件。本輪只修改本計畫，不修改產品程式、commit 或啟動 Sonnet。
- 修訂 4（2026-11-10 本次討論結果）：核定 Guard 2 保留最舊訊息阻擋、expiry 獨立提交、delivery 略過舊 recovery，以及 conversion 只納入 legacy binding 的修正；核對 CLI 保留最終路徑。授權依隔離條件進 C，補上完整失敗證據保留、未具名失敗限制及 1.4 pt 回報點；保留原 1a／A 歷史與 v7 候選狀態，記錄當前查無 PID 10459 的證據界線。
- 修訂 5（2026-11-10 本次討論結果）：接收 1b-A checkpoint C；上界調整為 1.45 pt，註明自報未量測，更新 1b、Phase 1 及整體估算。記錄九項親跑、三份日誌、schema／DTO 比對與未定位失敗限制；核准依固定 BAT ref 建立 1b-B 文件 worktree 並交付 Sonnet 做 B07，保留產品快照、共同合入及 v7 尚未定案等界線。
