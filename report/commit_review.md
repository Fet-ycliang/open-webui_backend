# 程式碼審查與功能修正報表 (Code Review & Functional Correction Report)

## 1. 變更概覽 (Change Overview)

**Commit**: `220f3ed` (first commit)
**Branch**: `master` (Tracked via `azure-pipelines.yml` logic)

本次審查主要針對目前 Repository 當下的程式碼狀態與 Azure 部署整合的設定進行分析。由於是 Initial Commit 或單一快照，重點在於**系統配置 (Configuration)** 與 **CI/CD 流程 (Pipeline)** 的新增與調整。

### 變更檔案清單摘要

- `azure-pipelines.yml`: **[NEW/MODIFIED]** 新增 Azure DevOps Pipeline 定義，包含 Build、Push Image 至 ACR、Security Scan (Nexus IQ, pip-audit)。
- `backend/requirements.txt`: Python 相依套件清單。
- `backend/open_webui/config.py`: 後端核心設定檔。
- `Dockerfile`: 容器建置腳本 (Root level)。

---

## 2. 功能修正報表 (Functional Correction Report)

| 功能名稱 / 模組        | 原本行為 (Original Behavior)             | 新行為 / 修正內容 (New Behavior / Changes)                                                                                                                                                                    | 影響範圍與風險 (Impact & Risk)                                                                                                            |
| :--------------------- | :--------------------------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | :---------------------------------------------------------------------------------------------------------------------------------------- |
| **CI/CD Pipeline**     | 無 (Open Source 預設僅含 GitHub Actions) | **新增 Azure Pipelines (`azure-pipelines.yml`)**<br>1. 定義 ACR (`fetdadacr.azurecr.io`)<br>2. 設定 Image Name (`fet-genai-backend`)<br>3. 整合資安掃描 (Codescan, Nexus IQ)<br>4. 區分 Staging/Prod 建置流程 | **影響**: 部署流程標準化。<br>**風險**: Hardcoded ACR 名稱與 Image Name 需確認是否符合所有環境需求。若 ACR 認證失效會導致 Pipeline 失敗。 |
| **Security Scan**      | 無強制掃描                               | **新增安全性掃描步驟**<br>1. `pip-audit`: 檢查 Python 相依套件漏洞。<br>2. `Nexus IQ`: 進行元件授權與漏洞掃描。                                                                                               | **影響**: 建置時間增加，但安全性提升。<br>**風險**: 若掃描出 High/Critical 漏洞會阻擋 Build，需預留修補時間。                             |
| **Docker Build**       | 使用官方 Dockerfile                      | **客製化 Build 流程**<br>Pipeline 中指定 Build Context 與 Argument。                                                                                                                                          | **影響**: 確保 Image 包含正確的 Azure 相關設定。<br>**風險**: 需確保 `Dockerfile` 與 `azure-pipelines.yml` 中的路徑一致。                 |
| **Environment Config** | 使用 `.env` 檔案                         | **整合 Pipeline 變數**<br>透過 Pipeline 變數 (如 `AZURE_ACR_USER`) 注入敏感資訊，而非寫死在檔案中。                                                                                                           | **影響**: 提升機密安全性。<br>**風險**: 需在 Azure DevOps Library 正確設定變數，否則 Runtime 會缺損。                                     |

---

## 3. 風險與建議 (Risks & Recommendations)

### 3.1 程式碼/配置風險

1.  **Hardcoded Values**: `azure-pipelines.yml` 中直接寫死 `AZURE_ACR: 'fetdadacr.azurecr.io'`。
    - _建議_: 將 ACR 名稱改為 Pipeline Library Variable Group 變數，增加重用性。
2.  **Missing Frontend Source**: 目前 Repo 結構看似以 Backend 為主 (`backend/` folder domination)。需確認 Frontend 原始碼是否包含在內或採 Pre-built 模式。若為 Pre-built，後續 UI 修改會受限。
    - _建議_: 確認 `Frontend` 原始碼管理策略。
3.  **Requirements Versioning**: `requirements.txt` 中部分套件未鎖定版本或使用範圍 (e.g. `openai`, `anthropic`)。
    - _建議_: 使用 `pip freeze` 產出的完整版本鎖定清單，避免 Build 環境與 Dev 環境不一致。

### 3.2 架構建議

1.  **State Management**: 目前 `open-webui` 預設使用 SQLite。若部署至 Azure Container，**重新啟動後資料會遺失**。
    - _建議_: 務必在 Azure 設定 `DATABASE_URL` 指向 Azure PostgreSQL，並掛載 Azure Files/Blob 作為 `DATA_DIR` 以持久化上傳檔案。
2.  **Vector DB Persistence**: 同理，內建 ChromaDB 需掛載 Persistent Volume。
    - _建議_: 考慮使用外部 Vector DB 服務，或將 ChromaDB 資料路徑掛載至 Azure Storage。
