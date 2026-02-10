# 系統架構與程式設計文件 (System Architecture & Design)

## 1. 系統架構 (System Architecture)

### 1.1 邏輯架構圖 (Logical Architecture)

本系統基於 Open WebUI 架構，採用前後端分離開發、但整合部署（Backend Serving Frontend）的模式。

```mermaid
graph TD
    User([使用者 User]) -->|HTTPS/WebSocket| Ingress[Azure Application Gateway / Load Balancer]
    Ingress -->|Route / & /api| WebContainer[Open WebUI Container]

    subgraph "Open WebUI Container"
        Frontend[Frontend (SvelteKit Static Build)]
        Backend[Backend API (FastAPI)]

        Frontend -->|Fetch/WS| Backend
    end

    subgraph "Data Layer"
        Backend -->|SQL| SQLDB[(Database\nSQLite/PostgreSQL)]
        Backend -->|Vectors| VectorDB[(Vector DB\nChroma/Milvus)]
        Backend -->|Files| Storage[(File Storage\nLocal/Azure Blob)]
    end

    subgraph "External Services"
        Backend -->|LLM API| LLM[LLM Inference Engine\n(Ollama / Azure OpenAI / OpenAI)]
        Backend -->|Search| WebSearch[Web Search API\n(Google/Bing)]
        Backend -->|Auth| IDP[Identity Provider\n(Entra ID / OAuth)]
    end
```

### 1.2 Azure 部署架構圖 (Azure Deployment Architecture)

建議採用 **Azure Container Apps (ACA)** 或 **AKS** 以獲得最佳彈性。以下以 Azure Container Apps 為例：

```mermaid
graph TB
    Internet((Internet)) --> FD[Azure Front Door / App Gateway]

    subgraph "Azure Resource Group"
        subgraph "VNet (Virtual Network)"
            FD -->|Private Link/Endpoint| ACAEnv[Container Apps Environment]

            subgraph "Container App: Open WebUI"
                AppInstance[Replicas: 1..N]
            end

            ACAEnv --> AppInstance
        end

        AppInstance -->|Private Endpoint| PGSQL[Azure Database for PostgreSQL]
        AppInstance -->|Private Endpoint| Blob[Storage Account (Blob)]
        AppInstance -->|Private Endpoint| ACR[Azure Container Registry]

        AppInstance -->|HTTPS| AOAI[Azure OpenAI Service]
    end

    subgraph "Identity & Security"
        KV[Key Vault]
        Entra[Entra ID (Managed Identity)]
    end

    AppInstance -.->|Role Assignment| KV
    AppInstance -.->|OIDC| Entra
```

---

## 2. 程式設計說明 (Programming Design)

### 2.1 主要模組說明 (Key Modules)

| 模組 (Module)       | 路徑 (Path)                                         | 說明 (Description)                                                                                          | 關鍵技術 (Tech Stack)        |
| :------------------ | :-------------------------------------------------- | :---------------------------------------------------------------------------------------------------------- | :--------------------------- |
| **Backend Entry**   | `backend/open_webui/main.py`                        | 應用程式入口，設定 FastAPI app、Middleware (CORS, Auth)、Router 註冊、以及靜態檔案 (Frontend) 的掛載。      | FastAPI, Starlette, Uvicorn  |
| **Configuration**   | `backend/open_webui/config.py`                      | 統一管理所有設定 (Env Vars)。包含 DB 連線、LLM Endpoint、Auth 設定等。支援動態載入與 Redis 快取。           | Pydantic, SQLAlchemy, Redis  |
| **Retrieval (RAG)** | `backend/open_webui/retrieval/`                     | 處理文件上傳、切塊 (Chunking)、Embedding 產生與向量搜尋。整合 LangChain 與各種 Vector DB (Chroma, Milvus)。 | LangChain, LanceDB, ChromaDB |
| **LLM Routers**     | `backend/open_webui/routers/ollama.py`, `openai.py` | 代理與轉發請求至後端 LLM 引擎。處理 Stream response 轉換。                                                  | Server-Sent Events (SSE)     |
| **Authentication**  | `backend/open_webui/utils/auth.py`                  | JWT Token 簽發與驗證、密碼 Hash (Bcrypt)、OAuth 整合邏輯。                                                  | PyJWT, Passlib, OAuth2       |

### 2.2 關鍵資料流 (Key Data Flows)

1.  **使用者登入 (Auth Flow)**:
    - User -> Frontend (Login Page) -> POST `/api/v1/auths/signin` -> Backend.
    - Backend 驗證帳密或 OAuth Code -> 回傳 JWT Token (Cookie/Header).

2.  **聊天對話 (Chat Flow)**:
    - User -> Frontend -> POST `/api/chat/completions` (OpenAI Compatible API) -> Backend.
    - Backend -> Check Auth -> Retrieve Context (RAG, if enabled) -> Call LLM (Ollama/Azure OpenAI) -> Stream Token -> Frontend.

3.  **文件上傳與 RAG (RAG Flow)**:
    - User -> Upload File -> Backend (`/api/v1/files/`) -> Save to Storage.
    - Trigger ETL Task -> Extract Text -> Chunking -> Embedding Model -> Upsert to Vector DB.

### 2.3 關鍵外部依賴 (External Dependencies)

- **Database**: 預設 SQLite (`webui.db`)，生產環境建議改為 PostgreSQL。
- **Vector DB**: 預設使用內嵌 ChromaDB 或 LanceDB，可設定為外部 Cloud Vector DB。
- **LLM Provider**: 必須連接 Ollama (Local) 或 OpenAI/Azure OpenAI (Cloud API)。
- **Redis** (Optional): 用於 Cache 與 Pub/Sub (若開啟多 Replica 部署則為必須)。
