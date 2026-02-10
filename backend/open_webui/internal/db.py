import json
import logging
from contextlib import contextmanager
from typing import Any, Optional

from open_webui.env import (
    DATABASE_URL,
    DATABASE_SCHEMA,
    SRC_LOG_LEVELS,
    DATABASE_POOL_MAX_OVERFLOW,
    DATABASE_POOL_RECYCLE,
    DATABASE_POOL_SIZE,
    DATABASE_POOL_TIMEOUT,
)
from sqlalchemy import Dialect, create_engine, MetaData, types, Unicode
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import QueuePool, NullPool
from sqlalchemy.sql.type_api import _T
from typing_extensions import Self

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["DB"])


# 定義統一的 Unicode 文字類型，確保跨資料庫兼容性
class UnicodeText(Unicode):
    """
    統一的 Unicode 文字類型，確保在所有資料庫中正確處理中文字符
    - MS SQL Server: 映射為 NVARCHAR/NTEXT
    - PostgreSQL: 映射為 VARCHAR/TEXT (UTF-8)
    - SQLite: 映射為 TEXT (UTF-8)
    - MySQL: 映射為 VARCHAR/TEXT (utf8mb4)
    """

    def __init__(self, length=None, **kwargs):
        super().__init__(length=length, **kwargs)


class JSONField(types.TypeDecorator):
    impl = UnicodeText
    cache_ok = True

    def process_bind_param(self, value: Optional[_T], dialect: Dialect) -> Any:
        # ensure_ascii=False 確保中文字符以 UTF-8 形式儲存，而不是 Unicode 轉義序列
        # 例如：儲存 "測試" 而不是 "\\u6e2c\\u8a66"
        # 這樣可以：
        # 1. 節省儲存空間（減少約 30-50% 的 JSON 大小）
        # 2. 提高可讀性（資料庫中直接顯示中文）
        # 3. 便於除錯和維護
        return json.dumps(value, ensure_ascii=False)

    def process_result_value(self, value: Optional[_T], dialect: Dialect) -> Any:
        if value is not None:
            return json.loads(value)

    def copy(self, **kw: Any) -> Self:
        return JSONField()

    def db_value(self, value):
        return json.dumps(value, ensure_ascii=False)

    def python_value(self, value):
        if value is not None:
            return json.loads(value)


# =============================================================================
# SQLALCHEMY CONFIGURATION
# =============================================================================
# This configuration provides database connection and ORM capabilities.
# Database tables should be created manually before starting the application.
# =============================================================================

SQLALCHEMY_DATABASE_URL = DATABASE_URL
if "sqlite" in SQLALCHEMY_DATABASE_URL:
    engine = create_engine(
        SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
    )
else:
    # 為不同資料庫類型配置正確的連接參數
    connect_args = {}

    if "mssql" in SQLALCHEMY_DATABASE_URL:
        # MS SQL Server 的正確 Unicode 配置
        connect_args = {
            "use_setinputsizes": False,
            "autocommit": False,
        }
    elif "mysql" in SQLALCHEMY_DATABASE_URL:
        # MySQL 的 Unicode 配置
        connect_args = {
            "charset": "utf8mb4",
            "use_unicode": True,
        }

    if DATABASE_POOL_SIZE > 0:
        engine = create_engine(
            SQLALCHEMY_DATABASE_URL,
            pool_size=DATABASE_POOL_SIZE,
            max_overflow=DATABASE_POOL_MAX_OVERFLOW,
            pool_timeout=DATABASE_POOL_TIMEOUT,
            pool_recycle=DATABASE_POOL_RECYCLE,
            pool_pre_ping=True,
            poolclass=QueuePool,
            connect_args=connect_args,
        )
    else:
        engine = create_engine(
            SQLALCHEMY_DATABASE_URL,
            pool_pre_ping=True,
            poolclass=NullPool,
            connect_args=connect_args,
        )


SessionLocal = sessionmaker(
    autocommit=False, autoflush=False, bind=engine, expire_on_commit=False
)
metadata_obj = MetaData(schema=DATABASE_SCHEMA)
Base = declarative_base(metadata=metadata_obj)
Session = scoped_session(SessionLocal)


def get_session():
    """
    Get a database session for ORM operations.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


get_db = contextmanager(get_session)
