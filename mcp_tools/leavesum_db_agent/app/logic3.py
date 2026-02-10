import os
from dotenv import load_dotenv
from typing import Any, Dict, List

from sqlalchemy import inspect, Engine
from langchain_openai import ChatOpenAI
from langchain_community.agent_toolkits import create_sql_agent
from langchain_community.utilities import SQLDatabase
from langchain.agents.agent_types import AgentType
from langchain.memory import ConversationBufferWindowMemory

from .db import get_engine
from . import db_metadata # 使用您提供的 db_metadata.py

# 載入 .env 檔案
load_dotenv()

# --- Debug Log Helper ---
def _debug_print(title: str, content: Any):
    """根據 DEBUG_MODE 環境變數決定是否打印日誌"""
    if os.getenv("DEBUG_MODE", "false").lower() == "true":
        print(f"\n--- DEBUG: {title} ---\n{content}\n--- END DEBUG ---")

# --- Helper function to filter tables ---
def _get_fully_described_tables(engine: Engine) -> List[str]:
    """獲取資料庫中所有被完整描述的資料表。"""
    _debug_print("Table Filtering", "Starting to filter for fully described tables...")
    inspector = inspect(engine)
    all_tables = inspector.get_table_names()
    fully_described_tables = []
    for table_name in all_tables:
        table_comment = inspector.get_table_comment(table_name)
        if not table_comment:
            _debug_print("Table Filtering", f"- Rejecting table '{table_name}': No table comment.")
            continue
        columns = inspector.get_columns(table_name)
        all_columns_described = True
        for column in columns:
            if not column.get('comment'):
                _debug_print("Table Filtering", f"- Rejecting table '{table_name}': Column '{column['name']}' has no comment.")
                all_columns_described = False
                break
        if all_columns_described:
            _debug_print("Table Filtering", f"+ Accepting table '{table_name}': Fully described.")
            fully_described_tables.append(table_name)
    return fully_described_tables

# --- LLM 配置函數 ---
def get_llm_instance(temperature: float = 0.0) -> ChatOpenAI:
    """根據環境變數配置返回適當的 LLM 實例"""
    llm_provider = os.getenv("LLM_PROVIDER", "openai").lower()
    _debug_print("LLM Configuration", f"Provider: {llm_provider}")
    if llm_provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
        model = os.getenv("OPENAI_MODEL", "gpt-4-turbo")
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required when using OpenAI provider")
        return ChatOpenAI(model=model, temperature=temperature, api_key=api_key)
    elif llm_provider == "ollama":
        base_url = os.getenv("OLLAMA_API_BASE_URL", "http://localhost:11434")
        model = os.getenv("OLLAMA_MODEL", "llama3")
        return ChatOpenAI(model=model, temperature=temperature, base_url=base_url, api_key="ollama")
    else:
        raise ValueError(f"Unsupported LLM provider: {llm_provider}. Supported: 'openai', 'ollama'")

