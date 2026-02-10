import os
import re
import ast
import json
from typing import List, TypedDict, Tuple, Dict, Any
import logging
from dotenv import load_dotenv

from sqlalchemy import inspect, Engine
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_community.utilities import SQLDatabase
from langgraph.graph import StateGraph, END, START

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

def get_llm_instance(temperature: float = 0.0) -> ChatOpenAI:
    llm_provider = os.getenv("LLM_PROVIDER", "openai").lower()
    if llm_provider == "openai":
        return ChatOpenAI(model=os.getenv("OPENAI_MODEL", "gpt-4-turbo"), temperature=temperature, api_key=os.getenv("OPENAI_API_KEY"))
    elif llm_provider == "ollama":
        return ChatOpenAI(model=os.getenv("OLLAMA_MODEL", "llama3"), temperature=temperature, base_url=os.getenv("OLLAMA_API_BASE_URL", "http://localhost:11434"), api_key="ollama")
    else:
        raise ValueError(f"Unsupported LLM provider: {llm_provider}")

# --- Prompt Templates ---

SQL_GENERATION_PROMPT = '''You are a PostgreSQL expert. Given the provided database schema, write a single, precise, and safe SQL query to accomplish the user's task.

**Critical Rules:**
1.  Only use the tables and columns provided in the schema.
2.  Do not use any tables or columns not listed.
3.  The query MUST be a single SQL statement.
4.  Do NOT add any explanatory text or markdown formatting (like ```sql). Output ONLY the raw SQL query.

**Schema:**
{schema}

**Task:**
{task}

**SQL Query:**
'''

FINAL_SYNTHESIS_PROMPT = '''You are a helpful AI assistant in Traditional Chinese. Your task is to synthesize a final, user-facing answer based on the structured data provided and the user's original question.

- The answer should be a complete, natural, and helpful sentence.
- Do not just repeat the data, but frame it as an answer to the question.
- If the data indicates a failure or missing information, formulate a helpful message explaining that.

**User's Original Question:**
{question}

**Structured Data from System:**
```json
{context}
```

**Final Answer (in Traditional Chinese):**
'''

# --- SQL Generation Helper ---

async def _generate_and_run_sql(task: str, relevant_tables: List[str], params: Dict[str, Any] = None) -> Any:
    """Generates SQL using an LLM for a specific task and runs it."""
    _debug_print("SQL_GENERATOR", f"Task: {task}")
    db = SQLDatabase(get_engine())
    
    schema_str = ""
    for table in relevant_tables:
        schema_str += db_metadata.get_formatted_schema(table) + "\n\n"
    _debug_print("SQL_GENERATOR", f"Schema: {schema_str}")

    prompt = ChatPromptTemplate.from_template(SQL_GENERATION_PROMPT)
    llm = get_llm_instance(temperature=0)
    sql_generation_chain = prompt | llm | StrOutputParser()
    
    generated_sql = await sql_generation_chain.ainvoke({
        "schema": schema_str,
        "task": task
    })
    generated_sql = generated_sql.strip().replace("```sql", "").replace("```", "").strip()
    _debug_print("SQL_GENERATOR", f"Generated SQL: {generated_sql}")

    result_str = db.run(generated_sql, parameters=params)
    _debug_print("SQL_GENERATOR", f"Raw Result: {result_str}")
    
    try:
        return ast.literal_eval(result_str)
    except Exception:
        if not result_str:
            return []
        return result_str

# --- Agent State Definition ---
class AgentState(TypedDict):
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

# --- Node Functions ---

async def extract_target_campaign_id(state: AgentState) -> Dict[str, Any]:
    _debug_print("NODE: extract_target_campaign_id", f"Question: {state['question']}")
    match = re.search(r'(c_[0-9a-zA-Z_]+)', state['question'])
    if not match:
        return {"error_message": "問題中找不到有效的活動 ID。"}
    return {"target_campaign_id": match.group(1)}

