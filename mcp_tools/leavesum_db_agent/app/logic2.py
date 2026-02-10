import os
import json
import re
import ast
from typing import List, TypedDict, Tuple, Dict, Any
import logging
from dotenv import load_dotenv

from sqlalchemy import inspect, Engine
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser, JsonOutputParser
from langchain_community.utilities import SQLDatabase
from langgraph.graph import StateGraph, END

from .db import get_engine
from . import db_metadata

# Load .env file
load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Helper Functions ---
def _debug_print(title: str, content: Any):
    if os.getenv("DEBUG_MODE", "false").lower() == "true":
        print(f"\n--- DEBUG: {title} ---\n{content}\n--- END DEBUG ---")

def _get_fully_described_tables(engine: Engine) -> List[str]:
    _debug_print("Table Filtering", "Starting to filter for fully described tables...")
    inspector = inspect(engine)
    all_tables = inspector.get_table_names()
    fully_described_tables = []
    for table_name in all_tables:
        table_comment = inspector.get_table_comment(table_name)
        if not table_comment:
            continue
        columns = db_metadata.get_column_metadata(table_name)
        if not columns:
            continue
        all_columns_described = True
        for column in columns:
            if not column.get('comment'):
                all_columns_described = False
                break
        if all_columns_described:
            fully_described_tables.append(table_name)
    return fully_described_tables

# --- LLM Configuration ---
def get_llm_instance(temperature: float = 0.0) -> ChatOpenAI:
    llm_provider = os.getenv("LLM_PROVIDER", "openai").lower()
    if llm_provider == "openai":
        return ChatOpenAI(model=os.getenv("OPENAI_MODEL", "gpt-4-turbo"), temperature=temperature, api_key=os.getenv("OPENAI_API_KEY"))
    elif llm_provider == "ollama":
        return ChatOpenAI(model=os.getenv("OLLAMA_MODEL", "llama3"), temperature=temperature, base_url=os.getenv("OLLAMA_API_BASE_URL", "http://localhost:11434"), api_key="ollama")
    else:
        raise ValueError(f"Unsupported LLM provider: {llm_provider}")

# --- Prompts ---
JSON_EXAMPLE_BLOCK = """# High-Quality Examples (For reasoning pattern and JSON structure reference ONLY)

## Example 1: Two-step query
User Question: \"chpechen的特休什麼時候到期？\"
Your Response:
```json
{
  "plan": [
    {
      "step": 1,
      "description": \"根據使用者問題中的 'chpechen'，從 email 欄位查找對應的員工編號 (empno)。\",
      "query_details": {
        "table": "leavesum",
        "columns": ["empno"],
        "filters": [
          {"column": "email", "operator": "=", "value": "chpechen"}
        ]
      }
    },
    {
      "step": 2,
      "description": \"使用前一步驟取得的員工編號，查詢該員工特休（假別代碼 JFET01）的有效迄日。\",
      "query_details": {
        "table": "leavesum",
        "columns": ["timelimite"],
        "filters": [
          {"column": "empno", "operator": "IN", "value": \"{{step_1.results}}\"},
          {"column": "leavetype", "operator": "=", "value": "JFET01"}
        ]
      }
    }
  ]
}
```
"""

PLANNER_TEMPLATE = '''# Role and Goal
You are a hyper-intelligent and meticulous database query planner. Your sole purpose is to convert a user's question into a precise, multi-step JSON execution plan. You must operate ONLY on the provided database schema.

# Critical Rules
1.  **Identity Priority (CRITICAL)**: The user account provided in the prefix (e.g., "我的使用者帳號是 'chpechen'") is the **single source of truth** for user identity. If the text of the question mentions a different name (e.g., "俊沛", "Jack"), you MUST **completely ignore** that name and perform all queries ONLY for the user account from the prefix. There are no exceptions to this rule.
2.  **Analyze the Schema First**: You will be provided with a list of tables and their comments. Your plan MUST be based *only* on those tables.
3.  **Prioritize Descriptions**: You MUST give absolute preference to tables that have descriptive comments.
4.  **Column Fidelity**: You MUST ONLY use the columns listed for each table.
5.  **Semantic Column Matching**: You MUST analyze the user's filter value (e.g., 'chpechen') and the column descriptions to find the most logical column to apply the filter to.
6.  **Multi-Step Planning**: If a question requires information from multiple tables, you MUST create a multi-step plan with dependencies. Use the special syntax '{{step_N.results}}' to reference the output of a previous step.
7.  **Output JSON Only**: Your entire response MUST be a single, valid JSON object in the format `{{"plan": [{{ "step": 1, ...}}]}}`.

{json_example}

# Database Schema:
{schema_context}

# User Question:
{question}
'''

