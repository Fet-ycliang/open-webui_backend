"""
JWT身份驗證模組 - 與原版本完全一致的實作，包含debug輸出
"""
import os
import jwt
import logging
from fastapi import HTTPException, Header
from typing import Optional

logger = logging.getLogger(__name__)

# 從環境變數取得密鑰，與原版本完全相同
SECRET_KEY = os.environ.get("WEBUI_SECRET_KEY", "t0p-s3cr3t")
ALGORITHM = "HS256"

async def get_username_from_custom_header(
    x_user_jwt: Optional[str] = Header(None, alias="X-User-JWT")
) -> str:
    print(f"===================CDP活動 mcp_tools_v2 X-User-JWT x_user_jwt: {x_user_jwt}")

    """
    從 'X-User-JWT' header 讀取並驗證JWT，然後返回email的使用者名稱部分
    """
    if not x_user_jwt:
        raise HTTPException(status_code=401, detail="User JWT token is required in 'X-User-JWT' header")

    parts = x_user_jwt.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid 'X-User-JWT' header format. Expected 'Bearer <token>'.")

    token = parts[1]

    try:
        # 使用與原版本完全相同的JWT解碼參數
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: Optional[str] = payload.get("email")

        print(f"===================CDP活動 mcp_tools_v2 token payload: {payload}...")

        if not email:
            raise HTTPException(status_code=401, detail="Email claim not found in token payload")

    except jwt.PyJWTError as e:
        # 與原版本相同的錯誤處理
        raise HTTPException(status_code=401, detail=f"Invalid token: {e}")

    # 從email中提取使用者名稱部分（與原版本完全相同）
    if "@" in email:
        username = email.split('@')[0]
    else:
        username = email

    logger.info(f"User identity '{username}' (from email '{email}') successfully validated.")
    return username
