"""
資料庫metadata模組
"""
from sqlalchemy import inspect, Engine
from .db import get_engine

def get_all_tables_with_comments() -> list[dict]:
    """Gets a list of all tables and their comments from the database."""
    engine = get_engine()
    if not isinstance(engine, Engine):
        return []

    try:
        inspector = inspect(engine)
        tables = []
        # Get all tables from the default schema (usually 'public')
        for table_name in inspector.get_table_names():
            comment = inspector.get_table_comment(table_name).get('text', '')
            tables.append({"name": table_name, "comment": comment})
        return tables
    except Exception as e:
        print(f"An error occurred while fetching all table names: {e}")
        return []

def get_column_metadata(table_name: str) -> list[dict] | None:
    """Returns a list of dictionaries containing metadata for each column in the specified table."""
    engine = get_engine()
    if not isinstance(engine, Engine):
        return None

    try:
        inspector = inspect(engine)
        if inspector.has_table(table_name):
            return inspector.get_columns(table_name)
        else:
            return None
    except Exception as e:
        print(f"An error occurred while fetching column metadata: {e}")
        return None

def get_formatted_schema(table_name: str) -> str:
    """
    連接資料庫，檢查指定的資料表，
    返回格式化的schema字串表示，包含資料表和欄位註解。
    此格式針對LLM理解進行優化。
    **只包含有描述的欄位**
    """
    engine = get_engine()
    if not isinstance(engine, Engine):
        return f"無法為資料表 '{table_name}' 生成schema，因為資料庫連接未配置。"

    try:
        inspector = inspect(engine)

        if not inspector.has_table(table_name):
            return f"錯誤：在資料庫中找不到資料表 '{table_name}'。"

        table_comment = inspector.get_table_comment(table_name).get('text', '無資料表註解。')

        schema_str = f"**Table: {table_name}**\n"
        schema_str += f"Description: {table_comment}\n\n"
        schema_str += f"Available columns for table '{table_name}' (ONLY these columns exist):\n"

        # **關鍵修復：只包含有描述（comment）的欄位**
        columns = [col for col in inspector.get_columns(table_name) if col.get('comment')]

        if not columns:
            schema_str += f"  ⚠️  No documented columns available for table '{table_name}'\n"
            return schema_str

        for column in columns:
            col_name = column['name']
            col_type = str(column['type'])
            col_comment = column.get('comment', '')
            nullable = "NULL" if column['nullable'] else "NOT NULL"

            schema_str += f"  - {table_name}.{col_name}: {col_type} ({nullable})"

            if col_comment:
                schema_str += f" -- {col_comment}"

            schema_str += "\n"

        # 添加重要警告
        schema_str += f"\n⚠️  IMPORTANT: Table '{table_name}' ONLY has the columns listed above. Do NOT assume it has any other columns!\n"

        return schema_str

    except Exception as e:
        print(f"檢查資料庫時發生錯誤: {e}")
        return f"錯誤：無法檢查資料表 '{table_name}'。詳細信息請查看服務日誌。"