async def check_permission(state: AgentState) -> Dict[str, Any]:
    _debug_print("NODE: check_permission", f"User ID: {state['user_id']}")
    try:
        task = f"Select the id from the accounts table where the account column is exactly equal to '{state['user_id']}'."
        result = await _generate_and_run_sql(task, relevant_tables=['accounts'])
        if result and isinstance(result, list) and len(result) > 0 and result[0]:
            return {"user_has_permission": True, "user_account_id": result[0][0]}
        else:
            return {"user_has_permission": False}
    except Exception as e:
        logger.error(f"Error in check_permission: {e}")
        return {"error_message": f"檢查權限時發生錯誤: {e}"}

async def find_user_campaigns(state: AgentState) -> Dict[str, Any]:
    _debug_print("NODE: find_user_campaigns", f"User Account ID: {state['user_account_id']}")
    try:
        task = f"Select the id and name from the campaign table where the creator_id column is equal to {state['user_account_id']}."
        result = await _generate_and_run_sql(task, relevant_tables=['campaign'])
        campaigns = [dict(zip(['id', 'name'], row)) for row in result]
        return {"user_campaigns": campaigns}
    except Exception as e:
        logger.error(f"Error in find_user_campaigns: {e}")
        return {"error_message": f"查詢名下活動時發生資料庫錯誤: {e}"}

async def check_target_campaign(state: AgentState) -> Dict[str, Any]:
    target_id = state['target_campaign_id']
    user_campaigns = state['user_campaigns']
    for campaign in user_campaigns:
        if campaign['id'] == target_id:
            return {"target_campaign_exists": True}
    return {"target_campaign_exists": False}

async def get_data_for_synthesis(state: AgentState) -> Dict[str, Any]:
    target_id = state['target_campaign_id']
    _debug_print("NODE: get_data_for_synthesis", f"Getting details for campaign: {target_id}")
    try:
        task1 = f"Select id, channel_id from campaign_channel where campaign_id = '{target_id}'."
        channels_result = await _generate_and_run_sql(task1, relevant_tables=['campaign_channel'])
        if not channels_result:
            return {"synthesis_context": {"status": "failure", "reason": "no_channels_found", "campaign_id": target_id}}

        channels = [dict(zip(['id', 'channel_id'], row)) for row in channels_result]
        campaign_channel_ids = tuple([c['id'] for c in channels])
        if not campaign_channel_ids:
            return {"synthesis_context": {"status": "failure", "reason": "no_channel_ids", "campaign_id": target_id}}

        task2 = f"Select over_due_day from ros_tm_content where campaign_channel_id in {campaign_channel_ids}."
        over_due_days_result = await _generate_and_run_sql(task2, relevant_tables=['ros_tm_content'])
        
        if over_due_days_result and over_due_days_result[0]:
            days = over_due_days_result[0][0]
            return {"synthesis_context": {"status": "success", "campaign_id": target_id, "contact_days": days}}
        else:
            return {"synthesis_context": {"status": "failure", "reason": "no_contact_days_found", "campaign_id": target_id}}

    except Exception as e:
        logger.error(f"Error in get_data_for_synthesis: {e}")
        return {"error_message": f"查詢活動細節時發生資料庫錯誤: {e}"}

# --- Responder and Synthesizer Nodes ---
def synthesize_no_permission(state: AgentState) -> Dict[str, Any]:
    return {"final_answer": "您沒有CDP的使用權限，所以無法查詢資料。"}

def synthesize_no_campaigns(state: AgentState) -> Dict[str, Any]:
    return {"final_answer": "您尚未建立任何CDP活動。"}

def synthesize_target_not_found(state: AgentState) -> Dict[str, Any]:
    user_campaigns = state['user_campaigns']
    target_id = state['target_campaign_id']
    if not user_campaigns:
         return {"final_answer": f"您名下沒有 {target_id} 活動，且您沒有其他任何活動。"}
    campaign_list_str = "\n".join([f"- ID: {c['id']}, 名稱: {c['name']}" for c in user_campaigns])
    return {"final_answer": f"您名下沒有 {target_id} 活動。您擁有的活動清單如下：\n{campaign_list_str}"}