# --- 主要進入點 (最終版) ---
async def get_query_response_langchain_agent(question: str, user_id: str) -> Dict[str, Any]:
    _debug_print("Input", f"Question: {question}\nUser ID: {user_id}")
    try:
        engine = get_engine()
        permissible_tables = _get_fully_described_tables(engine)
        if not permissible_tables:
            return {"status": "error", "data": [], "query_used": "Table filtering failed", "error_detail": "資料庫中沒有任何被完整描述的資料表可供查詢。請洽詢管理員。"}
        _debug_print("Permissible Tables", permissible_tables)

        # 1. 使用 db_metadata.py 手動建立乾淨、清晰的 Schema 文字描述
        schema_descriptions = []
        inspector = inspect(engine)
        for table_name in permissible_tables:
            table_comment = inspector.get_table_comment(table_name).get('text', 'No table comment.')
            schema_descriptions.append(f"Table: {table_name}")
            schema_descriptions.append(f"Comment: {table_comment}")
            columns = db_metadata.get_column_metadata(table_name)
            if columns:
                schema_descriptions.append("Columns:")
                for col in columns:
                    col_name = col['name']
                    col_type = str(col['type'])
                    col_comment = col.get('comment', 'No comment')
                    schema_descriptions.append(f" - {col_name} ({col_type}): {col_comment}")
            schema_descriptions.append("") # Add a blank line for readability
        clean_schema_str = "\n".join(schema_descriptions)
        _debug_print("Clean Schema Generated (Manual)", clean_schema_str)

        # 2. 定義 Agent 的前綴 (包含乾淨的 Schema) 和後綴 (包含我們的規則)
        custom_prompt_prefix = f"""You are an agent designed to interact with a SQL database.
Given an input question, create a syntactically correct query to run, then look at the results and return the answer.
You have access to tools for interacting with the database.
Only use the information returned by the tools to construct your final answer.
You MUST double check your query before executing it. If you get an error while executing a query, rewrite the query and try again.
DO NOT make any DML statements (INSERT, UPDATE, DELETE, DROP etc.) to the database.

Here is the schema of the tables you are allowed to use:
---
{clean_schema_str}
---
"""

        custom_prompt_suffix = """# Instructions
- **Identity Priority (CRITICAL)**: The user account provided in the prefix (e.g., "我的使用者帳號是 'chen'") is the **single source of truth** for user identity. If the text of the question mentions a different name (e.g., "小明", "Jack"), you MUST **completely ignore** that name and perform all queries ONLY for the user account from the prefix. There are no exceptions to this rule.
- **Goal**: Your primary goal is to answer the user's question accurately based on the retrieved database information.
- **Language**: You MUST respond in the same language as the user's question (繁體中文).
- **Confidentiality (VERY STRICT)**: You MUST NOT mention any database table or column names. Furthermore, you MUST NOT mention concepts derived from column names like \"員工編號\" or \"假別代碼\". Frame the answer naturally from the user's perspective. For example, instead of \"員工編號 001 的特休...\", use \"您的特休...\".
- **Code Translation (VERY STRICT)**: If a database column's value is a code (e.g., 'JFET01') and its schema description provides a Chinese mapping, your answer MUST use the Chinese description as a **replacement**. The final answer MUST ONLY contain the Chinese description and MUST NOT contain the original code or any associated English text.
- **User Identification (VERY IMPORTANT)**: The user's question will be prefixed with their user account. You MUST determine which database column (e.g., `email`, `username`) corresponds to this user account by carefully examining the column names and their comments. DO NOT assume the user account maps to `empno` or a generic `id` column if a more descriptive column like `email` exists.

# SQL Generation Rules (CRITICAL)
- When writing a SQL query, you MUST use the actual column names (e.g., `email`, `empno`) in the SQL syntax.
- You should use the column comments/descriptions ONLY to help you decide WHICH column name to use.
- NEVER put a column's description or comment inside the SQL query itself.

# Final Answer Formatting (CRITICAL)
# When you have the final answer, you MUST use the following format:
# Thought: [Your final thought process before answering]
# Final Answer: [The final answer in Traditional Chinese, following all rules]

# --- CORRECT EXAMPLE of Final Answer ---
# Thought: 我已經找到了使用者 'chpechen' 的特休到期日是 2026/10/15。我現在可以回答問題了。
# Final Answer: 您的特休到期日是 2026年10月15日。

# VERY IMPORTANT: TOOL USAGE FORMAT
# When you decide to use a tool, you MUST strictly follow this format on two separate lines:
# 1. Action: [tool_name]
# 2. Action Input: [input_to_tool]

# --- CORRECT EXAMPLE of Tool Usage ---
# Thought: I need to see the available tables to understand the schema.
# Action: sql_db_list_tables
# Action Input: 

Begin!

User's Question: {input}
Thought: {agent_scratchpad}
"""

        # 3. 建立一個窗口大小為 3 的記憶體模組
        memory = ConversationBufferWindowMemory(
            k=3, 
            memory_key="chat_history", 
            input_key="input",
            return_messages=True
        )

        # 4. 建立 Agent，並傳入記憶體
        db = SQLDatabase(engine, include_tables=permissible_tables)
        llm = get_llm_instance(temperature=0)

        agent_executor = create_sql_agent(
            llm=llm,
            db=db,
            agent_type=AgentType.ZERO_SHOT_REACT_DESCRIPTION,
            prefix=custom_prompt_prefix,
            suffix=custom_prompt_suffix,
            memory=memory, # <-- 傳入記憶體
            verbose=os.getenv("DEBUG_MODE", "false").lower() == "true",
            handle_parsing_errors=True,
            agent_executor_kwargs={"handle_parsing_errors": True},
        )

        full_question = f"我的使用者帳號是 '{user_id}'。我的問題是：{question}"
        _debug_print("Full Question to Agent", full_question)

        response = agent_executor.invoke({"input": full_question})
        _debug_print("Agent Raw Response", response)

        final_answer = response.get("output", "無法生成回答。")

        return {
            "status": "success",
            "data": [{"answer": final_answer}],
            "query_used": "LangChain SQL Agent Executed (Custom Schema & Prompt with Memory)",
            "error_detail": None
        }

    except Exception as e:
        _debug_print("Agent Execution Error", f"Type: {type(e).__name__}\nError: {str(e)}")
        return {
            "status": "error",
            "data": [],
            "query_used": "LangChain SQL Agent Failed",
            "error_detail": "在處理您的請求時發生內部錯誤，無法取得結果。"
        }