"""
SQL安全驗證模組 - 統一的SQL安全檢查功能
"""
import logging
import re
from typing import List

logger = logging.getLogger(__name__)

class SQLSecurityValidator:
    """SQL安全驗證器"""

    def __init__(self):
        # 危險的SQL關鍵字
        self.dangerous_keywords = [
            'DROP', 'DELETE', 'UPDATE', 'INSERT', 'CREATE', 'ALTER',
            'TRUNCATE', 'EXEC', 'EXECUTE', 'GRANT', 'REVOKE', 'MERGE',
            'CALL', 'REPLACE', 'RENAME', 'COPY', 'BACKUP', 'RESTORE',
            'SHUTDOWN', 'KILL', 'USE', 'SHOW', 'DESCRIBE', 'DESC'
        ]

        # 危險符號和注入模式
        self.dangerous_patterns = [
            '--',           # SQL註解
            ';',            # 多重語句分隔符
            '/*',           # 多行註解開始
            '*/',           # 多行註解結束
            'xp_',          # SQL Server擴展程序
            'sp_',          # SQL Server系統預存程序
            'fn_',          # 函數名稱模式
            'UNION',        # 聯集查詢（可能用於注入）
            'LOAD_FILE',    # MySQL讀取檔案
            'INTO OUTFILE', # MySQL寫入檔案
            'INTO DUMPFILE', # MySQL寫入檔案
            'INFORMATION_SCHEMA', # 系統資訊架構
            'pg_',          # PostgreSQL系統函數前綴
            'current_user', # 系統使用者資訊
            'version()',    # 版本資訊
            '@@',          # MySQL系統變數
            'CHAR(',       # 字元編碼注入
            'CONCAT(',     # 字串連接注入
            'SUBSTRING(',  # 子字串注入
            'ASCII(',      # ASCII注入
            'HEX(',        # 十六進位注入
            'UNHEX(',      # 反十六進位注入
            'BENCHMARK(',  # 基準測試注入
            'SLEEP(',      # 延遲注入
            'WAITFOR',     # SQL Server延遲注入
        ]

        # 允許的SQL函數（白名單）
        self.allowed_functions = [
            'COUNT', 'SUM', 'AVG', 'MAX', 'MIN', 'ROUND', 'FLOOR', 'CEIL',
            'UPPER', 'LOWER', 'LENGTH', 'TRIM', 'LTRIM', 'RTRIM',
            'DATE', 'NOW', 'CURRENT_DATE', 'CURRENT_TIME', 'CURRENT_TIMESTAMP',
            'EXTRACT', 'DATE_PART', 'DATE_TRUNC', 'TO_CHAR', 'TO_DATE',
            'COALESCE', 'NULLIF', 'CASE', 'WHEN', 'THEN', 'ELSE', 'END',
            'DISTINCT', 'ORDER', 'GROUP', 'HAVING', 'LIMIT', 'OFFSET'
        ]

    def validate_sql_safety(self, sql: str) -> dict:
        """
        完整的SQL安全驗證

        Args:
            sql: SQL查詢字串

        Returns:
            dict: 包含is_safe (bool) 和 error_message (str) 的結果
        """
        if not sql or not sql.strip():
            logger.warning("空的SQL查詢")
            return {"is_safe": False, "error_message": "SQL查詢為空"}

        sql_upper = sql.upper().strip()

        # 1. 檢查是否只是SELECT或WITH語句
        if not sql_upper.startswith('SELECT') and not sql_upper.startswith('WITH'):
            error_msg = f"只允許SELECT或WITH查詢語句，收到的SQL開頭：{sql_upper[:50]}"
            logger.warning(error_msg)
            return {"is_safe": False, "error_message": error_msg}

        # 2. 檢查危險關鍵字
        for keyword in self.dangerous_keywords:
            if self._contains_dangerous_keyword(sql_upper, keyword):
                error_msg = f"SQL包含危險關鍵字: {keyword}"
                logger.warning(error_msg)
                return {"is_safe": False, "error_message": error_msg}

        # 3. 檢查危險模式
        for pattern in self.dangerous_patterns:
            if pattern.upper() in sql_upper:
                error_msg = f"SQL包含危險模式: {pattern}"
                logger.warning(error_msg)
                return {"is_safe": False, "error_message": error_msg}

        # 4. 檢查多重語句
        sql_statements = sql.strip().split(';')
        non_empty_statements = [stmt.strip() for stmt in sql_statements if stmt.strip()]
        if len(non_empty_statements) > 1:
            error_msg = "不允許多重SQL語句"
            logger.warning(error_msg)
            return {"is_safe": False, "error_message": error_msg}

        # 5. 檢查是否包含過多的嵌套查詢（防止複雜注入）
        if sql_upper.count('SELECT') > 3:
            error_msg = "SQL包含過多嵌套查詢"
            logger.warning(error_msg)
            return {"is_safe": False, "error_message": error_msg}

        # 6. 檢查字串長度（防止超長注入）
        if len(sql) > 5000:
            error_msg = "SQL查詢過長"
            logger.warning(error_msg)
            return {"is_safe": False, "error_message": error_msg}

        # 7. 檢查括號平衡
        if not self._check_parentheses_balance(sql):
            error_msg = "SQL語法錯誤：括號不平衡"
            logger.warning(error_msg)
            return {"is_safe": False, "error_message": error_msg}

        logger.info("SQL安全驗證通過")
        return {"is_safe": True, "error_message": None}

    def _contains_dangerous_keyword(self, sql_upper: str, keyword: str) -> bool:
        """檢查是否包含危險關鍵字（考慮單詞邊界）"""
        # 使用正則表達式確保是完整的單詞，而不是其他單詞的一部分
        pattern = r'\b' + re.escape(keyword) + r'\b'
        return bool(re.search(pattern, sql_upper))

    def _check_parentheses_balance(self, sql: str) -> bool:
        """檢查括號是否平衡"""
        count = 0
        for char in sql:
            if char == '(':
                count += 1
            elif char == ')':
                count -= 1
                if count < 0:
                    return False
        return count == 0

    def get_allowed_tables(self) -> List[str]:
        """獲取允許查詢的資料表清單"""
        # 這裡應該返回系統允許查詢的資料表清單
        # 可以從配置文件或資料庫中動態載入
        return [
            'accounts', 'campaign', 'campaign_channel', 'ros_tm_content',
            # 根據實際需求添加更多表
        ]

    def validate_table_access(self, sql: str, allowed_tables: List[str] = None) -> dict:
        """
        驗證SQL是否只存取允許的資料表

        Args:
            sql: SQL查詢字串
            allowed_tables: 允許的資料表清單

        Returns:
            dict: 包含is_safe (bool) 和 error_message (str) 的結果
        """
        if allowed_tables is None:
            allowed_tables = self.get_allowed_tables()

        # 提取SQL中的資料表名稱（簡單的正則表達式匹配）
        # 這個實現可以根據需要進一步完善
        sql_upper = sql.upper()

        # 查找FROM和JOIN後面的表名
        table_patterns = [
            r'FROM\s+(\w+)',
            r'JOIN\s+(\w+)',
            r'INNER\s+JOIN\s+(\w+)',
            r'LEFT\s+JOIN\s+(\w+)',
            r'RIGHT\s+JOIN\s+(\w+)',
            r'FULL\s+JOIN\s+(\w+)'
        ]

        found_tables = set()
        for pattern in table_patterns:
            matches = re.findall(pattern, sql_upper)
            found_tables.update(matches)

        # 檢查是否所有表都在允許清單中
        allowed_tables_upper = [table.upper() for table in allowed_tables]
        unauthorized_tables = found_tables - set(allowed_tables_upper)

        if unauthorized_tables:
            error_msg = f"SQL存取未授權的資料表: {', '.join(unauthorized_tables)}"
            logger.warning(error_msg)
            return {"is_safe": False, "error_message": error_msg}

        return {"is_safe": True, "error_message": None}

# 全域安全驗證器實例
_security_validator = SQLSecurityValidator()

def validate_sql_safety(sql: str, allowed_tables: List[str] = None) -> bool:
    """
    統一的SQL安全驗證入口函數

    Args:
        sql: SQL查詢字串
        allowed_tables: 允許的資料表清單

    Returns:
        bool: 是否安全
    """
    # 基本安全檢查
    safety_result = _security_validator.validate_sql_safety(sql)
    if not safety_result["is_safe"]:
        logger.error(f"SQL安全檢查失敗: {safety_result['error_message']}")
        return False

    # 資料表存取檢查
    if allowed_tables:
        table_result = _security_validator.validate_table_access(sql, allowed_tables)
        if not table_result["is_safe"]:
            logger.error(f"SQL資料表存取檢查失敗: {table_result['error_message']}")
            return False

    return True

def get_security_validator() -> SQLSecurityValidator:
    """獲取安全驗證器實例"""
    return _security_validator
