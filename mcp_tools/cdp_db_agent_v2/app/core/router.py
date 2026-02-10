"""
條件式路由器 - 負責分析使用者問題並決定使用哪個工作流程
"""
import re
import logging
from typing import Dict, Any
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser

from ..utils.llm import get_llm_instance

logger = logging.getLogger(__name__)

class QueryRouter:
    """查詢路由器，負責決定問題應該走哪個工作流程"""

    def __init__(self):
        self.routing_prompt = ChatPromptTemplate.from_template("""
你是一個智慧路由器，需要分析使用者的問題並決定最適合的處理方式。

**可用的工作流程類型：**
1. CAMPAIGN_SPECIFIC - 針對特定活動ID的查詢（問題中包含 c_xxx 格式的活動ID）
2. GENERAL_QUERY - 一般性查詢，讓LLM根據資料庫schema自由查詢

**使用者問題：**
{question}

**判斷規則：**
- 如果問題中包含活動ID（格式：c_開頭後接數字字母下劃線），選擇 CAMPAIGN_SPECIFIC
- 其他所有情況選擇 GENERAL_QUERY

**回應格式（只回傳工作流程類型）：**
CAMPAIGN_SPECIFIC 或 GENERAL_QUERY
        """)

    async def route_query(self, question: str, user_id: str) -> Dict[str, Any]:
        """
        分析問題並決定路由

        Args:
            question: 使用者問題
            user_id: 使用者ID

        Returns:
            包含路由決策的字典
        """
        logger.info(f"Routing query for user {user_id}: {question}")

        # 快速檢查：如果包含活動ID，直接路由到特定活動查詢
        campaign_id_match = re.search(r'(c_[0-9a-zA-Z_]+)', question)
        # 測試不走特殊工作流程LLM的辨識程度
        # if campaign_id_match:
        #     return {
        #         "workflow_type": "CAMPAIGN_SPECIFIC",
        #         "extracted_campaign_id": campaign_id_match.group(1),
        #         "reasoning": "問題中包含特定活動ID"
        #     }

        # 使用LLM進行更複雜的路由決策
        try:
            llm = get_llm_instance(temperature=0)
            routing_chain = self.routing_prompt | llm | StrOutputParser()

            workflow_type = await routing_chain.ainvoke({"question": question})
            workflow_type = workflow_type.strip()

            if workflow_type not in ["CAMPAIGN_SPECIFIC", "GENERAL_QUERY"]:
                workflow_type = "GENERAL_QUERY"  # 預設值

            return {
                "workflow_type": workflow_type,
                "extracted_campaign_id": None,
                "reasoning": "LLM路由決策"
            }

        except Exception as e:
            logger.error(f"路由決策發生錯誤: {e}")
            return {
                "workflow_type": "GENERAL_QUERY",
                "extracted_campaign_id": None,
                "reasoning": f"路由錯誤，使用預設路由: {e}"
            }

