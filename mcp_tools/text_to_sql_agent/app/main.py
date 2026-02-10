import os
import jwt
import logging
from fastapi import FastAPI, HTTPException, Depends, Header
from pydantic import BaseModel
from typing import Any, Dict, Optional

from . import logic2

log = logging.getLogger(__name__)

# --- Authentication ---

# 1. 如果環境變數中沒有設定 WEBUI_SECRET_KEY，則使用預設值
SECRET_KEY = os.environ.get("WEBUI_SECRET_KEY", "t0p-s3cr3t")

# 2. 定義 JWT 使用的演算法
ALGORITHM = "HS256"

# 3. 定義一個依賴項來從自訂標頭中讀取並驗證 JWT，然後提取使用者名稱
async def get_username_from_custom_header(
    x_user_jwt: Optional[str] = Header(None, alias="X-User-JWT")
) -> str:
    """
    從 'X-User-JWT' 標頭中提取並驗證 JWT，然後返回使用者名稱 (從 email 中剝離域名部分)。
    這個依賴項可以用於保護需要身份驗證的端點。
    """
    if not x_user_jwt:
        raise HTTPException(status_code=401, detail="User JWT token is required in 'X-User-JWT' header")
    
    parts = x_user_jwt.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid 'X-User-JWT' header format. Expected 'Bearer <token>'.")
        
    token = parts[1]

    try:
        # 使用定義的Secret_Key和算法解碼JWT
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: Optional[str] = payload.get("email")
        
        if not email:
            raise HTTPException(status_code=401, detail="Email claim not found in token payload")

    except jwt.PyJWTError as e:
        # 從PYJWT庫中捕獲所有可能的錯誤（例如，已過期，無效簽名）
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

    # 從email中提取使用者名稱（去掉域名部分）
    if "@" in email:
        username = email.split('@')[0]
    else:
        username = email

    log.info(f"User identity '{username}' (from email '{email}') successfully validated.")
    return username


# --- FastAPI App Definition ---
app = FastAPI(
    title="個人假勤查詢代理 (Personnel Leave-Management Agent)",
    description="一個透過查詢資料庫來回答'特定員工'個人化假勤問題的代理程式。**注意：呼叫此工具需要有效的身份驗證，客戶端必須在 HTTP Authorization 標頭中提供一個有效的 JWT Bearer Token。**",
    version="0.5.0",
)

# --- API Models ---
class QueryRequest(BaseModel):
    question: str

class QueryResponse(BaseModel):
    status: str
    data: list[dict]
    query_used: str
    debug_sql: str | None = None
    debug_params: dict | None = None
    error_detail: str | None = None

# --- API Endpoints ---
@app.get("/")
def read_root():
    return {"message": "Personnel Leave-Management Agent is running with custom JWT authentication."}

@app.get("/health")
def health_check():
    return {"status": "ok"}

@app.post("/query", response_model=QueryResponse, openapi_extra={"x-security-policy": "requires_user_jwt"})
async def execute_query(
    request: QueryRequest, 
    current_username: str = Depends(get_username_from_custom_header)
):
    """
    執行自然語言到SQL查詢的轉換和執行。

    該端點需要在'X-User-JWT'頭部中提供有效的用戶JWT令牌。
    函數接收用戶的自然語言查詢， 將其轉換為SQL查詢，執行查詢並返回結果。

    參數:
    - request (QueryRequest): 包含用戶自然語言問題的請求對象
    - current_username (str): 從JWT令牌中提取的當前用戶名，由依賴項自動解析
    返回:
    - QueryResponse: 包含查詢狀態、結果數據、使用的查詢語句和調試SQL的響應對象
    異常:
    - HTTPException: 當發生錯誤時拋出適當的HTTP異常
    """

    try:
        log.info(f"Executing query for verified user: {current_username}")
        
        result = await logic2.get_query_response_langgraph(
            question=request.question, 
            user_id=current_username
        )

        if result.get("status") == "error":
            return QueryResponse(**result)

        return QueryResponse(**result)
    except Exception as e:
        log.error(f"Unexpected error during query execution: {e}")
        raise HTTPException(status_code=500, detail=str(e))