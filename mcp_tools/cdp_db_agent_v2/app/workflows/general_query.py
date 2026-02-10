"""
通用查詢工作流程 - 讓LLM根據資料庫schema自由查詢
"""
import logging
from typing import Dict, Any
from langchain_core.prompts import ChatPromptTemplate

from ..core.base_workflow import BaseWorkflow
from ..core.permissions import PermissionManager
from ..database.sql_generator import generate_and_run_sql
from ..database.metadata import get_all_tables_with_comments, get_formatted_schema
from ..utils.llm import get_llm_instance

logger = logging.getLogger(__name__)

class GeneralQueryWorkflow(BaseWorkflow):
    """通用查詢工作流程"""

    def __init__(self):
        super().__init__("General_LLM_Query_v2")
        self.permission_manager = PermissionManager()
        self.analysis_prompt = ChatPromptTemplate.from_template("""
你是一個資料庫查詢專家，需要分析使用者問題並生成適當的SQL查詢。

**資料庫Schema：**
{schema}

**使用者問題：**
{question}

**使用者ID：**
{user_id}

**重要規則：**
1. 只使用提供的資料表和欄位
2. 查詢必須是單一的SQL語句
3. 不要添加任何解釋文字或markdown格式
4. 考慮使用者權限，只查詢該使用者有權存取的資料
5. 如果涉及活動查詢，記得檢查 creator_id 是否為該使用者的 account_id

**SQL查詢：**
        """)

        self.synthesis_prompt = ChatPromptTemplate.from_template("""
根據使用者的問題和查詢結果，提供一個清楚、有用的繁體中文回答。

**使用者問題：**
{question}

**查詢結果：**
{results}

**回答要求：**
- 不可以顯示，creator_id、account id、使用者資料欄流水號。
- 不可以顯示，sql script、程式碼、json。
- 可以在你能取得的資訊範圍內提供與上述兩點沒有牴觸的建議，但不可以把上述兩點用來回答。
- 使用繁體中文回答
- 將查詢結果轉換為自然、易懂的語言
- 如果沒有找到資料，請提供有用的說明
- 回答要參考"資料庫Schema"的描述(comments)，不可直接使用欄位名稱翻譯中文後回答
- 保持回答簡潔但完整

**最終回答：**
        """)

    async def run(self, initial_state: Dict[str, Any]) -> Dict[str, Any]:
        """
        執行通用查詢工作流程

        Args:
            initial_state: 包含問題、使用者ID等初始狀態的字典

        Returns:
            符合QueryResponse格式的結果字典
        """
        try:
            logger.info(f"開始執行通用查詢工作流程，使用者: {initial_state.get('user_id')}")

            # 執行工作流程步驟
            result = await self.execute(initial_state)

            return result

        except Exception as e:
            logger.error(f"通用查詢工作流程執行失敗: {e}")
            return {
                "status": "error",
                "data": [],
                "query_used": "通用查詢工作流程",
                "error_detail": f"工作流程執行失敗: {str(e)}"
            }

    async def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """執行通用查詢邏輯"""
        try:
            # 第一步：檢查權限
            permission_result = await self.permission_manager.check_cdp_permission(state["user_id"])

            if not permission_result["has_permission"]:
                # **修復：權限檢查失敗時返回正常答案，而不是錯誤**
                return {
                    "status": "success",
                    "data": [{"answer": "您沒有CDP的使用權限，所以無法查詢資料。"}],
                    "query_used": "權限檢查",
                    "debug_sql": None,
                    "debug_params": None
                }

            user_account_id = permission_result["user_account_id"]
            self.log_step("權限檢查", f"使用者有權限，account_id: {user_account_id}")

            # 第二步：獲取資料庫schema
            all_tables = get_all_tables_with_comments()
            schema_str = ""
            # 修復：從字典中提取table name
            for table_info in all_tables:
                if isinstance(table_info, dict):
                    table_name = table_info['name']
                else:
                    table_name = table_info
                schema_str += get_formatted_schema(table_name) + "\n\n"

            self.log_step("Schema準備", f"已準備 {len(all_tables)} 個資料表的schema")

            # 第三步：生成SQL查詢
            llm = get_llm_instance(temperature=0)
            analysis_chain = self.analysis_prompt | llm

            sql_response = await analysis_chain.ainvoke({
                "schema": schema_str,
                "question": state["question"],
                "user_id": user_account_id  # 使用account_id而非user_id
            })

            generated_sql = sql_response.content.strip().replace("```sql", "").replace("```", "").strip()
            self.log_step("SQL生成", f"生成的SQL: {generated_sql}")

            # 第四步：執行SQL查詢
            # 修復：生成資料表名稱列表用於SQL執行
            table_names = []
            for table_info in all_tables:
                if isinstance(table_info, dict):
                    table_names.append(table_info['name'])
                else:
                    table_names.append(table_info)

            # 修復：移除不存在的custom_sql參數
            query_results = await generate_and_run_sql(
                generated_sql,
                relevant_tables=table_names
            )

            self.log_step("SQL執行", f"查詢結果行數: {len(query_results) if query_results else 0}")

            # 第五步：合成最終答案
            synthesis_chain = self.synthesis_prompt | llm

            final_response = await synthesis_chain.ainvoke({
                "question": state["question"],
                "results": str(query_results) if query_results else "沒有找到相關資料"
            })

            final_answer = final_response.content
            self.log_step("答案合成", f"最終答案: {final_answer}")

            return {
                "status": "success",
                "data": [{"answer": final_answer, "raw_results": query_results}],
                "query_used": generated_sql,
                "debug_sql": generated_sql,
                "debug_params": {"user_account_id": user_account_id}
            }

        except Exception as e:
            self.log_step("執行錯誤", str(e))
            return {
                "status": "error",
                "data": [],
                "query_used": "通用查詢工作流程",
                "error_detail": f"執行過程發生錯誤: {str(e)}"
            }
