"""
工作流程基礎類別 - 定義所有工作流程的共同介面
"""
from abc import ABC, abstractmethod
from typing import Dict, Any, TypedDict
import logging

logger = logging.getLogger(__name__)

class BaseWorkflowState(TypedDict):
    """基礎工作流程狀態"""
    question: str
    user_id: str
    user_account_id: int | None
    has_permission: bool
    final_answer: str
    error_message: str | None

class BaseWorkflow(ABC):
    """工作流程基礎類別"""

    def __init__(self, workflow_name: str):
        self.workflow_name = workflow_name
        self.logger = logging.getLogger(f"{__name__}.{workflow_name}")

    @abstractmethod
    async def execute(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        執行工作流程

        Args:
            state: 工作流程狀態

        Returns:
            執行結果
        """
        pass

    def log_step(self, step_name: str, content: Any):
        """記錄工作流程步驟"""
        self.logger.info(f"[{self.workflow_name}] {step_name}: {content}")

    def create_error_response(self, error_message: str) -> Dict[str, Any]:
        """建立錯誤回應"""
        return {
            "status": "error",
            "data": [],
            "query_used": self.workflow_name,
            "error_detail": error_message
        }

    def create_success_response(self, answer: str, debug_info: Dict[str, Any] = None) -> Dict[str, Any]:
        """建立成功回應"""
        response = {
            "status": "success",
            "data": [{"answer": answer}],
            "query_used": self.workflow_name,
        }
        if debug_info:
            response.update(debug_info)
        return response
