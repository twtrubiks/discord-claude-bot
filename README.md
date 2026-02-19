# Discord Claude Bot

這是一個 Discord 私人助理機器人，基於 [openclaw](https://github.com/openclaw/openclaw) 概念重新實作精簡版。

專為個人使用設計，具備對話記憶與排程提醒功能，讓 Claude 成為你的專屬 AI 助手。

* [Youtube Tutorial - OpenClaw 啟發 - Discord + Claude AI 整合實戰](https://youtu.be/2UwnqbQsRyY)

## 功能

- 與 Claude AI 對話（使用 Claude Code 訂閱額度，無需另購 API Key）
- 對話歷史管理與自動摘要
- 長期記憶（跨對話記住用戶偏好與重要資訊）
- 排程任務（一次性提醒、定期觸發、每日排程）

## 安裝

```bash
pip install -r requirements.txt
```

## 環境設定

建立 `.env` 檔案：

```
DISCORD_BOT_TOKEN=你的_Discord_Bot_Token
# 選填：用戶白名單，逗號分隔；留空代表不啟用白名單（所有人可用）
ALLOWED_USER_IDS=用戶ID1,用戶ID2
# 選填：達到此訊息數時觸發自動壓縮（預設 16）
MAX_MESSAGES_BEFORE_COMPRESS=16
```

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
        ├─ 空訊息？        → 忽略
        ├─ 特殊指令？      → 對應處理
        │   (/help, /new, /clear, /context, /summarize, /summary,
        │    /memory, /forget, /cron, /remind, /every, /daily...)
        │
        ▼
ask_claude(user_id, message)
        │
        ├─ 組合上下文（長期記憶 + 摘要 + 歷史）
        │
        ▼
subprocess.run(["claude", "-p", prompt, "--permission-mode", "bypassPermissions"])
        │  （含重試機制：最多 3 次）
        ▼
取得回應 + 儲存對話歷史
        │
        ▼
chunk_message() 分塊處理（代碼塊感知）
        │
        ├─ ≤ 2000 字元 → 直接送出
        └─ > 2000 字元 → 分段送出（保持代碼塊完整）
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

### 兩層記憶架構

| 層級 | 用途 | 生命週期 | 儲存位置 |
|------|------|----------|----------|
| **摘要 (summary)** | 單次對話內的上下文壓縮 | `/clear` 或 `/new` 時清除 | `conversation_history.json` |
| **長期記憶 (memory)** | 跨對話的用戶長期資訊 | 永久保留，需 `/forget` 清除 | `memory.json` |

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
    └── ask_claude (async)
            │
            ├── run_in_executor(run_claude_sync)        ← 執行緒池
            │
            ├── save_history()                          ← 主執行緒（毫秒級）
            │
            └── run_in_executor(maybe_compress_history) ← 執行緒池
                    │
                    ├── generate_summary()  ← 同步，subprocess（同時萃取長期記憶條目）
                    ├── compress_summary()  ← 同步，subprocess
                    ├── merge_memory_facts() + save_memory() ← 同步，檔案 I/O
                    └── save_history()      ← 同步，檔案 I/O
```

> **註**：`ask_claude` 中的 `save_history()` 在主執行緒執行，但檔案 I/O 通常只需 1-10 毫秒，遠低於 Discord heartbeat 的容忍範圍（數秒），因此不影響 bot 穩定性。

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
| **無法即時串流** | 難以實現逐字輸出的串流效果 |
| **參數控制受限** | 無法精細調整 temperature、top_p 等參數 |
| **併發能力差** | 同時處理多個請求較困難 |
| **錯誤處理困難** | 難以捕捉和處理結構化的 API 錯誤 |
| **非 API Function Calling** | 可用 Claude Code Skill / MCP，但不等同 API function calling |

### HTTP API 的優勢（但需付費）

| 功能 | `claude -p` | HTTP API |
|------|:-----------:|:--------:|
| 即時串流回覆 | ❌ | ✅ |
| 多用戶併發 | ⚠️ 受限 | ✅ |
| 結構化回應 | ❌ | ✅ |
| Claude Code Skill / MCP | ✅（搭配 `bypassPermissions`） | ❌ |
| API Function Calling | ❌ | ✅ |
| 精細參數控制 | ❌ | ✅ |
| 使用訂閱額度 | ✅ | ❌ |

### 結論

這是一個**取捨**：為了使用訂閱額度，犧牲了一些功能性。對於個人使用的 Discord Bot，這個取捨是可接受的。如果需要更強大的功能（如 Tool Use、串流回覆），建議購買官方 API Key。

## 專案結構

```
bot_discord.py              # 主程式
claude_cli.py               # Claude CLI 指令組裝（含 permission mode）
cron_scheduler.py           # 排程核心
cron_commands.py            # 排程命令處理
conversation_history.json   # 對話歷史（自動產生）
memory.json                 # 長期記憶（自動產生）
cron_jobs.json              # 排程任務（自動產生）
```

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
