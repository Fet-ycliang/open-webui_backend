import os
import json
import re
import time
from openai import OpenAI
from sqlalchemy import text, Engine

from .db import get_engine
from .db_metadata import get_column_metadata, get_all_tables_with_comments

# --- New Prompt for Stage 1: Generating the execution plan ---

PLANNER_PROMPT = '''# Role and Goal
You are a hyper-intelligent and meticulous database query planner. Your sole purpose is to convert a
user\'s question into a precise, multi-step JSON execution plan. You must operate ONLY on the provided 
database schema.

# Critical Rules
1.  **Analyze the Schema First**: You will be provided with a list of tables and their comments under
the "Database Schema" section. Your plan MUST be based *only* on those tables.
2.  **Prioritize Descriptions**: You **MUST** give absolute preference to tables that have descriptive
comments. A table with a relevant comment is always the correct choice over a table with no comment.
**Using a table without a comment is forbidden if a commented table could plausibly contain the
required information.**
3.  **Column Fidelity**: You **MUST ONLY** use the columns listed for each table in the provided
"Database Schema". Do not invent or assume columns exist.
4.  **Semantic Column Matching**: You **MUST** analyze the user's filter value (e.g., a username) and 
the column descriptions to find the most logical column to apply the filter to. For example, if the 
user provides a username and you find a column described as 'Employee Email', you should infer that the
filter should be applied to that email column. If a required mapping (like username to employee_id) is 
not present in any available table, your plan must contain a single step explaining this limitation.
5.  **Example is a Guide**: The example below is for demonstrating the JSON structure and multi-step
logic. The table, column, and value names in the example are generic and **MUST NOT** be copied
directly. You must use the actual table and column names from the "Database Schema" section and the
values from the user's question.
6.  **Multi-Step Planning**: If a question requires information from multiple tables, you MUST create a
multi-step plan with dependencies. Use `{{step_N.results}}` to reference the output of a previous step.
7.  **Output JSON Only**: Your entire response MUST be a single, valid JSON object. Do not include any
text outside of the JSON.
8.  **User Context**: Always filter for the user\'s identity (`:current_user_id`) when the query is 
about a specific user\'s data. Use the table comments to find the correct user identifier column (e.g.,
`empno`, `account_name`).


# Example of a Multi-Step Plan (Demonstrates logic and format ONLY)
User Question: "How many vacation days does user '<a username>' have left?"
Database Schema:
- Table: `leave_balance` (Comment: Employee leave balance)
  Columns:
    - `employee_id` (INTEGER): The employee's unique ID
    - `leave_type` (VARCHAR): The type of leave
    - `remaining_days` (INTEGER): Remaining days for the leave type

- Table: `employees` (Comment: Employee profiles)
  Columns:
    - `employee_id` (INTEGER): The employee's unique ID
    - `username` (VARCHAR): The user's login name

Your Response:
{{
"plan": [
  {{
    "step": 1,
    "description": "Find the employee_id for the given username from the employees table.",
    "query_details": {{
      "table": "employees",
      "columns": ["employee_id"],
      "filters": [
        {{"column": "username", "operator": "=", "value": "<the username from the question>"}}
      ]
    }}
  }},
  {{
    "step": 2,
    "description": "Use the employee_id from step 1 to find the remaining vacation days from the leave_balance
table.",
    "query_details": {{
      "table": "leave_balance",
      "columns": ["remaining_days"],
      "filters": [
        {{"column": "employee_id", "operator": "IN", "value": "{{step_1.results}}"}}
      ]
    }}
  }}
]
}}

# Database Schema:
{schema_context}

# User Question:
{question}
'''

def _create_semantic_map(columns: list[dict]) -> dict[str, str]:
    """Creates a mapping from raw column name to a clean, semantic name derived from the comment."""
    semantic_map = {}
    for col in columns:
        raw_name = col['name']
        comment = col.get('comment', '')
        if comment:
            # Extract the first part of the comment as the semantic name
            semantic_name = comment.split(' ')[0].split('(')[0]
            semantic_map[raw_name] = semantic_name
        else:
            semantic_map[raw_name] = raw_name
    return semantic_map


