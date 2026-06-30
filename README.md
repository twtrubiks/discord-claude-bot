# Discord Claude Bot

這是一個 Discord 私人助理機器人，基於 [openclaw](https://github.com/openclaw/openclaw) 概念重新實作精簡版。

專為個人使用設計，具備對話記憶與排程提醒功能，讓 Claude 成為你的專屬 AI 助手。

* [Youtube Tutorial - OpenClaw 啟發 - Discord + Claude AI 整合實戰](https://youtu.be/2UwnqbQsRyY)

* [Youtube Tutorial - 一支影片看懂 Moltbot：架構、爭議、自己做一個](https://youtu.be/UGR7Z4yKGhE)

* [Youtube Tutorial - 免費又快！Groq Whisper 語音轉文字整合 Discord Claude Bot 實戰](https://youtu.be/P2Oovw_YZkY)

## 功能

- 與 Claude AI 對話（使用 Claude Code 訂閱額度，無需另購 API Key）
- 即時串流回覆（token-level streaming，逐步顯示回應）
- 對話歷史管理與自動摘要
- 長期記憶（跨對話記住用戶偏好與重要資訊）
- 排程任務（一次性提醒、定期觸發、每日排程）
- 語音訊息轉文字（透過 Groq Whisper API 轉錄後自動交給 Claude 回應）

## 安裝

```bash
pip install -r requirements.txt
```

## 環境設定

複製範例檔並填入你的設定：

```bash
cp .env.example .env
```

各變數說明：

| 變數 | 必填 | 說明 |
|------|:----:|------|
| `DISCORD_BOT_TOKEN` | 是 | 從 [Discord Developer Portal](https://discord.com/developers/applications) 取得 |
| `ALLOWED_USER_IDS` | 否 | 用戶白名單，逗號分隔；留空代表所有人可用 |
| `GROQ_API_KEY` | 否 | 語音轉文字功能，從 [Groq Console](https://console.groq.com/) 取得，未設定時語音訊息僅存檔 |
| `DISCORD_GUILD_ID` | 否 | 伺服器 ID，設定後 `/help` slash command 即時生效（不設則需等最多 1 小時） |
| `MAX_MESSAGES_BEFORE_COMPRESS` | 否 | 達到此訊息數時觸發自動壓縮（預設 16） |
| `STREAM_ENABLED` | 否 | 串流輸出開關（預設 `true`，設為 `false` 改回等待完整回應） |
| `CLAUDE_MODEL` | 否 | 指定 `claude -p` 使用的模型，可用別名 `opus` / `sonnet` / `haiku` 或完整 ID（如 `claude-opus-4-8`）；留空使用 CLI 預設模型 |
| `CLAUDE_LIGHT_MODEL` | 否 | 輕量任務（摘要壓縮、cron 標題生成）使用的模型（如 `haiku`），省成本；未設定時退回 `CLAUDE_MODEL` |
| `CLAUDE_EFFORT` | 否 | `claude -p` 的推理強度，可用 `low` / `medium` / `high` / `xhigh` / `max`；越高思考越深、token 與成本越高；未設定時預設 `xhigh`。Haiku 不支援 effort，使用 Haiku 模型時會自動略過 |
| `CLAUDE_TIMEOUT` | 否 | 排程任務（cron）單次 `claude -p` 執行超時秒數；未設定時預設 `1800`（30 分鐘）。排程重活在 `xhigh` 下常超過 10 分鐘，可視需要加大 |

## 使用方式

啟動機器人：

```bash
python bot_discord.py
```

### 指令

**對話**
- `/help` - 顯示說明
- `/new` - 保存記憶並開始新對話
- `/clear` - 清除對話歷史和摘要（保留長期記憶）
- `/context` - 查看上下文狀態
- `/summarize` - 手動生成摘要
- `/summary` - 查看當前摘要

**記憶**
- `/memory` - 查看長期記憶
- `/forget` - 清除所有長期記憶
- `/forget <編號>` - 刪除特定一條記憶
- `/recall <關鍵字>` - 搜尋已封存的歷史對話（即使已被摘要壓縮丟棄，原文仍可搜尋）

**排程**
- `/remind <時間> <訊息>` - 一次性提醒（如 `/remind 30m 開會`，到時觸發 Claude）
- `/every <間隔> <訊息>` - 定期觸發（如 `/every 1h 喝水`）
- `/daily <HH:MM> <提示>` - 每日觸發 Claude（如 `/daily 09:00 今日新聞`）
- `/cron list` - 列出所有排程
- `/cron info <id>` - 查看任務詳情（含完整提示詞，過長自動分段）
- `/cron remove <id>` - 刪除任務
- `/cron toggle <id>` - 啟用/停用任務
- `/cron test <id>` - 立即執行測試

## 架構

```
Discord 使用者發訊息
        │
        ▼
on_message() 接收
        │
        ├─ 是 bot 自己？   → 忽略
        ├─ 不在白名單？    → 回應「未授權」
        ├─ 語音訊息？      → 語音處理流程（見下方）
        ├─ 空訊息？        → 忽略
        ├─ 特殊指令？      → 對應處理
        │   (/help, /new, /clear, /context, /summarize, /summary,
        │    /memory, /forget, /recall, /cron, /remind, /every, /daily...)
        │
        ▼
ask_claude_with_lock(user_id, message)
        │
        ├─ 組合上下文（長期記憶 + 摘要 + 歷史 + 自動召回封存）
        │
        ▼
STREAM_ENABLED?
        │
        ├─ true（預設）→ ask_claude_stream()
        │     │
        │     ▼
        │   asyncio.create_subprocess_exec(["claude", "-p", ..., "--output-format", "stream-json", ...])
        │     │  逐 token 讀取 NDJSON，每秒編輯 Discord 訊息
        │     │  （含重試機制：最多 3 次）
        │     ▼
        │   串流結束 → 儲存對話歷史
        │
        └─ false → ask_claude()
              │
              ▼
            subprocess.run(["claude", "-p", prompt, ...])
              │  等待完整回應（含重試機制：最多 3 次）
              ▼
            取得回應 + 儲存對話歷史
              │
              ▼
            chunk_message() 分塊處理（代碼塊感知）
```

## 語音訊息轉文字

Discord 語音訊息會自動轉錄為文字，再交給 Claude 回應。使用 [Groq](https://console.groq.com/) 提供的 Whisper API，免費方案檔案上限 25 MB。

### 轉錄設定

| 參數 | 值 | 說明 |
|------|-----|------|
| 模型 | `whisper-large-v3` | OpenAI Whisper 大型模型，由 Groq 託管 |
| 語言 | `zh` | 指定中文，提高辨識準確度 |
| prompt | `以下是繁體中文的語音內容` | 引導模型輸出繁體中文而非簡體 |
| 回應格式 | `verbose_json` | 包含時間戳等詳細資訊 |

> **關於繁體中文輸出**：Whisper 模型預設可能輸出簡體中文，透過設定 `prompt` 參數為繁體中文提示語，可以引導模型優先輸出繁體中文。

### 流程

```
收到語音訊息
    │
    ▼
儲存 .ogg 到 voice_messages/
    │
    ▼
有 GROQ_API_KEY？
    │
    ├─ 否 → 回覆「語音已儲存（未設定 Key）」
    │
    └─ 是 → Groq Whisper 轉錄
              │
              ├─ 失敗 → 通知使用者，語音檔保留
              ├─ 結果為空 → 提示無法辨識
              └─ 成功 → 顯示轉錄文字
                        │
                        ▼
                  ask_claude() 回應
```

### 獨立使用轉錄工具

`speech_to_text.py` 也可以作為獨立 CLI 工具使用：

```bash
# 轉錄既有音訊
python speech_to_text.py transcribe voice_messages/xxx.ogg

# 錄音再轉錄
python speech_to_text.py record 10
```

## 上下文記憶

實作了一套上下文記憶系統，讓 Claude 能夠記住與每位用戶的對話歷史。

### 資料結構

- `Message` dataclass：儲存單條訊息（role, content, timestamp）
- `ConversationState` dataclass：儲存對話狀態（summary 摘要 + messages 最近對話）
- `conversation_states`：以 user_id 為 key 的字典，每個用戶擁有獨立的對話狀態
- `user_memories`：以 user_id 為 key 的字典，儲存每個用戶的長期記憶條目（程式欄位名為 `facts`）

### 對話格式設計

`Message` 中的 `role` 欄位採用 LLM 業界標準格式：

| Provider | User 角色 | AI 回應角色 |
|----------|----------|------------|
| OpenAI (GPT) | `user` | `assistant` |
| Anthropic (Claude) | `user` | `assistant` |
| Google (Gemini) | `user` | `model` |

這是多輪對話的本質需求：LLM 需要知道「誰說的」、「說了什麼」、「什麼順序」。

#### 實際送給 Claude 的格式

```
Current Date: 2026-02-15 Sat 10:30 (Asia/Taipei)

---

你是一個 Discord 上的個人 AI 助手...（系統 prompt）

---

[Long-term memory about this user]
- 用戶是軟體開發者，主要使用 Python 和 Docker
- 用戶對台股分析有興趣

---

[Previous conversation summary]
用戶偏好簡潔回答，之前討論過 Python 專案...

---

[Recent conversation]
User: 你好
Assistant: 你好！有什麼可以幫你的？
User: 幫我寫一個函數

---

使用者目前的訊息：
這個函數要能計算費氏數列
```

#### 多輪對話的已知限制

根據 [LLMs Get Lost In Multi-Turn Conversation (2025)](https://arxiv.org/abs/2505.06120) 研究：

- LLM 在多輪對話中效能平均下降 **39%**
- 頻繁切換主題可能導致**上下文混淆**和**幻覺**
- 一旦走錯方向，LLM 難以自我修正

**建議**：切換不同主題時，使用 `/new` 開始新對話，會自動保存重要資訊到長期記憶，避免不相關的上下文干擾。

### 持久化

- `save_history()`：將對話狀態序列化為 JSON，存到 `conversation_history.json`
- `load_history()`：啟動時載入歷史記錄（支援新舊格式相容）
- `save_memory()`：將長期記憶序列化為 JSON，存到 `memory.json`
- `load_memory()`：啟動時載入長期記憶

以上 JSON 寫檔皆走 `storage_utils.atomic_write_json()`（先寫同目錄 temp 檔，`os.fsync` 落盤後再 `os.replace` 原子置換），讀者永遠只會看到舊的或新的完整檔，寫一半當機也不會損毀原檔。對話原文另以 append-only 永久封存到 SQLite（見「對話封存與召回」）。

### 限制參數

| 參數 | 值 | 說明 |
|------|-----|------|
| `MAX_CONTEXT_CHARS` | 8000 | 送給 Claude 的上下文最大字元數 |
| `MAX_SUMMARY_CHARS` | 2000 | 摘要最大字元數 |
| `MAX_MEMORY_FACTS` | 20 | 每用戶最多保留的長期記憶條目數量 |
| `MAX_MEMORY_CHARS` | 1500 | 記憶注入上下文的最大字符數 |
| 壓縮觸發門檻 | 預設 16 條訊息 | 超過此數量觸發自動壓縮（可由 `MAX_MESSAGES_BEFORE_COMPRESS` 調整） |

### 運作流程

```
新對話進入
    │
    ▼
儲存到 messages[]
    │
    ▼
組合上下文送出 ◄─── 限制：最多 8000 字符
    │                 （從最新往回取）
    │                 包含：長期記憶 + 摘要 + 最近對話
    ▼
檢查 len(messages) >= MAX_MESSAGES_BEFORE_COMPRESS（預設 16）？
    │
    ├─ 否 → 繼續
    │
    └─ 是 → 觸發壓縮
              │
              ├─ 取最舊 10 條訊息
              ├─ 呼叫 Claude 生成摘要 + 萃取長期記憶條目
              ├─ 合併摘要到現有 summary
              │     │
              │     └─ 超過 2000 字？→ 再壓縮
              │
              ├─ 合併長期記憶條目到長期記憶 (memory.json)
              │
              └─ 保留未摘要的其餘訊息（預設門檻 16 時通常剩 6 條）
```

## 長期記憶

除了 session 內的摘要壓縮，本專案還實作了跨對話的長期記憶系統。

### 三層記憶架構

| 層級 | 用途 | 生命週期 | 儲存位置 |
|------|------|----------|----------|
| **摘要 (summary)** | 單次對話內的上下文壓縮 | `/clear` 或 `/new` 時清除 | `conversation_history.json` |
| **長期記憶 (memory)** | 跨對話的用戶長期資訊 | 永久保留，需 `/forget` 清除 | `memory.json` |
| **對話封存 (archive)** | 每則對話原文，供搜尋與自動召回 | append-only 永久保留 | `sessions.db` (SQLite+FTS5) |

### 記憶如何產生

長期記憶主要在摘要流程中提取；另外，當 `/new` 符合條件時，會額外呼叫一次 Claude 來提取長期記憶條目：

1. **自動壓縮時**：當訊息數 >= `MAX_MESSAGES_BEFORE_COMPRESS`（預設 16）時，`generate_summary()` 會同時產生摘要和長期記憶條目
2. **手動 `/summarize` 時**：同樣會順便萃取長期記憶條目
3. **`/new` 時**：如果當前有 >= 4 條訊息，會呼叫一次 Claude 萃取長期記憶條目後再清空

### 記憶儲存格式

`memory.json`：

```json
{
  "825626482582749194": {
    "facts": [
      "用戶是軟體開發者，主要使用 Python 和 Docker",
      "用戶對台股分析有興趣",
      "用戶偏好繁體中文回應"
    ],
    "updated_at": "2026-02-15T10:30:00"
  }
}
```

### 記憶管理

- **去重**：僅做「完全相同字串」去重，不做子字串或語意去重
- **上限**：每用戶最多 20 條長期記憶條目，超過時淘汰最舊的
- **手動管理**：
  - `/memory` 查看所有記憶（帶編號）
  - `/forget 3` 刪除第 3 條
  - `/forget` 清除全部

### `/new` vs `/clear`

| | `/new` | `/clear` |
|---|---|---|
| 清除對話歷史 | 是 | 是 |
| 清除摘要 | 是 | 是 |
| 萃取長期記憶條目 | 是（>= 4 條訊息時） | 否 |
| 清除長期記憶 | 否 | 否 |
| 適用場景 | 切換話題（推薦） | 完全重置對話 |

## 對話封存與召回

摘要與長期記憶都是「壓縮後」的資訊——摘要會丟棄原始字句，超過上限的記憶條目也會被淘汰。為了讓舊對話原文不流失，每則訊息都會 append-only 永久封存到 SQLite（`memory_store.py`），即使之後被摘要壓縮丟棄，原文仍可搜尋與召回。

### 儲存與索引

- **`messages`**：唯一真相（canonical），永久保存每則對話原文 + metadata。
- **`messages_fts`**：FTS5 unicode61 分詞，給英文/數字關鍵字（bm25 排序）。
- **`messages_fts_trigram`**：FTS5 trigram 分詞，給中文等無空格語言做子字串比對。
- 兩個 FTS 表都用 external content（只存索引、不存原文），壞掉時可從 `messages` 就地 `rebuild`，零資料損失。
- 啟動時會用「一定 rollback 的試寫」探測 FTS 寫入損毀（唯讀檢查抓不到此類），壞了自動重建；trigram tokenizer 不可用的 SQLite build 則降級為 `LIKE` 子字串。

### `/recall` 手動搜尋

`/recall <關鍵字>` 從封存撈出含該關鍵字的歷史對話（最多 8 筆，附時間與 snippet）。中文走精準子字串比對，英文/數字走 FTS5 詞比對、0 筆時降級 `LIKE`。

### 自動召回（auto-recall）

一般對話時，會用當前訊息去封存做模糊比對（中文拆 trigram 用 OR，任一片段命中即召回），把相關舊事注入 prompt，放在「最近對話」之前。

- 受 `RECALL_MIN_QUERY_CHARS`（預設 6）門檻控管，太短或純寒暄不觸發。
- 命中內容會與「最近對話」去重，並套用 `MAX_RECALL_CHARS`（預設 1200）/`RECALL_LIMIT`（預設 4）子預算。
- 注入區塊標示「系統自動撈出，未必精準，僅供參考」，避免模型過度採信。
- 封存查詢與寫入握同一把鎖，故 `_build_prompt()` 與封存皆丟 executor 執行，避免阻塞 Discord heartbeat。
- 封存或召回失敗一律只記 log，絕不影響正常回訊息（熱狀態仍在 JSON）。

> **隱私**：`sessions.db` 含完整對話原文，安全等級等同 `.env`，已列入 `.gitignore`（連同 `-wal`/`-shm`）。

為了讓這套記憶系統在 Discord 環境中順暢運作，需要特別處理非同步執行的問題。

## 非同步架構

[discord.py](https://pypi.org/project/discord.py/) 使用 asyncio 事件循環，若在事件處理器中執行阻塞操作（如 `subprocess.run`），會導致 heartbeat 中斷、bot 離線。

### 設計原則

- **業務邏輯保持同步**：`generate_summary`、`compress_summary`、`maybe_compress_history` 等函數保持同步，邏輯簡單易懂
- **入口點處理非同步**：只在 Discord 事件處理器中使用 `run_in_executor` 包裝阻塞操作

### 執行架構

```
on_message (async, 主執行緒/事件循環)
    │
    └── ask_claude_with_lock (async)
            │
            ├─ STREAM_ENABLED=true（預設）
            │   └── ask_claude_stream (async)
            │           │
            │           ├── asyncio.create_subprocess_exec    ← 原生 async subprocess
            │           │       逐行讀取 NDJSON，每秒編輯 Discord 訊息
            │           │
            │           ├── _save_conversation_turn()         ← 主執行緒（毫秒級）
            │           │
            │           └── run_in_executor(maybe_compress_history) ← 執行緒池
            │
            └─ STREAM_ENABLED=false
                └── ask_claude (async)
                        │
                        ├── run_in_executor(run_claude_sync)  ← 執行緒池
                        │
                        ├── _save_conversation_turn()         ← 主執行緒（毫秒級）
                        │
                        └── run_in_executor(maybe_compress_history) ← 執行緒池

背景壓縮任務（兩種模式共用）:
    maybe_compress_history()
        ├── generate_summary()  ← 同步，subprocess（同時萃取長期記憶條目）
        ├── compress_summary()  ← 同步，subprocess
        ├── merge_memory_facts() + save_memory() ← 同步，檔案 I/O
        └── save_history()      ← 同步，檔案 I/O
```

> **註**：`_save_conversation_turn()` 中的 `save_history()` 在主執行緒執行，但檔案 I/O 通常只需 1-10 毫秒，遠低於 Discord heartbeat 的容忍範圍（數秒），因此不影響 bot 穩定性。

## 串流輸出

預設啟用 token-level streaming，透過 `claude -p` 搭配 `--output-format stream-json --verbose --include-partial-messages` 實現。

### 運作方式

1. 使用者發送訊息後，Bot 立即發送一則 placeholder 訊息
2. Claude CLI 以 NDJSON 格式逐 token 輸出，Bot 每秒編輯一次 Discord 訊息
3. 回應超過 2000 字元時，自動切換到新訊息繼續串流
4. 串流結束後，使用 `chunk_message()` 確保代碼塊格式正確

### 關閉串流

在 `.env` 中設定：

```
STREAM_ENABLED=false
```

關閉後會退回原本的行為：等待 Claude 完整回應後一次送出。

## Claude API 呼叫方式

本專案使用 `claude -p "prompt" --permission-mode bypassPermissions` CLI 呼叫方式，而非直接呼叫 HTTP API。

### 兩種呼叫方式比較

| 方式 | 說明 |
|------|------|
| **`claude -p "prompt" --permission-mode bypassPermissions`** | 透過 Claude Code CLI 呼叫，使用訂閱額度 |
| **HTTP API** | 直接呼叫 `api.anthropic.com`，需要 API Key 付費 |

### 權限模式設定（Skill / MCP）

為了讓 Bot 在觸發 Claude Code skill 或 MCP 時更順利執行，本專案預設使用：

`claude -p "prompt" --permission-mode bypassPermissions`

如果你想自己手動維護權限，請到 `claude_cli.py` 的 `build_claude_command()`，把 `--permission-mode bypassPermissions` 這段程式拿掉即可。

### 禁用互動式工具

`-p` 非互動模式下沒有使用者可以回應 `AskUserQuestion` / `ExitPlanMode` / `EnterPlanMode` 這類互動式工具，模型會誤判為「使用者拒絕回答」而中斷工作，因此透過 `--disallowedTools` 直接移除這些工具。

### 模型設定（分級省成本）

在 `.env` 設定 `CLAUDE_MODEL` 即可為所有 `claude -p` 呼叫指定模型；另外可設定 `CLAUDE_LIGHT_MODEL`，讓簡單任務改走較便宜的模型：

```
CLAUDE_MODEL=claude-opus-4-8
CLAUDE_LIGHT_MODEL=claude-haiku-4-5
```

| 任務 | 使用模型 |
|------|---------|
| 主對話（含串流） | `CLAUDE_MODEL` |
| 摘要生成 + 長期記憶萃取 | `CLAUDE_MODEL` |
| cron 排程任務執行 | `CLAUDE_MODEL` |
| 摘要再壓縮 | `CLAUDE_LIGHT_MODEL` |
| cron 排程標題生成 | `CLAUDE_LIGHT_MODEL` |

退回規則：`CLAUDE_LIGHT_MODEL` 未設定時使用 `CLAUDE_MODEL`；兩者皆未設定時不帶 `--model`，沿用 CLI 預設模型，行為與舊版相同。

摘要生成之所以維持主模型，是因為它同時負責長期記憶萃取，品質直接影響跨對話記憶的準確度；摘要再壓縮與標題生成則是單純的文字濃縮，輕量模型即可勝任。

### 推理強度設定（effort）

`CLAUDE_EFFORT` 控制 `claude -p` 的推理強度（thinking 預算），套用到**所有**呼叫（含串流與 cron）；未設定時預設 `xhigh`：

```
CLAUDE_EFFORT=xhigh
```

| 等級 | 說明 |
|------|------|
| `low` | 最少推理，最快、最省成本 |
| `medium` | 中等推理 |
| `high` | 較深推理 |
| `xhigh` | **預設值**，高推理品質 |
| `max` | 最大推理，最慢、最貴 |

`effort` 越高，模型思考越深，output token 與花費也越高（實測同一道推理題，`max` 的 output token 約為 `low` 的 3 倍、耗時約 2.7 倍）。除 Haiku 外，本專案都會帶 `--effort`（預設 `xhigh`），所以沒設定也有明確值。

> **Haiku 例外**：Haiku 模型不支援 `effort`，帶了 CLI 也只會默默忽略。因此當解析後的模型名稱含 `haiku`（例如 `CLAUDE_LIGHT_MODEL=haiku` 的輕量任務）時，本專案會自動**不帶** `--effort`。

CLI 不會在輸出或 transcript 回報「這次用了哪一級 effort」，若要事後驗證實際效果，可用 `claude -p "..." --effort <級別> --output-format json` 比對回傳的 `output_tokens` / `duration_ms` / `total_cost_usd` 反推。

### 為什麼選擇 `claude -p`

選擇這個方式是因為想**使用 Claude Code 訂閱額度**，而不是購買 API Key。

Anthropic 目前**不允許**直接使用訂閱的 OAuth token 呼叫 HTTP API：

```
"This credential is only authorized for use with Claude Code
and cannot be used for other API requests."
```

因此，透過 CLI 呼叫是使用訂閱額度的**唯一可行方式**。

### `claude -p` 的限制

| 限制 | 說明 |
|------|------|
| **速度較慢** | 每次呼叫都要啟動新的 CLI 進程 |
| **參數控制受限** | 無法精細調整 temperature、top_p 等參數 |
| **併發能力差** | 同時處理多個請求較困難 |
| **錯誤處理困難** | 難以捕捉和處理結構化的 API 錯誤 |
| **非 API Function Calling** | 可用 Claude Code Skill / MCP，但不等同 API function calling |

### HTTP API 的優勢（但需付費）

| 功能 | `claude -p` | HTTP API |
|------|:-----------:|:--------:|
| 即時串流回覆 | ✅（透過 `--include-partial-messages`） | ✅ |
| 多用戶併發 | ⚠️ 受限 | ✅ |
| 結構化回應 | ❌ | ✅ |
| Claude Code Skill / MCP | ✅（搭配 `bypassPermissions`） | ❌ |
| API Function Calling | ❌ | ✅ |
| 精細參數控制 | ❌ | ✅ |
| 使用訂閱額度 | ✅ | ❌ |

### 結論

這是一個**取捨**：為了使用訂閱額度，犧牲了一些功能性。對於個人使用的 Discord Bot，這個取捨是可接受的。透過 `--include-partial-messages` 已實現即時串流回覆，大幅改善使用體驗。如果需要更強大的功能（如 Tool Use），建議購買官方 API Key。

## Claude Code Skills

支援 [Claude Code Skills](https://docs.anthropic.com/en/docs/claude-code/skills)，可在 `.claude/skills/` 目錄下新增自訂 skill 來擴充 Claude 的能力.

## Donation

文章都是我自己研究內化後原創，如果有幫助到您，也想鼓勵我的話，歡迎請我喝一杯咖啡 :laughing:

綠界科技ECPAY ( 不需註冊會員 )

![alt tag](https://payment.ecpay.com.tw/Upload/QRCode/201906/QRCode_672351b8-5ab3-42dd-9c7c-c24c3e6a10a0.png)

[贊助者付款](http://bit.ly/2F7Jrha)

歐付寶 ( 需註冊會員 )

![alt tag](https://i.imgur.com/LRct9xa.png)

[贊助者付款](https://payment.opay.tw/Broadcaster/Donate/9E47FDEF85ABE383A0F5FC6A218606F8)

## 贊助名單

[贊助名單](https://github.com/twtrubiks/Thank-you-for-donate)

## License

MIT license
