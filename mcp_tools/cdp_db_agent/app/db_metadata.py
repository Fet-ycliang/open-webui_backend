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
    Connects to the database, inspects the specified table,
    and returns a formatted string representation of the schema,
    including table and column comments.
    This format is optimized for an LLM to understand.
    """
    engine = get_engine()
    if not isinstance(engine, Engine):
        return f"Could not generate schema for table '{table_name}' because database connection is not configured."

    try:
        inspector = inspect(engine)
        
        if not inspector.has_table(table_name):
            return f"Error: Table '{table_name}' not found in the database."

        table_comment = inspector.get_table_comment(table_name).get('text', 'No table comment.')

        schema_str = f"-- Table: {table_name}\n"
        schema_str += f"-- Description: {table_comment}\n"
        schema_str += f"CREATE TABLE {table_name} (\n"

        columns = [col for col in inspector.get_columns(table_name) if col.get('comment')]

        for i, column in enumerate(columns):
            col_name = column['name']
            col_type = str(column['type'])
            col_comment = column.get('comment', '')
            
            schema_str += f"    {col_name} {col_type}"
            if not column['nullable']:
                schema_str += " NOT NULL"
            
            if i < len(columns) - 1:
                schema_str += ","
            
            if col_comment:
                schema_str += f" -- {col_comment}"
            
            schema_str += "\n"

        schema_str += ");"
        return schema_str

    except Exception as e:
        print(f"An error occurred while inspecting the database: {e}")
        return f"Error: Could not inspect table '{table_name}'. See service logs for details."