# CDP活動查詢代理 v2

## 重構後的架構特點

### 🎯 主要改進
1. **條件式路由**：自動判斷問題類型，決定使用適當的工作流程
2. **模組化工作流程**：特定活動查詢 vs 通用LLM查詢
3. **統一權限管理**：所有查詢都先檢查CDP權限
4. **易於擴展**：可輕鬆添加新的工作流程

### 📁 目錄結構
```
cdp_db_agent_v2/
├── app/
│   ├── __init__.py
│   ├── main.py                    # FastAPI 主程式
│   ├── auth/                      # 身份驗證模組
│   │   ├── __init__.py
│   │   └── jwt_auth.py            # JWT 認證邏輯
│   ├── core/                      # 核心邏輯
│   │   ├── __init__.py
│   │   ├── router.py              # 條件式路由器
│   │   ├── base_workflow.py       # 工作流程基礎類別
│   │   └── permissions.py         # 權限檢查
│   ├── workflows/                 # 各種工作流程
│   │   ├── __init__.py
│   │   ├── campaign_query.py      # 特定活動查詢流程
│   │   └── general_query.py       # 通用 LLM 查詢流程
│   ├── database/                  # 資料庫相關
│   │   ├── __init__.py
│   │   ├── db.py                  # 資料庫連接
│   │   ├── metadata.py            # Schema 管理
│   │   └── sql_generator.py       # SQL 生成器
│   └── utils/                     # 工具函數
│       ├── __init__.py
│       ├── llm.py                 # LLM 配置
│       └── debug.py               # 除錯工具
├── requirements.txt
├── .env.example
├── Dockerfile
└── docker-compose.yml
```

### 🔄 工作流程說明

#### 1. 條件式路由
- **目的**：分析使用者問題，決定要走哪個工作流程
- **位置**：`app/core/router.py`
- **決策邏輯**：
  - 包含活動ID（c_xxx格式）→ 特定活動查詢
  - 其他問題 → 通用LLM查詢

#### 2. 特定活動查詢工作流程
- **目的**：處理針對特定活動ID的查詢
- **位置**：`app/workflows/campaign_query.py`
- **流程**：權限檢查 → 活動驗證 → 詳細資料查詢 → 回應合成

#### 3. 通用LLM查詢工作流程
- **目的**：讓LLM根據資料庫schema自由查詢
- **位置**：`app/workflows/general_query.py`
- **流程**：權限檢查 → Schema分析 → SQL生成 → 執行查詢 → 結果合成

### 🛠️ 安裝與執行

#### 方法1：直接執行
```bash
# 安裝依賴
pip install -r requirements.txt

# 設定環境變數
cp .env.example .env
# 編輯 .env 檔案，填入實際的配置值

# 執行服務
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

#### 方法2：Docker Compose
```bash
# 設定環境變數
cp .env.example .env
# 編輯 .env 檔案

# 啟動服務
docker-compose up -d
```

### 🔧 環境變數配置

```env
# 資料庫連接
DATABASE_URL=postgresql://username:password@localhost:5432/your_database

# JWT 密鑰
WEBUI_SECRET_KEY=your-secret-key-here

# LLM 設定
LLM_PROVIDER=openai
OPENAI_API_KEY=your-openai-api-key
OPENAI_MODEL=gpt-4-turbo

# 除錯模式
DEBUG_MODE=false
```

### 📊 API 端點

- `GET /` - 服務狀態
- `GET /health` - 健康檢查
- `GET /info` - 系統資訊
- `POST /query` - 主要查詢端點（需要JWT認證）

### 🔐 身份驗證

所有查詢都需要在 `X-User-JWT` header 中提供有效的 JWT Token：

```
X-User-JWT: Bearer <your-jwt-token>
```

### 🎯 回答您的問題

#### 問題1：如何拆分較適當？
✅ **解決方案**：
- **條件式路由**：`core/router.py` - 負責決策邏輯
- **工作流程**：`workflows/` 目錄 - 各種業務邏輯獨立模組
- **核心功能**：`core/` 目錄 - 權限、基礎類別等共用邏輯
- **資料存取**：`database/` 目錄 - 統一的資料庫操作

#### 問題2：條件式路由的期望實現
✅ **實現**：
- 路由器先分析問題類型
- 有特殊邏輯 → 特定工作流程
- 無特殊邏輯 → 通用LLM工作流程

#### 問題3：條件式路由與工作流程的關係
✅ **正確**：條件式路由決定後才進入工作流程

#### 問題4：關於LangFlow
💡 **建議**：目前使用 LangGraph + 策略模式更適合，因為：
- 更好的程式化控制
- 易於版本控制和測試
- 條件邏輯更靈活

#### 問題5：權限檢查
✅ **實現**：所有工作流程都先檢查 `accounts` 表的使用者權限

### 🚀 擴展新工作流程

要添加新的工作流程，只需要：

1. 在 `workflows/` 目錄建立新的工作流程類別
2. 繼承 `BaseWorkflow` 基礎類別
3. 在 `router.py` 中添加新的路由邏輯
4. 在 `main.py` 中註冊新的工作流程

### 🔍 除錯與監控

- 設定 `DEBUG_MODE=true` 可看到詳細的除錯資訊
- 所有工作流程都有完整的日誌記錄
- API回應包含除錯資訊（SQL查詢、路由決策等）
