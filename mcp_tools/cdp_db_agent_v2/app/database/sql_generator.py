"""
SQL生成器模組 - 使用LLM生成和執行SQL查詢
"""
import os
import ast
from typing import List, Dict, Any
import logging
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import StrOutputParser
from langchain_community.utilities import SQLDatabase

from .db import get_engine
from .metadata import get_formatted_schema
from ..utils.llm import get_llm_instance
from ..utils.debug import debug_print

logger = logging.getLogger(__name__)

SQL_GENERATION_PROMPT = '''You are a PostgreSQL expert. Given the provided database schema, write a single, precise, and safe SQL query to accomplish the user's task.

**ULTRA-CRITICAL RULES - VIOLATION WILL CAUSE SYSTEM FAILURE:**
1. ONLY use the tables and columns that are EXPLICITLY listed in the schema below
2. NEVER assume any column exists if it's not explicitly shown in the schema
3. NEVER use any tables or columns not listed in the provided schema
4. Each table has different columns - NEVER assume one table has the same columns as another
5. The query MUST be a single SQL statement
6. Do NOT add any explanatory text or markdown formatting (like ```sql). Output ONLY the raw SQL query
7. Pay special attention to which table each column belongs to - do not mix them up
8. If a table only has certain columns listed, it means ONLY those columns exist - there are NO OTHER columns

**SCHEMA VALIDATION CHECKLIST:**
Before writing SQL, verify each column reference:
- ✓ Does this exact column name appear in the schema for this exact table?
- ✓ Am I using the correct table alias for this column?
- ✓ Am I not assuming any columns that aren't explicitly listed?

**IMPORTANT: The schema shows ALL available columns for each table. If a column is not listed, it does NOT exist in that table.**

**Schema:**
{schema}

**Task:**
{task}

**SQL Query (verify each column exists before writing):**
'''

async def generate_and_run_sql(task: str, relevant_tables: List[str], params: Dict[str, Any] = None) -> Any:
    """
    使用LLM生成SQL查詢並執行

    Args:
        task: SQL任務描述
        relevant_tables: 相關資料表清單
        params: 查詢參數

    Returns:
        查詢結果
    """
    debug_print("SQL_GENERATOR", f"Task: {task}")
    logger.info(f"生成SQL任務: {task}")

    try:
        db = SQLDatabase(get_engine())

        # 建立schema字串
        schema_str = ""
        for table in relevant_tables:
            schema_str += get_formatted_schema(table) + "\n\n"
        debug_print("SQL_GENERATOR", f"Schema: {schema_str}")

        # 生成SQL
        prompt = ChatPromptTemplate.from_template(SQL_GENERATION_PROMPT)
        llm = get_llm_instance(temperature=0)
        sql_generation_chain = prompt | llm | StrOutputParser()

        generated_sql = await sql_generation_chain.ainvoke({
            "schema": schema_str,
            "task": task
        })

        # 清理SQL
        generated_sql = generated_sql.strip().replace("```sql", "").replace("```", "").strip()
        debug_print("SQL_GENERATOR", f"Generated SQL: {generated_sql}")
        logger.info(f"生成的SQL: {generated_sql}")

        # 執行SQL
        result_str = db.run(generated_sql, parameters=params)
        debug_print("SQL_GENERATOR", f"Raw Result: {result_str}")

        # 嘗試解析結果
        try:
            return ast.literal_eval(result_str)
        except Exception:
            if not result_str:
                return []
            return result_str

    except Exception as e:
        logger.error(f"SQL生成和執行錯誤: {e}")
        raise Exception(f"SQL生成和執行失敗: {e}")

def validate_sql_safety(sql: str) -> bool:
    """
    驗證SQL查詢的安全性

    Args:
        sql: SQL查詢字串

    Returns:
        是否安全
    """
    # 基本的SQL注入防護
    dangerous_keywords = [
        'DROP', 'DELETE', 'UPDATE', 'INSERT', 'CREATE', 'ALTER',
        'TRUNCATE', 'EXEC', 'EXECUTE', '--', ';'
    ]

    sql_upper = sql.upper()
    for keyword in dangerous_keywords:
        if keyword in sql_upper:
            logger.warning(f"SQL包含危險關鍵字: {keyword}")
            return False

    return True