SYNTHESIZER_PROMPT = '''You are a helpful AI assistant who is an expert in summarizing database results in Traditional Chinese.

# Context
- Original Question: "{question}"
- Authenticated User: "{user_id}"
- Final Execution Result: The final result of the query plan, representing the direct answer to the user's question.

# Rules
1.  **Analyze Identity**: Compare the person mentioned in the "Original Question" to the "Authenticated User".
    - If they are different, you MUST start your response with this exact security notice: "基於安全策略，您無法查詢他人的資料。為您查詢到您自己的帳號 ({user_id}) 的資料如下："
    - If they are the same, proceed directly to step 2.

2.  **Analyze Result**: Look at the "Final Execution Result".
    - If the result is `[]`, `[[]]`, `[()]`, `[(None,)]`, or anything similar (indicating no data), your entire response MUST be only this exact phrase: "根據現有資料，我無法回答這個問題。"
    - If the result contains data (e.g., `[('0',)]`, `[('2024-12-31',)]`), proceed to step 3. Note that `[('0',)]` means the answer is the number 0, not "no data".

3.  **Synthesize Answer**:
    - If you printed the security notice in step 1, add to it.
    - Convert the data from the "Final Execution Result" into a clear, natural, and complete sentence in Traditional Chinese.
    - Example: If the question was "特休剩幾天？" and the result is `[('8',)]`, a good answer is "您的特休還剩下 8 天。"

# Data
- Original Question: "{question}"
- Authenticated User: "{user_id}"
- Final Execution Result:
```json
{results}
```

# Final Answer (Traditional Chinese)
'''

# --- LangGraph State and Nodes ---

class AgentState(TypedDict):
    question: str
    user_id: str
    plan: List[dict]
    step_results: List[dict]
    current_step: int
    final_answer: str

async def planner_node(state: AgentState):
    _debug_print("PLANNER NODE - INPUT", f"Question: {state.get('question', 'N/A')}")
    try:
        engine = get_engine()
        tables = _get_fully_described_tables(engine)
        if not tables:
            raise ValueError("No fully described tables found in the database.")

        schema_descriptions = []
        inspector = inspect(engine)
        for table_name in tables:
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
            schema_descriptions.append("")
        clean_schema_str = "\n".join(schema_descriptions)
        _debug_print("Clean Schema for Planner", clean_schema_str)

        prompt = ChatPromptTemplate.from_template(PLANNER_TEMPLATE)
        llm = get_llm_instance(temperature=0)
        planner_chain = prompt | llm | JsonOutputParser()

        full_question = f"我的使用者帳號是 '{state['user_id']}'. 我的問題是：{state['question']}"
        
        plan = await planner_chain.ainvoke({
            "json_example": JSON_EXAMPLE_BLOCK,
            "schema_context": clean_schema_str, 
            "question": full_question
        })
        
        _debug_print("PLANNER NODE - OUTPUT", f"Generated plan: {plan}")
        return {"plan": plan.get("plan", [])}
    except Exception as e:
        logger.error(f"Planner Node Error: {e}")
        raise

async def executor_node(state: AgentState):
    _debug_print("EXECUTOR NODE - INPUT", f"Executing step {state.get('current_step', 0)}")
    current_step_idx = state.get("current_step", 0)
    plan = state.get("plan", [])
    if current_step_idx >= len(plan):
        return {}

    plan_step = plan[current_step_idx]
    query_details = plan_step.get("query_details")
    if not query_details:
        raise ValueError(f"Step {plan_step.get('step')} is missing 'query_details'.")

    try:
        db = SQLDatabase(get_engine())
        step_results_map = {step["step"]: step["result"] for step in state.get("step_results", [])}
        
        sql_query, params, _ = _build_sql_from_details(query_details, step_results_map, state["user_id"])
        _debug_print(f"EXECUTOR NODE - SQL for Step {plan_step.get('step')}", sql_query)
        _debug_print(f"EXECUTOR NODE - Params for Step {plan_step.get('step')}", params)

        result_data_str = db.run(sql_query, parameters=params)
        _debug_print(f"EXECUTOR NODE - Raw DB Result", result_data_str)

        try:
            parsed_result = ast.literal_eval(result_data_str)
        except (ValueError, SyntaxError):
            parsed_result = [result_data_str]

        new_result = {"step": plan_step["step"], "result": parsed_result}
        return {"step_results": state.get("step_results", []) + [new_result]}

    except Exception as e:
        logger.error(f"Executor Node Error: {e}")
        error_result = {"step": plan_step.get("step"), "result": [], "error": str(e)}
        return {"step_results": state.get("step_results", []) + [error_result]}

