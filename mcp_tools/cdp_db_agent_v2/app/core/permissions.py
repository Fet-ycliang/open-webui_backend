"""
權限檢查模組 - 統一管理所有查詢的權限驗證
"""
import logging
from typing import Dict, Any, Optional
from ..database.sql_generator import generate_and_run_sql

logger = logging.getLogger(__name__)

class PermissionManager:
    """權限管理器，負責檢查使用者權限"""

    async def check_cdp_permission(self, user_id: str) -> Dict[str, Any]:
        """
        檢查使用者是否有CDP權限

        Args:
            user_id: 使用者ID

        Returns:
            權限檢查結果
        """
        logger.info(f"檢查使用者 {user_id} 的CDP權限")

        try:
            task = f"Select the id from the accounts table where the account column is exactly equal to '{user_id}'."
            result = await generate_and_run_sql(task, relevant_tables=['accounts'])

            if result and isinstance(result, list) and len(result) > 0 and result[0]:
                user_account_id = result[0][0]
                logger.info(f"使用者 {user_id} 有CDP權限，account_id: {user_account_id}")
                return {
                    "has_permission": True,
                    "user_account_id": user_account_id,
                    "message": "使用者有CDP權限"
                }
            else:
                logger.warning(f"使用者 {user_id} 沒有CDP權限")
                return {
                    "has_permission": False,
                    "user_account_id": None,
                    "message": "您沒有CDP的使用權限，所以無法查詢資料。"
                }

        except Exception as e:
            logger.error(f"檢查權限時發生錯誤: {e}")
            return {
                "has_permission": False,
                "user_account_id": None,
                "message": f"檢查權限時發生錯誤: {e}"
            }

    async def get_user_campaigns(self, user_account_id: int) -> Dict[str, Any]:
        """
        取得使用者建立的所有活動

        Args:
            user_account_id: 使用者帳戶ID

        Returns:
            使用者活動清單
        """
        logger.info(f"查詢使用者 {user_account_id} 的活動清單")

        try:
            task = f"Select the id and name from the campaign table where the creator_id column is equal to {user_account_id}."
            result = await generate_and_run_sql(task, relevant_tables=['campaign'])

            campaigns = [dict(zip(['id', 'name'], row)) for row in result]

            return {
                "success": True,
                "campaigns": campaigns,
                "count": len(campaigns)
            }

        except Exception as e:
            logger.error(f"查詢使用者活動時發生錯誤: {e}")
            return {
                "success": False,
                "campaigns": [],
                "count": 0,
                "error": f"查詢名下活動時發生資料庫錯誤: {e}"
            }
