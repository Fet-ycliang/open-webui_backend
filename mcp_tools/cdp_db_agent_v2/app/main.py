"""
CDP活動查詢代理 v2 主程式
"""
import logging
from fastapi import FastAPI, HTTPException, Depends
from pydantic import BaseModel

from .auth.jwt_auth import get_username_from_custom_header
from .core.router import QueryRouter
from .workflows.campaign_query import CampaignQueryWorkflow
from .workflows.general_query import GeneralQueryWorkflow

# 設定日誌
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# FastAPI應用程式定義
app = FastAPI(
    title="CDP活動查詢工具 - Campaign Activity Query Tool",
    description="""專門用於查詢用戶的活動、Campaign、CDP活動狀態、渠道內容、發送結果等數據的查詢工具。
    
    主要功能：
    - 查詢用戶的所有活動列表
    - 查詢特定活動的詳細信息  
    - 查詢活動的發送狀態和結果
    - 查詢渠道配置和內容
    
    **注意：此工具需要用戶身份驗證，需在 X-User-JWT 標頭中提供有效的 JWT Token。**
    """,
    version="2.0.0",
)

# API模型定義
class QueryRequest(BaseModel):
    question: str

class QueryResponse(BaseModel):
    status: str
    data: list[dict]
    query_used: str
    debug_sql: str | None = None
    debug_params: dict | None = None
    error_detail: str | None = None

# 初始化核心組件
query_router = QueryRouter()
campaign_workflow = CampaignQueryWorkflow()
general_workflow = GeneralQueryWorkflow()

# API端點定義
@app.get("/")
def read_root():
    return {
        "message": "CDP活動查詢工具已啟動 - Campaign Activity Query Tool is running",
        "version": "2.0.0",
        "capabilities": [
            "查詢用戶活動列表",
            "查詢活動詳細信息",
            "查詢發送結果",
            "查詢渠道配置"
        ]
    }

@app.get("/health")
def health_check():
    return {"status": "ok", "version": "2.0.0", "service": "CDP Campaign Query Tool"}

@app.post("/query", response_model=QueryResponse, openapi_extra={"x-security-policy": "requires_user_jwt"})
async def query_campaign_activities(
    request: QueryRequest,
    current_username: str = Depends(get_username_from_custom_header)
):
    """
    查詢用戶的活動和 Campaign 數據

    此端點專門用於查詢用戶的：
    - 活動列表（"我有哪些活動？"）
    - 活動狀態（"活動狀態如何？"）
    - Campaign 詳情（"Campaign 的詳細信息"）
    - 發送結果（"發送結果如何？"）
    - 渠道信息（"渠道配置"）

    需要在 'X-User-JWT' header 中提供有效的用戶JWT。
    """
    print(f"===================CDP活動查詢工具被調用，用戶: {current_username}, 查詢: {request.question}")
    try:
        logger.info(f"收到使用者 {current_username} 的查詢: {request.question}")

        # 第一步：條件式路由決策
        routing_result = await query_router.route_query(request.question, current_username)
        logger.info(f"路由決策: {routing_result}")

        # 第二步：根據路由結果執行對應的工作流程
        workflow_type = routing_result["workflow_type"]

        if workflow_type == "CAMPAIGN_SPECIFIC":
            # 使用特定活動查詢工作流程
            workflow_state = {
                "question": request.question,
                "user_id": current_username,
                "routing_info": routing_result
            }

            # 如果有提取到活動ID，加入狀態中
            if routing_result.get("extracted_campaign_id"):
                workflow_state["extracted_campaign_id"] = routing_result["extracted_campaign_id"]

            result = await campaign_workflow.run(workflow_state)

        else:  # GENERAL_QUERY
            # 使用通用查詢工作流程
            workflow_state = {
                "question": request.question,
                "user_id": current_username,
                "routing_info": routing_result
            }

            result = await general_workflow.run(workflow_state)

        logger.info(f"工作流程執行完成，結果狀態: {result.get('status', 'unknown')}")

        # 確保返回的格式符合QueryResponse模型
        if result.get("status") == "error":
            return QueryResponse(**result)

        return QueryResponse(**result)

    except Exception as e:
        logger.error(f"查詢執行時發生未預期錯誤: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

# 健康檢查和資訊端點
@app.get("/info")
def get_info():
    """取得系統資訊"""
    return {
        "service": "提供登入的使用者查詢個人的 Campaign、CDP活動狀態、渠道、渠道詳細內容、發送內容、發送結果的代理程式。",
        "version": "2.0.0",
        "architecture": {
            "routing": "條件式路由器",
            "workflows": ["特定活動查詢", "通用LLM查詢"],
            "features": ["權限管理", "模組化設計", "向後相容性", "可擴展性"]
        },
        "available_workflows": {
            "CAMPAIGN_SPECIFIC": "針對特定活動ID（c_xxx格式）的查詢",
            "GENERAL_QUERY": "讓LLM根據資料庫schema自由查詢"
        },
        "compatibility": {
            "legacy_endpoint": "/legacy_query",
            "original_functionality": "完全保持"
        }
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