async def synthesizer_node(state: AgentState):
    _debug_print("SYNTHESIZER NODE - INPUT", f"Synthesizing answer for question: {state.get('question')}")
    prompt = ChatPromptTemplate.from_template(SYNTHESIZER_PROMPT)
    llm = get_llm_instance(temperature=0.7)
    synthesizer_chain = prompt | llm | StrOutputParser()

    step_results = state.get("step_results", [])
    # Extract the result from the very last step, which holds the final answer.
    last_result = step_results[-1]["result"] if step_results and "result" in step_results[-1] else "[]"
    results_json = json.dumps(last_result, ensure_ascii=False, indent=2)

    final_answer = await synthesizer_chain.ainvoke({
        "question": state["question"],
        "results": results_json,
        "user_id": state["user_id"]
    })

    _debug_print("SYNTHESIZER NODE - OUTPUT", final_answer)
    return {"final_answer": final_answer}


def should_continue(state: AgentState):
    return "continue" if state.get("current_step", 0) < len(state.get("plan", [])) else "end"

async def increment_step(state: AgentState):
    return {"current_step": state.get("current_step", 0) + 1}

def _build_sql_from_details(query_details: dict, step_results: dict, user_id: str) -> Tuple[str, dict, dict]:
    target_table = query_details.get("table")
    if not target_table:
        raise ValueError("Query details must specify a table.")

    raw_columns_to_query = query_details["columns"]
    quoted_table = f'\"{target_table}\"'
    quoted_cols = ", ".join([f'\"{col}\"' for col in raw_columns_to_query])

    where_clauses = []
    params = {}
    param_counter = 0

    for f in query_details.get("filters", []):
        col, op, val = f["column"], f["operator"], f["value"]
        quoted_raw_col_name = f'\"{col}"'

        if isinstance(val, str) and "{{step_" in val and ".results}}" in val:
            match = re.match(r"{{?step_(\d+)\.results}}?", val)
            if not match:
                raise ValueError(f"Invalid dependency format: {val}")

            dep_step_num = int(match.group(1))
            dep_results = step_results.get(dep_step_num)
            if not dep_results:
                raise ValueError(f"Could not find results for dependency: step {dep_step_num}")

            in_values = [row[0] for row in dep_results if isinstance(row, tuple) and len(row) > 0]
            unique_in_values = sorted(list(set(in_values)))
            if not unique_in_values:
                where_clauses.append("1=0")
                continue

            in_placeholders = []
            for v in unique_in_values:
                param_name = f"param_{param_counter}"
                params[param_name] = v
                in_placeholders.append(f":{param_name}")
                param_counter += 1
            where_clauses.append(f'{quoted_raw_col_name} IN ({ ", ".join(in_placeholders) })')
        else:
            param_name = f"param_{param_counter}"
            param_counter += 1
            params[param_name] = user_id if val == ":current_user_id" else val
            where_clauses.append(f'{quoted_raw_col_name} {op} :{param_name}')

    sql_query_str = f"SELECT {quoted_cols} FROM {quoted_table}"
    if where_clauses:
        sql_query_str += f" WHERE {' AND '.join(where_clauses)}"

    return sql_query_str, params, {}

# --- Graph Definition ---
workflow = StateGraph(AgentState)
workflow.add_node("planner", planner_node)
workflow.add_node("executor", executor_node)
workflow.add_node("increment_step", increment_step)
workflow.add_node("synthesizer", synthesizer_node)

workflow.set_entry_point("planner")
workflow.add_edge("planner", "executor")
workflow.add_conditional_edges("executor", should_continue, {"continue": "increment_step", "end": "synthesizer"})
workflow.add_edge("increment_step", "executor")
workflow.add_edge("synthesizer", END)

app = workflow.compile()

# --- Main Entry Point ---
async def get_query_response_langgraph(question: str, user_id: str) -> dict:
    initial_state = {
        "question": question,
        "user_id": user_id,
        "plan": [],
        "step_results": [],
        "current_step": 0,
        "final_answer": "",
    }
    final_state = await app.ainvoke(initial_state)
    return {
        "status": "success",
        "data": [{"answer": final_state.get("final_answer", "Execution finished with no answer.")}],
        "query_used": "Planner-Executor Agent",
    }