def _build_sql_from_details(query_details: dict, step_results: dict, user_id: str) -> (str, dict, dict):
    """Constructs a SQL query from a plan step, handling dependencies and parameterization."""
    target_table = query_details.get("table")
    if not target_table:
        raise ValueError("Query details must specify a table.")

    column_metadata = get_column_metadata(target_table)
    if not column_metadata:
        raise ValueError(f"Could not retrieve metadata for table {target_table}")
    semantic_map = _create_semantic_map(column_metadata)
    reverse_semantic_map = {v: k for k, v in semantic_map.items()}

    raw_columns_to_query = [reverse_semantic_map.get(col, col) for col in query_details["columns"]]
    
    quoted_table = f'"{target_table}"'
    quoted_cols = ", ".join([f'"{col}"' for col in raw_columns_to_query])

    where_clauses = []
    params = {}
    param_counter = 0

    for f in query_details.get("filters", []):
        col = f["column"]
        op = f["operator"]
        val = f["value"]
        raw_col_name = reverse_semantic_map.get(col, col)
        quoted_raw_col_name = f'"{raw_col_name}"'

        # --- Dependency Injection ---
        if isinstance(val, str) and "step_" in val and ".results" in val:
            match = re.match(r"{{?step_(\d+)\.results}}?", val)
            if not match:
                raise ValueError(f"Invalid dependency format: {val}")

            dep_step_num = int(match.group(1))
            dep_results = step_results.get(dep_step_num)
            if not dep_results:
                raise ValueError(f"Could not find results for dependency: step {dep_step_num}")
            if not isinstance(dep_results[0], dict):
                raise ValueError(f"Dependency result for step {dep_step_num} is not a list of dicts.")

            first_col_name = list(dep_results[0].keys())[0]
            in_values = [row[first_col_name] for row in dep_results]

            if not in_values:
                where_clauses.append("1=0")
                continue

            in_placeholders = []
            for v in in_values:
                param_name = f"param_{param_counter}"
                params[param_name] = v
                in_placeholders.append(f":{param_name}")
                param_counter += 1
            where_clauses.append(f'{quoted_raw_col_name} IN ({ ", ".join(in_placeholders) })')

        # --- Standard Parameterization ---
        else:
            if op.strip().upper() == "IN" and isinstance(val, list):
                if not val:
                    where_clauses.append("1=0")
                    continue
                in_placeholders = []
                for item in val:
                    param_name = f"param_{param_counter}"
                    params[param_name] = item
                    in_placeholders.append(f":{param_name}")
                    param_counter += 1
                where_clauses.append(f'{quoted_raw_col_name} IN ({ ", ".join(in_placeholders) })')
            else:
                param_name = f"param_{param_counter}"
                param_counter += 1
                if val == ":current_user_id":
                    params[param_name] = user_id
                else:
                    params[param_name] = val
                where_clauses.append(f'{quoted_raw_col_name} {op} :{param_name}')

    sql_query_str = f"SELECT {quoted_cols} FROM {quoted_table}"
    if where_clauses:
        where_sql = " AND ".join(where_clauses)
        sql_query_str += f" WHERE {where_sql}"

    return sql_query_str, params, semantic_map