async def master_synthesizer(state: AgentState) -> Dict[str, Any]:
    _debug_print("NODE: master_synthesizer", f"Context: {state.get('synthesis_context')}")
    context = state.get('synthesis_context')
    if not context:
        return {}

    context_str = json.dumps(context, ensure_ascii=False, indent=2)
    prompt = ChatPromptTemplate.from_template(FINAL_SYNTHESIS_PROMPT)
    llm = get_llm_instance(temperature=0.7)
    synthesis_chain = prompt | llm | StrOutputParser()

    final_answer = await synthesis_chain.ainvoke({
        "question": state["question"],
        "context": context_str
    })
    return {"final_answer": final_answer}

def synthesize_error(state: AgentState) -> Dict[str, Any]:
    return {"final_answer": f"執行過程中發生錯誤: {state['error_message']}"}

# --- Graph Definition ---

workflow = StateGraph(AgentState)

workflow.add_node("extract_target_campaign_id", extract_target_campaign_id)
workflow.add_node("check_permission", check_permission)
workflow.add_node("find_user_campaigns", find_user_campaigns)
workflow.add_node("check_target_campaign", check_target_campaign)
workflow.add_node("get_data_for_synthesis", get_data_for_synthesis)
workflow.add_node("master_synthesizer", master_synthesizer)
workflow.add_node("synthesize_no_permission", synthesize_no_permission)
workflow.add_node("synthesize_no_campaigns", synthesize_no_campaigns)
workflow.add_node("synthesize_target_not_found", synthesize_target_not_found)
workflow.add_node("synthesize_error", synthesize_error)

# --- Edge Definition ---

workflow.add_edge(START, "extract_target_campaign_id")
workflow.add_edge("extract_target_campaign_id", "check_permission")

def decide_after_permission_check(state: AgentState) -> str:
    if state.get("error_message"): return "error"
    return "has_permission" if state.get("user_has_permission") else "no_permission"

workflow.add_conditional_edges(
    "check_permission",
    decide_after_permission_check,
    {
        "has_permission": "find_user_campaigns",
        "no_permission": "synthesize_no_permission",
        "error": "synthesize_error"
    }
)

def decide_after_find_campaigns(state: AgentState) -> str:
    if state.get("error_message"): return "error"
    return "has_campaigns" if state.get("user_campaigns") else "no_campaigns"

workflow.add_conditional_edges(
    "find_user_campaigns",
    decide_after_find_campaigns,
    {
        "has_campaigns": "check_target_campaign",
        "no_campaigns": "synthesize_no_campaigns",
        "error": "synthesize_error"
    }
)

def decide_after_target_check(state: AgentState) -> str:
    if state.get("error_message"): return "error"
    return "target_found" if state.get("target_campaign_exists") else "target_not_found"

workflow.add_conditional_edges(
    "check_target_campaign",
    decide_after_target_check,
    {
        "target_found": "get_data_for_synthesis",
        "target_not_found": "synthesize_target_not_found",
        "error": "synthesize_error"
    }
)

workflow.add_edge("get_data_for_synthesis", "master_synthesizer")
workflow.add_edge("master_synthesizer", END)
workflow.add_edge("synthesize_no_permission", END)
workflow.add_edge("synthesize_no_campaigns", END)
workflow.add_edge("synthesize_target_not_found", END)
workflow.add_edge("synthesize_error", END)

app = workflow.compile()

# --- Main Entry Point ---
async def get_query_response_langgraph(question: str, user_id: str) -> dict:
    initial_state = {
        "question": question,
        "user_id": user_id,
    }
    final_state = await app.ainvoke(initial_state)
    answer = final_state.get("final_answer", "代理程式執行完畢，但沒有產生明確的回應。")
    return {
        "status": "success",
        "data": [{"answer": answer}],
        "query_used": "Hybrid State Machine Agent v4 (Final)",
    }
