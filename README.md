# Discord Claude Bot

這是一個 Discord 私人助理機器人，基於 [openclaw](https://github.com/openclaw/openclaw) 概念重新實作精簡版。

專為個人使用設計，具備對話記憶與排程提醒功能，讓 Claude 成為你的專屬 AI 助手。

* [Youtube Tutorial - OpenClaw 啟發 - Discord + Claude AI 整合實戰](https://youtu.be/2UwnqbQsRyY)

## 功能

- 與 Claude AI 對話
- 對話歷史管理與自動摘要
- 排程任務（一次性提醒、定期訊息、每日排程）

## 安裝

```bash
pip install -r requirements.txt
```

## 環境設定

建立 `.env` 檔案：

```
DISCORD_BOT_TOKEN=你的_Discord_Bot_Token
ALLOWED_USER_IDS=用戶ID1,用戶ID2
```

## 使用方式

啟動機器人：

```bash
python bot_discord.py
```

### 指令

**對話**
- `/help` - 顯示說明
- `/clear` - 清除對話歷史
- `/context` - 查看上下文狀態

**排程**
- `/remind <時間> <訊息>` - 一次性提醒（如 `/remind 30m 開會`）
- `/every <間隔> <訊息>` - 定期訊息（如 `/every 1h 喝水`）
- `/daily <HH:MM> <提示>` - 每日排程
- `/cron list` - 列出所有排程

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
        │   (/help, /clear, /cron, /remind...)
        │
        ▼
ask_claude(user_id, message)
        │
        ├─ 組合上下文（摘要 + 歷史）
        │
        ▼
subprocess.run(["claude", "-p", prompt])
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
[Previous conversation summary]
用戶偏好簡潔回答，之前討論過 Python 專案...

[Recent conversation]
User: 你好
Assistant: 你好！有什麼可以幫你的？
User: 幫我寫一個函數

---

Current message from user:
這個函數要能計算費氏數列
```

#### 多輪對話的已知限制

根據 [LLMs Get Lost In Multi-Turn Conversation (2025)](https://arxiv.org/abs/2505.06120) 研究：

- LLM 在多輪對話中效能平均下降 **39%**
- 頻繁切換主題可能導致**上下文混淆**和**幻覺**
- 一旦走錯方向，LLM 難以自我修正

**建議**：切換不同主題時，使用 `/clear` 清除歷史，避免不相關的上下文干擾。

### 持久化

- `save_history()`：將對話狀態序列化為 JSON，存到 `conversation_history.json`
- `load_history()`：啟動時載入歷史記錄（支援新舊格式相容）

### 限制參數

| 參數 | 值 | 說明 |
|------|-----|------|
| `MAX_CONTEXT_CHARS` | 8000 | 送給 Claude 的上下文最大字元數 |
| `MAX_SUMMARY_CHARS` | 2000 | 摘要最大字元數 |
| 壓縮觸發門檻 | 16 條訊息 | 超過此數量觸發自動壓縮 |

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
    ▼
檢查 len(messages) >= 16？
    │
    ├─ 否 → 繼續
    │
    └─ 是 → 觸發壓縮
              │
              ├─ 取最舊 10 條訊息
              ├─ 呼叫 Claude 生成摘要
              ├─ 合併到現有 summary
              │     │
              │     └─ 超過 2000 字？→ 再壓縮
              │
              └─ 保留最新 6 條訊息
```

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
                    ├── generate_summary()  ← 同步，subprocess
                    ├── compress_summary()  ← 同步，subprocess
                    └── save_history()      ← 同步，檔案 I/O
```

### 執行位置說明

| 層級 | 執行位置 | 是否阻塞事件循環 |
|------|----------|------------------|
| `on_message` | 主執行緒（事件循環） | 否（async） |
| `ask_claude` | 主執行緒（事件循環） | 否（async） |
| `run_claude_sync` | 執行緒池 | 否 |
| `save_history`（ask_claude 內） | 主執行緒 | 是（毫秒級，可忽略） |
| `maybe_compress_history` | 執行緒池 | 否 |
| `generate_summary` | 執行緒池（同一執行緒） | 否 |
| `compress_summary` | 執行緒池（同一執行緒） | 否 |
| `save_history`（壓縮流程內） | 執行緒池 | 否 |

`maybe_compress_history` 整個函數（包含內部的 subprocess 和檔案 I/O）都在執行緒池中執行，不會阻塞 Discord 的事件循環，確保 heartbeat 正常運作。

> **註**：`ask_claude` 中的 `save_history()` 在主執行緒執行，但檔案 I/O 通常只需 1-10 毫秒，遠低於 Discord heartbeat 的容忍範圍（數秒），因此不影響 bot 穩定性。

## Claude API 呼叫方式

本專案使用 `claude -p` CLI 呼叫方式，而非直接呼叫 HTTP API。

### 兩種呼叫方式比較

| 方式 | 說明 |
|------|------|
| **`claude -p "prompt"`** | 透過 Claude Code CLI 呼叫，使用訂閱額度 |
| **HTTP API** | 直接呼叫 `api.anthropic.com`，需要 API Key 付費 |

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
| **無 Tool Use** | 無法使用 function calling 功能 |

### HTTP API 的優勢（但需付費）

| 功能 | `claude -p` | HTTP API |
|------|:-----------:|:--------:|
| 即時串流回覆 | ❌ | ✅ |
| 多用戶併發 | ⚠️ 受限 | ✅ |
| 結構化回應 | ❌ | ✅ |
| Tool Use / Function Calling | ❌ | ✅ |
| 精細參數控制 | ❌ | ✅ |
| 使用訂閱額度 | ✅ | ❌ |

### 結論

這是一個**取捨**：為了使用訂閱額度，犧牲了一些功能性。對於個人使用的 Discord Bot，這個取捨是可接受的。如果需要更強大的功能（如 Tool Use、串流回覆），建議購買官方 API Key。

## 專案結構

```
bot_discord.py      # 主程式
cron_scheduler.py   # 排程核心
cron_commands.py    # 排程命令處理
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
