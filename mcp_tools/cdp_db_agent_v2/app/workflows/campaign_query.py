"""
特定活動查詢工作流程 - 使用 LangGraph StateGraph 架構
適配 langchain-community 0.3.18 版本
"""
import json
import re
import logging
from typing import Dict, Any, List, TypedDict
from langgraph.graph import StateGraph, END, START
from langchain_core.prompts import ChatPromptTemplate

from ..core.base_workflow import BaseWorkflow
from ..core.permissions import PermissionManager
from ..database.sql_generator import generate_and_run_sql
from ..utils.llm import get_llm_instance

logger = logging.getLogger(__name__)

class CampaignQueryState(TypedDict):
    """活動查詢狀態 - 與原版本相同的狀態定義"""
    question: str
    user_id: str
    user_account_id: int | None
    user_has_permission: bool
    user_campaigns: List[Dict[str, Any]]
    target_campaign_id: str | None
    target_campaign_exists: bool
    synthesis_context: Dict[str, Any]
    final_answer: str
    error_message: str | None

class CampaignQueryWorkflow(BaseWorkflow):
    """特定活動查詢工作流程 - 完整 StateGraph 實現"""

    def __init__(self):
        super().__init__("Campaign_Specific_Query_v2_StateGraph")
        self.permission_manager = PermissionManager()
        self.synthesis_prompt = ChatPromptTemplate.from_template("""
你是一個有用的AI助手，使用繁體中文回答。根據提供的結構化資料和使用者的原始問題，合成一個最終的、面向使用者的答案。

- 答案應該是完整、自然且有用的句子
- 不要只是重複資料，而是將其框架為問題的答案
- 如果資料顯示失敗或缺少資訊，請提供有用的訊息說明

**使用者的原始問題：**
{question}

**系統的結構化資料：**
```json
{context}
```

**最終回答（繁體中文）：**
        """)

        # 建立 StateGraph 工作流程
        self._build_workflow()

    def _build_workflow(self):
        """建立完整的 StateGraph 工作流程"""
        self.workflow = StateGraph(CampaignQueryState)

        # 添加所有節點（與原版本相同的節點架構）
        self.workflow.add_node("extract_target_campaign_id", self._extract_target_campaign_id)
        self.workflow.add_node("check_permission", self._check_permission)
        self.workflow.add_node("find_user_campaigns", self._find_user_campaigns)
        self.workflow.add_node("check_target_campaign", self._check_target_campaign)
        self.workflow.add_node("get_data_for_synthesis", self._get_data_for_synthesis)
        self.workflow.add_node("master_synthesizer", self._master_synthesizer)
        self.workflow.add_node("synthesize_no_permission", self._synthesize_no_permission)
        self.workflow.add_node("synthesize_no_campaigns", self._synthesize_no_campaigns)
        self.workflow.add_node("synthesize_target_not_found", self._synthesize_target_not_found)
        self.workflow.add_node("synthesize_error", self._synthesize_error)

        # 設定邊（使用新版本的 API）
        self.workflow.add_edge(START, "extract_target_campaign_id")
        self.workflow.add_edge("extract_target_campaign_id", "check_permission")

        # 條件邊：權限檢查後的決策
        self.workflow.add_conditional_edges(
            "check_permission",
            self._decide_after_permission_check,
            {
                "has_permission": "find_user_campaigns",
                "no_permission": "synthesize_no_permission",
                "error": "synthesize_error"
            }
        )

        # 條件邊：查詢活動後的決策
        self.workflow.add_conditional_edges(
            "find_user_campaigns",
            self._decide_after_find_campaigns,
            {
                "has_campaigns": "check_target_campaign",
                "no_campaigns": "synthesize_no_campaigns",
                "error": "synthesize_error"
            }
        )

        # 條件邊：目標活動檢查後的決策
        self.workflow.add_conditional_edges(
            "check_target_campaign",
            self._decide_after_target_check,
            {
                "target_found": "get_data_for_synthesis",
                "target_not_found": "synthesize_target_not_found",
                "error": "synthesize_error"
            }
        )

        # 最終邊
        self.workflow.add_edge("get_data_for_synthesis", "master_synthesizer")
        self.workflow.add_edge("master_synthesizer", END)
        self.workflow.add_edge("synthesize_no_permission", END)
        self.workflow.add_edge("synthesize_no_campaigns", END)
        self.workflow.add_edge("synthesize_target_not_found", END)
        self.workflow.add_edge("synthesize_error", END)

        # 編譯工作流程
        self.app = self.workflow.compile()

    async def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """執行 StateGraph 工作流程"""
        self.log_step("開始執行", f"查詢活動: {state.get('extracted_campaign_id')}")

        initial_state: CampaignQueryState = {
            "question": state["question"],
            "user_id": state["user_id"],
            "target_campaign_id": state.get("extracted_campaign_id"),
            "user_account_id": None,
            "user_has_permission": False,
            "user_campaigns": [],
            "target_campaign_exists": False,
            "synthesis_context": {},
            "final_answer": "",
            "error_message": None
        }

        try:
            final_state = await self.app.ainvoke(initial_state)
            answer = final_state.get("final_answer", "工作流程執行完畢，但沒有產生明確的回應。")
            return self.create_success_response(answer)

        except Exception as e:
            self.log_step("執行錯誤", str(e))
            return self.create_error_response(f"StateGraph 工作流程執行發生錯誤: {e}")

    async def run(self, initial_state: Dict[str, Any]) -> Dict[str, Any]:
        """
        執行特定活動查詢工作流程

        Args:
            initial_state: 包含問題、使用者ID等初始狀態的字典

        Returns:
            符合QueryResponse格式的結果字典
        """
        try:
            logger.info(f"開始執行特定活動查詢工作流程，使用者: {initial_state.get('user_id')}")

            # 準備完整的初始狀態
            state = CampaignQueryState(
                question=initial_state["question"],
                user_id=initial_state["user_id"],
                user_account_id=None,
                user_has_permission=False,
                user_campaigns=[],
                target_campaign_id=initial_state.get("extracted_campaign_id"),
                target_campaign_exists=False,
                synthesis_context={},
                final_answer="",
                error_message=None
            )

            # 編譯並運行工作流程
            compiled_workflow = self.workflow.compile()
            final_state = await compiled_workflow.ainvoke(state)

            # 根據最終狀態構建回應
            if final_state.get("error_message"):
                return {
                    "status": "error",
                    "data": [],
                    "query_used": "活動查詢工作流程",
                    "error_detail": final_state["error_message"]
                }
            else:
                return {
                    "status": "success",
                    "data": [{"answer": final_state["final_answer"]}],
                    "query_used": "活動查詢工作流程",
                    "debug_sql": None,
                    "debug_params": None
                }

        except Exception as e:
            logger.error(f"活動查詢工作流程執行失敗: {e}")
            return {
                "status": "error",
                "data": [],
                "query_used": "活動查詢工作流程",
                "error_detail": f"工作流程執行失敗: {str(e)}"
            }

    async def get_query_response_langgraph(self, question: str, user_id: str) -> dict:
        """
        與原版本完全一致的主要入口點

        Args:
            question: 使用者問題
            user_id: 使用者ID

        Returns:
            與原版本格式完全一致的回應
        """
        initial_state = {
            "question": question,
            "user_id": user_id,
        }

        try:
            # 編譯並執行工作流程（與原版本完全一致）
            compiled_workflow = self.workflow.compile()
            final_state = await compiled_workflow.ainvoke(initial_state)

            # 與原版本完全一致的回應格式
            answer = final_state.get("final_answer", "代理程式執行完畢，但沒有產生明確的回應。")
            return {
                "status": "success",
                "data": [{"answer": answer}],
                "query_used": "Hybrid State Machine Agent v4 (Final)",
            }

        except Exception as e:
            logger.error(f"工作流程執行失敗: {e}")
            # 原版本沒有錯誤處理，但我們加入以確保穩定性
            return {
                "status": "error",
                "data": [],
                "query_used": "Hybrid State Machine Agent v4 (Final)",
                "error_detail": f"工作流程執行失敗: {str(e)}"
            }

    # === StateGraph 節點函數 ===
    async def _extract_target_campaign_id(self, state: CampaignQueryState) -> Dict[str, Any]:
        """提取目標活動ID"""
        # 如果路由器已經提取了活動ID，直接使用
        if state.get("target_campaign_id"):
            self.log_step("活動ID", f"路由器已提取: {state['target_campaign_id']}")
            return {"target_campaign_id": state["target_campaign_id"]}

        # 從問題中提取活動ID
        match = re.search(r'(c_[0-9a-zA-Z_]+)', state['question'])
        if not match:
            return {"error_message": "問題中找不到有效的活動 ID（格式：c_xxx）。"}

        campaign_id = match.group(1)
        self.log_step("活動ID", f"從問題中提取: {campaign_id}")
        return {"target_campaign_id": campaign_id}

    async def _check_permission(self, state: CampaignQueryState) -> Dict[str, Any]:
        """檢查使用者權限"""
        self.log_step("權限檢查", f"檢查使用者 {state['user_id']} 的權限")

        try:
            result = await self.permission_manager.check_cdp_permission(state["user_id"])

            if result["has_permission"]:
                self.log_step("權限檢查", f"使用者有權限，account_id: {result['user_account_id']}")
                return {
                    "user_has_permission": True,
                    "user_account_id": result["user_account_id"]
                }
            else:
                self.log_step("權限檢查", "使用者沒有權限")
                return {"user_has_permission": False}

        except Exception as e:
            logger.error(f"權限檢查錯誤: {e}")
            return {"error_message": f"檢查權限時發生錯誤: {e}"}

    async def _find_user_campaigns(self, state: CampaignQueryState) -> Dict[str, Any]:
        """查詢使用者建立的活動"""
        self.log_step("查詢活動", f"查詢 account_id {state['user_account_id']} 的活動")

        try:
            result = await self.permission_manager.get_user_campaigns(state["user_account_id"])

            if result["success"]:
                campaigns = result["campaigns"]
                self.log_step("查詢活動", f"找到 {len(campaigns)} 個活動")
                return {"user_campaigns": campaigns}
            else:
                return {"error_message": result["error"]}

        except Exception as e:
            logger.error(f"查詢活動錯誤: {e}")
            return {"error_message": f"查詢名下活動時發生資料庫錯誤: {e}"}

    async def _check_target_campaign(self, state: CampaignQueryState) -> Dict[str, Any]:
        """檢查目標活動是否存在於使用者名下"""
        target_id = state['target_campaign_id']
        user_campaigns = state['user_campaigns']

        for campaign in user_campaigns:
            if campaign['id'] == target_id:
                self.log_step("活動驗證", f"找到目標活動 {target_id}")
                return {"target_campaign_exists": True}

        self.log_step("活動驗證", f"目標活動 {target_id} 不在使用者名下")
        return {"target_campaign_exists": False}

    async def _get_data_for_synthesis(self, state: CampaignQueryState) -> Dict[str, Any]:
        """獲取活動詳細資料用於合成答案"""
        target_id = state['target_campaign_id']
        self.log_step("獲取活動詳情", f"活動ID: {target_id}")

        try:
            # 查詢活動通道
            task1 = f"Select id, channel_id from campaign_channel where campaign_id = '{target_id}' and setting_count > 0."
            channels_result = await generate_and_run_sql(task1, relevant_tables=['campaign_channel'])

            if not channels_result:
                return {
                    "synthesis_context": {
                        "status": "failure",
                        "reason": "no_channels_found",
                        "campaign_id": target_id
                    }
                }

            channels = [dict(zip(['id', 'channel_id'], row)) for row in channels_result]
            campaign_channel_ids = tuple([c['id'] for c in channels])

            if not campaign_channel_ids:
                return {
                    "synthesis_context": {
                        "status": "failure",
                        "reason": "no_channel_ids",
                        "campaign_id": target_id
                    }
                }

            # 查詢聯絡天數
            task2 = f"Select over_due_day from ros_tm_content where campaign_channel_id in {campaign_channel_ids}."
            over_due_days_result = await generate_and_run_sql(task2, relevant_tables=['ros_tm_content'])

            if over_due_days_result and over_due_days_result[0]:
                days = over_due_days_result[0][0]
                self.log_step("獲取活動詳情", f"聯絡天數: {days}")
                return {
                    "synthesis_context": {
                        "status": "success",
                        "campaign_id": target_id,
                        "contact_days": days,
                        "channels": channels
                    }
                }
            else:
                return {
                    "synthesis_context": {
                        "status": "failure",
                        "reason": "no_contact_days_found",
                        "campaign_id": target_id
                    }
                }

        except Exception as e:
            logger.error(f"獲取活動詳情錯誤: {e}")
            return {"error_message": f"查詢活動細節時發生資料庫錯誤: {e}"}

    async def _master_synthesizer(self, state: CampaignQueryState) -> Dict[str, Any]:
        """主合成器 - 將結構化資料合成為自然語言回答"""
        context = state.get('synthesis_context', {})
        context_str = json.dumps(context, ensure_ascii=False, indent=2)

        self.log_step("合成答案", f"合成內容: {context}")

        try:
            llm = get_llm_instance(temperature=0.7)
            synthesis_chain = self.synthesis_prompt | llm

            response = await synthesis_chain.ainvoke({
                "question": state["question"],
                "context": context_str
            })

            final_answer = response.content
            self.log_step("合成答案", f"最終答案: {final_answer}")
            return {"final_answer": final_answer}

        except Exception as e:
            logger.error(f"合成答案錯誤: {e}")
            # 提供備用答案
            if context.get("status") == "success":
                campaign_id = context.get("campaign_id", "unknown")
                contact_days = context.get("contact_days", "unknown")
                return {"final_answer": f"根據查詢結果，活動 {campaign_id} 的聯絡天數為 {contact_days} 天。"}
            else:
                return {"final_answer": "無法合成最終答案，但查詢已完成。"}

    # === 錯誤處理節點 ===
    def _synthesize_no_permission(self, state: CampaignQueryState) -> Dict[str, Any]:
        """處理無權限情況"""
        return {"final_answer": "您沒有CDP的使用權限，所以無法查詢資料。"}

    def _synthesize_no_campaigns(self, state: CampaignQueryState) -> Dict[str, Any]:
        """處理無活動情況"""
        return {"final_answer": "您尚未建立任何CDP活動。"}

    def _synthesize_target_not_found(self, state: CampaignQueryState) -> Dict[str, Any]:
        """處理目標活動不存在情況"""
        user_campaigns = state['user_campaigns']
        target_id = state['target_campaign_id']

        if not user_campaigns:
            return {"final_answer": f"您名下沒有 {target_id} 活動，且您沒有其他任何活動。"}

        campaign_list_str = "\n".join([f"- ID: {c['id']}, 名稱: {c['name']}" for c in user_campaigns])
        return {"final_answer": f"您名下沒有 {target_id} 活動。您擁有的活動清單如下：\n{campaign_list_str}"}

    def _synthesize_error(self, state: CampaignQueryState) -> Dict[str, Any]:
        """處理錯誤情況"""
        error_msg = state.get('error_message', '未知錯誤')
        return {"final_answer": f"執行過程中發生錯誤: {error_msg}"}

    # === 條件判斷函數 ===
    def _decide_after_permission_check(self, state: CampaignQueryState) -> str:
        """權限檢查後的路由決策"""
        if state.get("error_message"):
            return "error"
        return "has_permission" if state.get("user_has_permission") else "no_permission"

    def _decide_after_find_campaigns(self, state: CampaignQueryState) -> str:
        """查詢活動後的路由決策"""
        if state.get("error_message"):
            return "error"
        return "has_campaigns" if state.get("user_campaigns") else "no_campaigns"

    def _decide_after_target_check(self, state: CampaignQueryState) -> str:
        """目標活動檢查後的路由決策"""
        if state.get("error_message"):
            return "error"
        return "target_found" if state.get("target_campaign_exists") else "target_not_found"
