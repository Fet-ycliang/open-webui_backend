import os
import jwt
import logging
from fastapi import FastAPI, HTTPException, Depends, Header
from pydantic import BaseModel
from typing import Any, Dict, Optional

from . import logic2

log = logging.getLogger(__name__)

# --- Authentication ---

# 1. Mimic env.py to get the secret key with a fallback
# NOTE: In a production environment, it is strongly recommended to set this via an environment variable.
SECRET_KEY = os.environ.get("WEBUI_SECRET_KEY", "t0p-s3cr3t")

# 2. Mimic auth.py to define the algorithm
ALGORITHM = "HS256"

# 3. Implement the full JWT decoding, validation, and username extraction logic
async def get_username_from_custom_header(
    x_user_jwt: Optional[str] = Header(None, alias="X-User-JWT")
) -> str:
    """
    Reads and validates a JWT from the 'X-User-JWT' header, 
    then returns the username part of the email claim.
    """
    if not x_user_jwt:
        raise HTTPException(status_code=401, detail="User JWT token is required in 'X-User-JWT' header")
    
    parts = x_user_jwt.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid 'X-User-JWT' header format. Expected 'Bearer <token>'.")
        
    token = parts[1]

    try:
        # Use the defined SECRET_KEY and ALGORITHM to decode the JWT
        # Note: You may need to install PyJWT (`pip install PyJWT`)
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: Optional[str] = payload.get("email")
        
        if not email:
            raise HTTPException(status_code=401, detail="Email claim not found in token payload")

    except jwt.PyJWTError as e:
        # Catch all possible errors from the PyJWT library (e.g., expired, invalid signature)
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

    # Strip the domain part from the email to get the username
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
    This endpoint is now protected and requires a valid User JWT in the 'X-User-JWT' header.
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