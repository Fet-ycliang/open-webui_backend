# Azure 部署評估報告 (Azure Hosting Assessment)

針對 **Open WebUI** 類型的 AI/LLM Web 應用程式，以下比較四種 Azure 主要容器部署方案：AKS, Web App for Containers, ACI, Azure Container Apps (ACA)。

## 1. 方案比較表 (Comparison Matrix)

| 特性 (Feature)     | **Azure Kubernetes Service (AKS)**                                        | **Web App for Containers (App Service)**          | **Azure Container Instances (ACI)**  | **Azure Container Apps (ACA)**                                                    |
| :----------------- | :------------------------------------------------------------------------ | :------------------------------------------------ | :----------------------------------- | :-------------------------------------------------------------------------------- |
| **適用場景**       | 大規模微服務、需完整 K8s API 支援、既有 K8s 團隊。                        | 單一 Web 應用、傳統分層架構、不想管理基礎設施。   | 臨時任務、批次作業、簡單測試環境。   | **微服務整合、Serverless 容器、需彈性擴展 (Scale-to-zero)**。                     |
| **架構複雜度**     | **高 (High)**<br>需維護 Cluster, Ingress, Node Pools。                    | **低 (Low)**<br>類似傳統 Hosting，PaaS 體驗。     | **低 (Low)**<br>單純容器執行。       | **中 (Medium)**<br>K8s 封裝，無需管理 Master/Node。                               |
| **維運成本 (Ops)** | **高**<br>需 K8s 專業知識、升級維護。                                     | **低**<br>Azure 全代管。                          | **低**<br>無 Server 管理。           | **中-低**<br>Azure 代管 K8s 底層。                                                |
| **成本 (Costs)**   | **中-高**<br>需支付 VM 費用 + Cluster 管理費 (若選 SLA)。適合持續高負載。 | **中**<br>App Service Plan (固費)。適合穩定流量。 | **中**<br>以秒計費，長期執行不划算。 | **彈性**<br>Consumption Plan (按使用量) 或 Dedicated (固費)。支援 Scale to Zero。 |
| **Autoscale**      | 強大 (HPA, Cluster Autoscale)。                                           | 普通 (依 CPU/Memory/排程)。                       | 無 (或需外掛 Script)。               | **強大 (KEDA)**<br>可依 HTTP 流量、Event 驅動擴展。                               |
| **安全性 & 網路**  | 完整 (VNet, Network Policy, Private Cluster)。                            | 良好 (VNet Integration, Private Endpoint)。       | 普通 (VNet 支援較貴/複雜)。          | **良好** (VNet, Internal Env, Managed Identity)。                                 |

---

## 2. 詳細評估 (Detailed Assessment)

### 2.1 Azure Kubernetes Service (AKS)

- **優點**: 生態系最完整，適合已經有 K8s 維運能力的團隊。若同時有多個 AI Model Serving 容器，AKS 資源調度最優。
- **缺點**: 對於單一 Open WebUI 此類應用來說**殺雞焉用牛刀**。建置與維護成本過高（需透過 Terraform/Bicep 管理大量資源）。
- **成本**: 至少 2-3 個節點 (VM Cost) + Load Balancer。

### 2.2 Azure Web App for Containers

- **優點**: **最簡單的入門選擇**。直接支援 CI/CD (Deployment Center)，內建 Authentication (Easy Auth)，SSL 憑證管理容易。
- **缺點**: Scale out 速度較慢。對於 WebSocket (Chat 應用) 連線數較多時需注意 Plan 限制。無法 Scale to Zero (除此之外無缺點)。
- **成本**: App Service Plan (e.g., P1v3 或 B series)。費用可預測。

### 2.3 Azure Container Instances (ACI)

- **優點**: 啟動極快，單一指令即可跑起來。
- **缺點**: **不建議用於生產環境 Web Server**。缺乏進階負載平衡、藍綠部署、與完整的網路整合功能。主要用於 "Run-once" Jobs。
- **成本**: 長期執行費用通常高於 Web App 或 ACA。

### 2.4 Azure Container Apps (ACA) - 🔥 **建議方案 (Secondary)**

- **優點**: 底層是 K8s 但免管理。支援 **KEDA** (若未來需依照 Queue 數量擴展 Worker 很方便)。支援 Sidecar (Dapr)。Scale to Zero 可省錢。
- **缺點**: 學習曲線比 Web App 稍高 (需了解 Environment, Revision 概念)。
- **成本**: Consumption Plan 有每月免費額度，閒置時不收費 (CPU/Memory)。

---

## 3. 建議方案 (Recommendation)

### 🏆 Primary Recommendation: Azure Web App for Containers

**理由 (Reasoning)**:

1.  **維運最簡**: Open WebUI 是一個單體 (Monolithic-like) 的 Web 服務 (Frontend + Backend in one container)。Web App 模型最貼合，設定最直覺。
2.  **安全性整合**: 內建 VNet Integration 可直接連線 Azure OpenAI / SQL / Storage Private Endpoints。
3.  **WebSocket 支援**: 預設支援，適合 Chat 應用。
4.  **成本可控**: 選用 Linux Plan (e.g., Basic or Premium v3)，費用固定且透明。

### 🥈 Secondary Recommendation: Azure Container Apps (ACA)

**理由 (Reasoning)**:

1.  **擴充彈性**: 若未來打算將 Open WebUI 的 "RAG Pipeline" 或 "Embedding Worker" 拆分成獨立微服務，ACA 是最佳選擇。
2.  **Serverless**: 若流量起伏非常大 (例如只有上班時間有人用)，ACA 的 Scale to Zero 可以節省更多成本。

### 4. 成本估算範例 (Cost Estimation Example)

_假設每月 730 小時，2 vCPU / 8 GB RAM_

1.  **Web App (P1v3)**: 約 **$80 - $100 USD / 月** (包含 SLA, Backup, Slot)。
2.  **ACA (Consumption)**: 視活躍時間而定。若每天只活躍 8 小時，費用可能降至 **$30 - $50 USD**。若全時段活躍，費用可能略高於 Web App。
3.  **AKS**: 最小 Cluster (2 nodes DS2_v2) 約 **$150+ USD / 月** (不含管理費)。