def get_query_response(question: str, user_id: str) -> dict:
    """Orchestrates the full Text-to-SQL process using a Planner/Executor model."""

    DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai")

    if DEBUG_MODE:
        print(f"--- NEW REQUEST (User: {user_id}) ---")
        print(f"Question: {question}")

    try:
        if LLM_PROVIDER == "ollama":
            client = OpenAI(base_url=os.getenv("OLLAMA_API_BASE_URL"), api_key="ollama")
            model_name = os.getenv("OLLAMA_MODEL", "llama3")
        else:
            client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
            model_name = os.getenv("OPENAI_MODEL", "gpt-4-turbo")
    except Exception as e:
        return {"status": "error", "data": [], "query_used": f"LLM client initialization error: {e}"}

    # STAGE 1: PLANNER - Generate the execution plan
    planning_start_time = time.time()
    try:
        if DEBUG_MODE:
            print("--- PLANNER: START ---")

        tables_with_comments = get_all_tables_with_comments()

        # Filter out tables that do not have a comment.
        filtered_tables = [table for table in tables_with_comments if table.get('comment')]

        print(f" ========> MCP Tool 開發用 --- Text2SQL tables_with_comments : {filtered_tables} <======== ")

        if not filtered_tables:
            return {"status": "error", "data": [], "query_used": "No tables with descriptions found in the database."}

        schema_prompt_part = "Available Tables:\n"
        for table in filtered_tables:
            table_name = table['name']
            table_comment = table['comment']
            
            schema_prompt_part += f"- Table: `{table_name}` (Comment: {table_comment})\n"
            
            columns = get_column_metadata(table_name)
            if columns:
                schema_prompt_part += "  Columns:\n"
                for col in columns:
                    col_name = col['name']
                    col_type = col['type']
                    col_comment = col.get('comment', 'No comment')
                    schema_prompt_part += f"    - `{col_name}` ({col_type}): {col_comment}\n"
            schema_prompt_part += "\n"

        print(f" ========> MCP Tool 開發用 --- Text2SQL schema_prompt_part : {schema_prompt_part} <======== ")
        full_planner_prompt = PLANNER_PROMPT.format(
            schema_context=schema_prompt_part, 
            question=question
        )
        print(f" ========> MCP Tool 開發用 --- Text2SQL full_planner_prompt : {full_planner_prompt} <======== ")

        if DEBUG_MODE:
            print(f"""--- PLANNER: PROMPT SENT TO LLM --- {full_planner_prompt} ------------------------------------""")

        plan_response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": "You are an AI data analyst and query planner."},
                {"role": "user", "content": full_planner_prompt}
            ],
            response_format={"type": "json_object"}
        )

        print(f" ========> MCP Tool 開發用 --- Text2SQL json.loads : in <======== ")
        execution_plan = json.loads(plan_response.choices[0].message.content)

        print(f" ========> MCP Tool 開發用 --- Text2SQL json.loads : out <======== ")
        plan_steps = execution_plan.get("plan", [])

        if DEBUG_MODE:
            print(f"""--- PLANNER: JSON PLAN RECEIVED --- {json.dumps(execution_plan, indent=2, ensure_ascii=False)} ---------------------------------""")

    except Exception as e:
        print(f"LLM plan generation error: {e}")
        return {"status": "error", "data": [], "query_used": f"LLM Error during planning: {e}"}
    finally:
        planning_end_time = time.time()
        if DEBUG_MODE:
            print(f"--- PLANNER: END (Total Time: {planning_end_time - planning_start_time:.2f}s) ---")


    # STAGE 2: EXECUTOR - Execute the plan
    execution_start_time = time.time()
    try:
        if DEBUG_MODE:
            print("--- EXECUTOR: START ---")
        
        # Handle policy questions identified by the planner
        if plan_steps and plan_steps[0].get("query_details", {}).get("table") == "policy_question":
            if DEBUG_MODE:
                print("--- EXECUTOR: Detected policy question. Bypassing execution. ---")
            return {"status": "success", "data": [{"answer": "此問題涉及公司政策，無法直接從資料庫查詢。"}], "query_used": "Policy question"}

        step_results = {}
        final_data = []
        final_semantic_map = {}

        engine = get_engine()
        if not isinstance(engine, Engine):
            return {"status": "error", "data": [], "query_used": "Database engine not initialized."}

        with engine.connect() as connection:
            for step in plan_steps:
                step_num = step["step"]
                query_details = step.get("query_details")

                if not query_details:
                    raise ValueError(f"Step {step_num} is missing 'query_details'.")
                
                if query_details.get("table") == "/* logic */":
                    if step_results:
                        final_data = step_results.get(max(step_results.keys()))
                    continue

                sql_query_str, params, semantic_map = _build_sql_from_details(query_details, step_results, user_id)

                if DEBUG_MODE:
                    debug_sql = sql_query_str
                    for key, value in params.items():
                        if isinstance(value, str):
                            debug_sql = debug_sql.replace(f':{key}', f'"{value}"')
                        else:
                            debug_sql = debug_sql.replace(f':{key}', str(value))
                    print(f"--- EXECUTOR: DEBUG SQL (Step {step_num}) ---")
                    print(debug_sql)
                    print("------------------------------------")

                result = connection.execute(text(sql_query_str), params)
                raw_data = [dict(row) for row in result.mappings()]
                step_results[step_num] = raw_data
                
                if DEBUG_MODE:
                    print(f"""--- EXECUTOR: RAW RESULT (Step {step_num}) --- {raw_data} ----------------------------------""")

                final_data = raw_data
                final_semantic_map = semantic_map

        semantically_mapped_data = []
        for row in final_data:
            new_row = {}
            for raw_key, value in row.items():
                semantic_key = final_semantic_map.get(raw_key, raw_key)
                new_row[semantic_key] = value
            semantically_mapped_data.append(new_row)
        
        if DEBUG_MODE:
            print(f"""--- EXECUTOR: FINAL MAPPED DATA --- {json.dumps(semantically_mapped_data, indent=2, ensure_ascii=False)} -----------------------------------""")

        return {"status": "success", "data": semantically_mapped_data, "query_used": "Multi-step plan executed."}

    except Exception as e:
        print(f"Plan execution error: {e}")
        return {"status": "error", "data": [], "query_used": f"Execution Error: {e}"}
    finally:
        execution_end_time = time.time()
        if DEBUG_MODE:
            print(f"--- EXECUTOR: END (Total Time: {execution_end_time - execution_start_time:.2f}s) ---")