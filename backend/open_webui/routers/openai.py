import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Literal, Optional, overload

import aiohttp
from aiocache import cached
import requests
import os


from fastapi import Depends, FastAPI, HTTPException, Request, APIRouter
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from open_webui.models.models import Models
from open_webui.config import (
    CACHE_DIR,
)
from open_webui.env import (
    AIOHTTP_CLIENT_TIMEOUT,
    AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST,
    ENABLE_FORWARD_USER_INFO_HEADERS,
    BYPASS_MODEL_ACCESS_CONTROL,
)
from open_webui.models.users import UserModel

from open_webui.constants import ERROR_MESSAGES
from open_webui.env import ENV, SRC_LOG_LEVELS


from open_webui.utils.payload import (
    apply_model_params_to_body_openai,
    apply_model_system_prompt_to_body,
)
from open_webui.utils.misc import (
    convert_logit_bias_input_to_json,
)

from open_webui.utils.auth import get_admin_user, get_verified_user
from open_webui.utils.access_control import has_access


log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["OPENAI"])

DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"

##########################################
#
# Azure OpenAI Helper Functions
#
##########################################

# Azure OpenAI API 版本配置
AZURE_OPENAI_API_VERSIONS = {
    "default": "2024-06-01",  # 最新穩定版本
    "stable": [
        "2024-06-01",
        "2024-02-15-preview",
        "2023-12-01-preview",
        "2023-09-15-preview"
    ],
    "preview": [
        "2024-08-01-preview",
        "2024-07-01-preview",
        "2024-06-01",
        "2024-02-15-preview"
    ],
    "fallback_order": [
        "2024-06-01",           # 最新穩定版
        "2024-02-15-preview",   # 較新預覽版
        "2023-12-01-preview",   # 已驗證可用版本
        "2023-09-15-preview"    # 舊版本備用
    ]
}

def get_azure_api_version(api_config: dict = None, prefer_stable: bool = True) -> str:
    """
    獲取 Azure OpenAI API 版本

    Args:
        api_config: API 配置字典
        prefer_stable: 是否優先使用穩定版本

    Returns:
        API 版本字符串
    """
    # 1. 如果配置中指定了版本，優先使用
    if api_config and api_config.get("api_version"):
        return api_config["api_version"]

    # 2. 根據偏好選擇版本類型
    if prefer_stable:
        return AZURE_OPENAI_API_VERSIONS["stable"][0]
    else:
        return AZURE_OPENAI_API_VERSIONS["preview"][0]

def get_azure_api_version_fallback_list(api_config: dict = None) -> list:
    """
    獲取 Azure OpenAI API 版本回退列表

    Args:
        api_config: API 配置字典

    Returns:
        API 版本列表，按優先級排序
    """
    # 如果配置中指定了版本，將其放在第一位
    if api_config and api_config.get("api_version"):
        specified_version = api_config["api_version"]
        fallback_list = [specified_version]
        # 添加其他版本作為備用
        for version in AZURE_OPENAI_API_VERSIONS["fallback_order"]:
            if version != specified_version:
                fallback_list.append(version)
        return fallback_list

    # 否則使用默認回退順序
    return AZURE_OPENAI_API_VERSIONS["fallback_order"].copy()

def build_azure_openai_url(base_url: str, deployment_name: str, api_version: str = None) -> str:
    """
    構建 Azure OpenAI API URL
    Args:
        base_url: Azure OpenAI 資源的基礎 URL (如: https://your-resource.openai.azure.com)
        deployment_name: 部署名稱
        api_version: API 版本（如果未提供，使用默認版本）
    Returns:
        完整的 Azure OpenAI API URL
    """
    if api_version is None:
        api_version = get_azure_api_version()

    # 移除可能的尾隨斜線
    base_url = base_url.rstrip('/')
    return f"{base_url}/openai/deployments/{deployment_name}/chat/completions?api-version={api_version}"

def get_azure_headers(api_key: str, user: UserModel = None) -> dict:
    """
    構建 Azure OpenAI 請求標頭
    Args:
        api_key: Azure OpenAI API 金鑰
        user: 用戶模型（可選）
    Returns:
        Azure OpenAI 請求標頭字典
    """
    headers = {
        "api-key": api_key,
        "Content-Type": "application/json",
    }

    # 添加用戶信息標頭（如果啟用）
    if ENABLE_FORWARD_USER_INFO_HEADERS and user:
        headers.update({
            "X-OpenWebUI-User-Name": user.name,
            "X-OpenWebUI-User-Id": user.id,
            "X-OpenWebUI-User-Email": user.email,
            "X-OpenWebUI-User-Role": user.role,
        })

    return headers

def is_azure_openai_provider(api_config: dict) -> bool:
    """
    檢查是否為 Azure OpenAI 提供者
    Args:
        api_config: API 配置字典
    Returns:
        True 如果是 Azure OpenAI 提供者，否則 False
    """
    return api_config.get("provider_type") == "azure"

def get_azure_deployment_name(api_config: dict, model_name: str) -> str:
    """
    獲取 Azure OpenAI 部署名稱
    Args:
        api_config: API 配置字典
        model_name: 模型名稱
    Returns:
        部署名稱
    """
    deployment_mapping = api_config.get("deployment_mapping", {})
    return deployment_mapping.get(model_name, model_name)

##########################################
#
# Utility functions
#
##########################################


async def send_get_request(url, key=None, user: UserModel = None):
    timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(
                url,
                headers={
                    **({"Authorization": f"Bearer {key}"} if key else {}),
                    **(
                        {
                            "X-OpenWebUI-User-Name": user.name,
                            "X-OpenWebUI-User-Id": user.id,
                            "X-OpenWebUI-User-Email": user.email,
                            "X-OpenWebUI-User-Role": user.role,
                        }
                        if ENABLE_FORWARD_USER_INFO_HEADERS and user
                        else {}
                    ),
                },
            ) as response:
                return await response.json()
    except Exception as e:
        # Handle connection error here
        log.error(f"Connection error: {e}")
        return None


async def cleanup_response(
    response: Optional[aiohttp.ClientResponse],
    session: Optional[aiohttp.ClientSession],
):
    if response:
        response.close()
    if session:
        await session.close()


def openai_o1_o3_handler(payload):
    """
    Handle o1, o3 specific parameters
    """
    if "max_tokens" in payload:
        # Remove "max_tokens" from the payload
        payload["max_completion_tokens"] = payload["max_tokens"]
        del payload["max_tokens"]

    # Fix: o1 and o3 do not support the "system" role directly.
    # For older models like "o1-mini" or "o1-preview", use role "user".
    # For newer o1/o3 models, replace "system" with "developer".
    if payload["messages"][0]["role"] == "system":
        model_lower = payload["model"].lower()
        if model_lower.startswith("o1-mini") or model_lower.startswith("o1-preview"):
            payload["messages"][0]["role"] = "user"
        else:
            payload["messages"][0]["role"] = "developer"

    return payload


##########################################
#
# API routes
#
##########################################

router = APIRouter()


@router.get("/config")
async def get_config(request: Request, user=Depends(get_admin_user)):
    return {
        "ENABLE_OPENAI_API": request.app.state.config.ENABLE_OPENAI_API,
        "OPENAI_API_BASE_URLS": request.app.state.config.OPENAI_API_BASE_URLS,
        "OPENAI_API_KEYS": request.app.state.config.OPENAI_API_KEYS,
        "OPENAI_API_CONFIGS": request.app.state.config.OPENAI_API_CONFIGS,
    }


class OpenAIConfigForm(BaseModel):
    ENABLE_OPENAI_API: Optional[bool] = None
    OPENAI_API_BASE_URLS: list[str]
    OPENAI_API_KEYS: list[str]
    OPENAI_API_CONFIGS: dict


@router.post("/config/update")
async def update_config(
    request: Request, form_data: OpenAIConfigForm, user=Depends(get_admin_user)
):
    request.app.state.config.ENABLE_OPENAI_API = form_data.ENABLE_OPENAI_API
    request.app.state.config.OPENAI_API_BASE_URLS = form_data.OPENAI_API_BASE_URLS
    request.app.state.config.OPENAI_API_KEYS = form_data.OPENAI_API_KEYS

    # Check if API KEYS length is same than API URLS length
    if len(request.app.state.config.OPENAI_API_KEYS) != len(
        request.app.state.config.OPENAI_API_BASE_URLS
    ):
        if len(request.app.state.config.OPENAI_API_KEYS) > len(
            request.app.state.config.OPENAI_API_BASE_URLS
        ):
            request.app.state.config.OPENAI_API_KEYS = (
                request.app.state.config.OPENAI_API_KEYS[
                    : len(request.app.state.config.OPENAI_API_BASE_URLS)
                ]
            )
        else:
            request.app.state.config.OPENAI_API_KEYS += [""] * (
                len(request.app.state.config.OPENAI_API_BASE_URLS)
                - len(request.app.state.config.OPENAI_API_KEYS)
            )

    request.app.state.config.OPENAI_API_CONFIGS = form_data.OPENAI_API_CONFIGS

    # Remove the API configs that are not in the API URLS
    keys = list(map(str, range(len(request.app.state.config.OPENAI_API_BASE_URLS))))
    request.app.state.config.OPENAI_API_CONFIGS = {
        key: value
        for key, value in request.app.state.config.OPENAI_API_CONFIGS.items()
        if key in keys
    }

    return {
        "ENABLE_OPENAI_API": request.app.state.config.ENABLE_OPENAI_API,
        "OPENAI_API_BASE_URLS": request.app.state.config.OPENAI_API_BASE_URLS,
        "OPENAI_API_KEYS": request.app.state.config.OPENAI_API_KEYS,
        "OPENAI_API_CONFIGS": request.app.state.config.OPENAI_API_CONFIGS,
    }


@router.post("/audio/speech")
async def speech(request: Request, user=Depends(get_verified_user)):
    idx = None
    try:
        idx = request.app.state.config.OPENAI_API_BASE_URLS.index(
            "https://api.openai.com/v1"
        )

        body = await request.body()
        name = hashlib.sha256(body).hexdigest()

        SPEECH_CACHE_DIR = CACHE_DIR / "audio" / "speech"
        SPEECH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        file_path = SPEECH_CACHE_DIR.joinpath(f"{name}.mp3")
        file_body_path = SPEECH_CACHE_DIR.joinpath(f"{name}.json")

        # Check if the file already exists in the cache
        if file_path.is_file():
            return FileResponse(file_path)

        url = request.app.state.config.OPENAI_API_BASE_URLS[idx]

        r = None
        try:
            r = requests.post(
                url=f"{url}/audio/speech",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {request.app.state.config.OPENAI_API_KEYS[idx]}",
                    **(
                        {
                            "HTTP-Referer": "https://openwebui.com/",
                            "X-Title": "Open WebUI",
                        }
                        if "openrouter.ai" in url
                        else {}
                    ),
                    **(
                        {
                            "X-OpenWebUI-User-Name": user.name,
                            "X-OpenWebUI-User-Id": user.id,
                            "X-OpenWebUI-User-Email": user.email,
                            "X-OpenWebUI-User-Role": user.role,
                        }
                        if ENABLE_FORWARD_USER_INFO_HEADERS
                        else {}
                    ),
                },
                stream=True,
            )

            r.raise_for_status()

            # Save the streaming content to a file
            with open(file_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)

            with open(file_body_path, "w") as f:
                json.dump(json.loads(body.decode("utf-8")), f)

            # Return the saved file
            return FileResponse(file_path)

        except Exception as e:
            log.exception(e)

            detail = None
            if r is not None:
                try:
                    res = r.json()
                    if "error" in res:
                        detail = f"External: {res['error']}"
                except Exception:
                    detail = f"External: {e}"

            raise HTTPException(
                status_code=r.status_code if r else 500,
                detail=detail if detail else "Open WebUI: Server Connection Error",
            )

    except ValueError:
        raise HTTPException(status_code=401, detail=ERROR_MESSAGES.OPENAI_NOT_FOUND)


async def get_all_models_responses(request: Request, user: UserModel) -> list:
    if not request.app.state.config.ENABLE_OPENAI_API:
        return []

    # Check if API KEYS length is same than API URLS length
    num_urls = len(request.app.state.config.OPENAI_API_BASE_URLS)
    num_keys = len(request.app.state.config.OPENAI_API_KEYS)

    if num_keys != num_urls:
        # if there are more keys than urls, remove the extra keys
        if num_keys > num_urls:
            new_keys = request.app.state.config.OPENAI_API_KEYS[:num_urls]
            request.app.state.config.OPENAI_API_KEYS = new_keys
        # if there are more urls than keys, add empty keys
        else:
            request.app.state.config.OPENAI_API_KEYS += [""] * (num_urls - num_keys)

    request_tasks = []
    for idx, url in enumerate(request.app.state.config.OPENAI_API_BASE_URLS):
        api_config = request.app.state.config.OPENAI_API_CONFIGS.get(
            str(idx),
            request.app.state.config.OPENAI_API_CONFIGS.get(url, {})  # Legacy support
        )

        enable = api_config.get("enable", True)
        model_ids = api_config.get("model_ids", [])

        # 確保停用的連接不會被處理
        if not enable:
            log.info(f"Connection {idx} ({url}) is disabled, skipping")
            request_tasks.append(asyncio.ensure_future(asyncio.sleep(0, {"data": []})))
            continue

        # 檢查是否為 Azure OpenAI 提供者
        is_azure_provider = is_azure_openai_provider(api_config)

        if is_azure_provider:
            # Azure OpenAI: 獲取部署清單
            request_tasks.append(get_azure_deployments(
                url,
                request.app.state.config.OPENAI_API_KEYS[idx],
                api_config,
                user,
                idx
            ))
        elif len(model_ids) == 0:
            # 標準 OpenAI: 獲取模型清單
            # 添加錯誤處理，確保即使請求失敗也不會影響其他連接
            async def safe_get_models():
                try:
                    result = await send_get_request(
                        f"{url}/models",
                        request.app.state.config.OPENAI_API_KEYS[idx],
                        user=user,
                    )
                    return result if result is not None else {"data": []}
                except Exception as e:
                    log.error(f"Error getting models from connection {idx} ({url}): {e}")
                    return {"data": []}

            request_tasks.append(safe_get_models())
        else:
            # 使用配置的模型 ID 清單
            model_list = {
                "object": "list",
                "data": [
                    {
                        "id": model_id,
                        "name": model_id,
                        "owned_by": "openai",
                        "openai": {"id": model_id},
                        "urlIdx": idx,
                    }
                    for model_id in model_ids
                ],
            }
            request_tasks.append(asyncio.ensure_future(asyncio.sleep(0, model_list)))

    responses = await asyncio.gather(*request_tasks)

    for idx, response in enumerate(responses):
        if response:
            url = request.app.state.config.OPENAI_API_BASE_URLS[idx]
            api_config = request.app.state.config.OPENAI_API_CONFIGS.get(
                str(idx),
                request.app.state.config.OPENAI_API_CONFIGS.get(
                    url, {}
                ),  # Legacy support
            )

            prefix_id = api_config.get("prefix_id", None)
            tags = api_config.get("tags", [])

            if prefix_id:
                for model in (
                    response if isinstance(response, list) else response.get("data", [])
                ):
                    model["id"] = f"{prefix_id}.{model['id']}"

            if tags:
                for model in (
                    response if isinstance(response, list) else response.get("data", [])
                ):
                    model["tags"] = tags

    log.debug(f"get_all_models:responses() {responses}")
    return responses


async def get_azure_deployments(url: str, key: str, api_config: dict, user: UserModel, idx: int):
    """
    獲取 Azure OpenAI 的部署清單
    優先使用手動配置的部署映射，只有在沒有配置時才嘗試自動獲取
    """
    api_version = get_azure_api_version(api_config)

    log.info(f"=== Azure Deployments Debug Info ===")
    log.info(f"URL: {url}")
    log.info(f"API Key: {key[:8]}...{key[-4:] if len(key) > 8 else '***'}")
    log.info(f"Configured API Version: {api_version}")
    log.info(f"Deployment Mapping: {api_config.get('deployment_mapping', {})}")

    # 優先檢查是否有手動配置的部署映射
    deployment_mapping = api_config.get("deployment_mapping", {})
    if deployment_mapping:
        log.info(f"Using manual deployment mapping with {len(deployment_mapping)} deployments")
        return await get_fallback_deployments(api_config, api_version, idx)

    # 只有在沒有手動配置時才嘗試自動獲取
    log.info("No manual deployment mapping found, attempting automatic discovery")

    try:
        # 使用 Azure OpenAI Python SDK 來獲取部署清單
        # 這是基於 testAzureGemini.py 的成功實現
        from openai import AzureOpenAI

        # 初始化 Azure OpenAI 客戶端
        client = AzureOpenAI(
            api_key=key,
            api_version=api_version,
            azure_endpoint=url.rstrip('/')
        )

        log.info("Using Azure OpenAI SDK to get deployments list...")

        # 使用同步方法獲取部署清單，然後在異步函數中處理
        import asyncio
        loop = asyncio.get_event_loop()

        # 在線程池中運行同步的 SDK 調用
        def get_deployments_sync():
            try:
                deployments_list = client.models.list()
                return deployments_list
            except Exception as e:
                log.error(f"Error calling client.models.list(): {e}")
                return None

        # 在執行器中運行同步函數
        deployments_list = await loop.run_in_executor(None, get_deployments_sync)

        if deployments_list is None:
            log.error("Failed to get deployments from Azure OpenAI SDK")
            # 如果 SDK 方法失敗，回退到配置的部署映射
            return await get_fallback_deployments(api_config, api_version, idx)

        # 將部署清單轉換為模型格式
        models = []
        deployment_count = 0

        for deployment in deployments_list:
            deployment_id = deployment.id
            # 嘗試獲取基礎模型名稱
            underlying_model = getattr(deployment, 'model', deployment_id)

            # 如果 underlying_model 是 'N/A' 或空，使用 deployment_id
            if not underlying_model or underlying_model == 'N/A':
                underlying_model = deployment_id

            # 檢查部署映射，如果有映射則使用映射的模型名稱作為顯示
            deployment_mapping = api_config.get("deployment_mapping", {})

            # 使用部署 ID 作為模型 ID（這樣發送請求時才能正確映射）
            # 顯示名稱使用：部署名稱 (模型名稱) 的格式，讓使用者清楚看到
            if underlying_model != deployment_id:
                display_name = f"{deployment_id} ({underlying_model})"
            else:
                display_name = deployment_id

            models.append({
                "id": deployment_id,  # 使用部署 ID 作為模型 ID
                "name": display_name,  # 顯示名稱包含完整資訊
                "owned_by": "azure",
                "object": "model",
                "azure": {
                    "deployment_id": deployment_id,
                    "model": underlying_model,
                    "api_version": api_version
                },
                "urlIdx": idx,
            })
            deployment_count += 1

            # 記錄前10個部署的詳細信息以便調試
            if deployment_count <= 10:
                log.info(f"Deployment {deployment_count}: ID={deployment_id}, Model={underlying_model}, Display={display_name}")

        log.info(f"Successfully retrieved {len(models)} deployments using Azure OpenAI SDK")

        # 如果沒有找到任何部署，使用回退方法
        if not models:
            log.warning("No deployments found via SDK, trying fallback methods")
            return await get_fallback_deployments(api_config, api_version, idx)

        return {
            "object": "list",
            "data": models
        }

    except ImportError:
        log.error("Azure OpenAI SDK not available, falling back to REST API method")
        # 如果沒有安裝 Azure OpenAI SDK，回退到原有的 REST API 方法
        return await get_azure_deployments_rest_api(url, key, api_config, user, idx)
    except Exception as e:
        log.error(f"Exception using Azure OpenAI SDK: {e}")
        # 如果 SDK 方法失敗，回退到 REST API 方法
        return await get_azure_deployments_rest_api(url, key, api_config, user, idx)


async def get_fallback_deployments(api_config: dict, api_version: str, idx: int):
    """回退到配置的部署映射或默認部署"""
    # 優先使用配置的部署映射
    deployment_mapping = api_config.get("deployment_mapping", {})
    if deployment_mapping:
        models = []
        for model_name, deployment_id in deployment_mapping.items():
            models.append({
                "id": model_name,
                "name": model_name,
                "owned_by": "azure",
                "object": "model",
                "azure": {
                    "deployment_id": deployment_id,
                    "model": model_name,
                    "api_version": api_version
                },
                "urlIdx": idx,
            })

        log.info(f"Using deployment mapping fallback with {len(models)} models")
        return {
            "object": "list",
            "data": models
        }

    # 如果沒有映射，返回空列表
    log.warning("No deployment mapping found and SDK failed, returning empty list")
    return {
        "object": "list",
        "data": []
    }


async def get_azure_deployments_rest_api(url: str, key: str, api_config: dict, user: UserModel, idx: int):
    """
    使用 Azure OpenAI REST API 直接調用獲取部署（第二層回退方法）
    注意：這不是標準 OpenAI API，而是 Azure OpenAI 的部署管理 API
    調用端點：{azure_endpoint}/openai/deployments?api-version={version}
    """
    log.info("Falling back to REST API method for Azure deployments")

    # 使用動態版本管理系統獲取回退版本列表
    possible_versions = get_azure_api_version_fallback_list(api_config)

    timeout = aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST)

    for version in possible_versions:
        deployments_url = f"{url.rstrip('/')}/openai/deployments?api-version={version}"

        try:
            log.info(f"Trying REST API deployments URL: {deployments_url}")

            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                headers = get_azure_headers(key, user)

                async with session.get(
                    deployments_url,
                    headers=headers
                ) as response:
                    log.info(f"REST API response status for API version {version}: {response.status}")

                    if response.status == 200:
                        try:
                            response_text = await response.text()
                            deployments_data = json.loads(response_text)
                            deployments = deployments_data.get('data', [])

                            log.info(f"Found {len(deployments)} deployments via REST API: {[d.get('id', 'no-id') for d in deployments]}")

                            # 將部署轉換為模型格式
                            models = []
                            for deployment in deployments:
                                deployment_id = deployment.get('id', '')
                                model_name = deployment.get('model', deployment_id)

                                # 檢查部署映射
                                deployment_mapping = api_config.get("deployment_mapping", {})
                                reverse_mapping = {v: k for k, v in deployment_mapping.items()}
                                display_name = reverse_mapping.get(deployment_id, model_name)

                                models.append({
                                    "id": display_name,
                                    "name": display_name,
                                    "owned_by": "azure",
                                    "object": "model",
                                    "azure": {
                                        "deployment_id": deployment_id,
                                        "model": model_name,
                                        "api_version": version
                                    },
                                    "urlIdx": idx,
                                })

                            log.info(f"Successfully retrieved {len(models)} deployments with REST API version {version}")
                            return {
                                "object": "list",
                                "data": models
                            }
                        except json.JSONDecodeError as e:
                            log.warning(f"JSON decode error with REST API version {version}: {e}")
                            continue
                    elif response.status == 404:
                        log.warning(f"REST API deployments not available with version {version} (404)")
                        continue
                    elif response.status == 403:
                        log.warning(f"Access denied (403) for REST API with version {version}")
                        # 如果是網路限制，跳出循環
                        break
                    elif response.status == 401:
                        log.warning(f"Authentication failed (401) for REST API version {version}")
                        continue
                    else:
                        log.warning(f"Failed to get deployments with REST API version {version}: {response.status}")
                        continue

        except Exception as e:
            log.error(f"Exception getting Azure deployments with REST API version {version}: {e}")
            continue

    # 如果 REST API 也失敗，使用回退方法
    return await get_fallback_deployments(api_config, get_azure_api_version(api_config), idx)


async def get_filtered_models(models, user):
    """Filter models based on user access control"""
    filtered_models = []
    for model in models.get("data", []):
        model_id = model.get("id", "")
        model_info = Models.get_model_by_id(model_id)
        if model_info:
            if user.id == model_info.user_id or has_access(
                user.id, type="read", access_control=model_info.access_control
            ):
                filtered_models.append(model)
        else:
            # If no model info found, include the model (default behavior)
            filtered_models.append(model)
    return filtered_models


@cached(ttl=1)
async def get_all_models(request: Request, user: UserModel) -> dict[str, list]:
    log.info("get_all_models()")

    if not request.app.state.config.ENABLE_OPENAI_API:
        return {"data": []}

    responses = await get_all_models_responses(request, user=user)

    def extract_data(response):
        if response and "data" in response:
            return response["data"]
        if isinstance(response, list):
            return response
        return None

    def merge_models_lists(model_lists):
        log.debug(f"merge_models_lists {model_lists}")
        merged_list = []

        for idx, models in enumerate(model_lists):
            if models is not None and "error" not in models:

                merged_list.extend(
                    [
                        {
                            **model,
                            "name": model.get("name", model["id"]),
                            "owned_by": "openai",
                            "openai": model,
                            "urlIdx": idx,
                        }
                        for model in models
                        if (model.get("id") or model.get("name"))
                        and (
                            "api.openai.com"
                            not in request.app.state.config.OPENAI_API_BASE_URLS[idx]
                            or not any(
                                name in model["id"]
                                for name in [
                                    "babbage",
                                    "dall-e",
                                    "davinci",
                                    "embedding",
                                    "tts",
                                    "whisper",
                                ]
                            )
                        )
                    ]
                )

        return merged_list

    models = {"data": merge_models_lists(map(extract_data, responses))}
    log.debug(f"models: {models}")

    request.app.state.OPENAI_MODELS = {model["id"]: model for model in models["data"]}
    return models


@router.get("/models")
@router.get("/models/{url_idx}")
async def get_models(
    request: Request, url_idx: Optional[int] = None, user=Depends(get_verified_user)
):
    models = {
        "data": [],
    }

    if url_idx is None:
        models = await get_all_models(request, user=user)
    else:
        # 检查指定连接是否存在和启用
        if url_idx >= len(request.app.state.config.OPENAI_API_BASE_URLS):
            raise HTTPException(status_code=404, detail="Connection index not found")

        # 获取连接配置并检查是否启用
        api_config = request.app.state.config.OPENAI_API_CONFIGS.get(
            str(url_idx),
            request.app.state.config.OPENAI_API_CONFIGS.get(
                request.app.state.config.OPENAI_API_BASE_URLS[url_idx], {}
            )
        )

        enable = api_config.get("enable", True)
        if not enable:
            # 如果连接被停用，返回空的模型列表而不是错误
            log.info(f"Connection {url_idx} is disabled, returning empty model list")
            return {"data": []}

        url = request.app.state.config.OPENAI_API_BASE_URLS[url_idx]
        key = request.app.state.config.OPENAI_API_KEYS[url_idx]

        # 检查是否为 Azure OpenAI 提供者
        is_azure_provider = is_azure_openai_provider(api_config)

        if is_azure_provider:
            # 对于 Azure OpenAI，使用专门的部署获取逻辑
            try:
                azure_models = await get_azure_deployments(url, key, api_config, user, url_idx)
                if azure_models:
                    models = azure_models
                else:
                    models = {"data": []}
            except Exception as e:
                log.error(f"Error getting Azure deployments for connection {url_idx}: {e}")
                models = {"data": []}
        else:
            # 标准 OpenAI API 处理
            r = None
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST)
            ) as session:
                try:
                    async with session.get(
                        f"{url}/models",
                        headers={
                            "Authorization": f"Bearer {key}",
                            "Content-Type": "application/json",
                            **(
                                {
                                    "X-OpenWebUI-User-Name": user.name,
                                    "X-OpenWebUI-User-Id": user.id,
                                    "X-OpenWebUI-User-Email": user.email,
                                    "X-OpenWebUI-User-Role": user.role,
                                }
                                if ENABLE_FORWARD_USER_INFO_HEADERS
                                else {}
                            ),
                        },
                    ) as r:
                        if r.status != 200:
                            # Extract response error details if available
                            error_detail = f"HTTP Error: {r.status}"
                            try:
                                res = await r.json()
                                if "error" in res:
                                    error_detail = f"External Error: {res['error']}"
                            except:
                                pass
                            log.error(f"Error getting models for connection {url_idx}: {error_detail}")
                            # 返回空列表而不是抛出異常，避免UI錯誤
                            return {"data": []}

                        response_data = await r.json()

                        # Check if we're calling OpenAI API based on the URL
                        if "api.openai.com" in url:
                            # Filter models according to the specified conditions
                            response_data["data"] = [
                                model
                                for model in response_data.get("data", [])
                                if not any(
                                    name in model["id"]
                                    for name in [
                                        "babbage",
                                        "dall-e",
                                        "davinci",
                                        "embedding",
                                        "tts",
                                        "whisper",
                                    ]
                                )
                            ]

                        models = response_data
                except aiohttp.ClientError as e:
                    # ClientError covers all aiohttp requests issues
                    log.exception(f"Client error for connection {url_idx}: {str(e)}")
                    # 返回空列表而不是抛出HTTP異常
                    return {"data": []}
                except Exception as e:
                    log.exception(f"Unexpected error for connection {url_idx}: {e}")
                    # 返回空列表而不是抛出HTTP異常
                    return {"data": []}

    if user.role == "user" and not BYPASS_MODEL_ACCESS_CONTROL:
        models["data"] = await get_filtered_models(models, user)

    return models


class ConnectionVerificationForm(BaseModel):
    url: str
    key: str
    config: Optional[dict] = {}


@router.post("/verify")
async def verify_connection(
    form_data: ConnectionVerificationForm, user=Depends(get_admin_user)
):
    url = form_data.url
    key = form_data.key
    config = form_data.config or {}

    # 檢查是否為 Azure OpenAI 提供者
    is_azure_provider = is_azure_openai_provider(config)

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT_MODEL_LIST)
    ) as session:
        try:
            if is_azure_provider:
                # Azure OpenAI 驗證邏輯 - 優先使用新的 SDK 方法
                try:
                    from openai import AzureOpenAI

                    api_version = config.get("api_version", "2023-12-01-preview")

                    # 使用 Azure OpenAI SDK 驗證
                    client = AzureOpenAI(
                        api_key=key,
                        api_version=api_version,
                        azure_endpoint=url.rstrip('/')
                    )

                    # 在線程池中運行同步的 SDK 調用
                    import asyncio
                    loop = asyncio.get_event_loop()

                    def verify_sync():
                        try:
                            deployments_list = client.models.list()
                            return list(deployments_list)
                        except Exception as e:
                            raise e

                    deployments = await loop.run_in_executor(None, verify_sync)

                    deployment_ids = [d.id for d in deployments]

                    return {
                        "status": "success",
                        "provider_type": "azure",
                        "api_version": api_version,
                        "available_deployments": deployment_ids,
                        "total_deployments": len(deployment_ids),
                        "message": f"連線成功，自動獲取到 {len(deployment_ids)} 個部署"
                    }

                except ImportError:
                    # 如果沒有 SDK，回退到 REST API 方法
                    api_version = config.get("api_version", "2024-02-15-preview")
                    deployments_url = f"{url.rstrip('/')}/openai/deployments?api-version={api_version}"

                    async with session.get(
                        deployments_url,
                        headers=get_azure_headers(key, user)
                    ) as r:
                        if r.status == 200:
                            deployments_data = await r.json()
                            deployments = [d['id'] for d in deployments_data.get('data', [])]

                            return {
                                "status": "success",
                                "provider_type": "azure",
                                "api_version": api_version,
                                "available_deployments": deployments,
                                "message": "連線成功，已獲取可用部署清單"
                            }
                        elif r.status == 403:
                            try:
                                error_response = await r.json()
                                error_msg = error_response.get('error', {}).get('message', 'Access denied')
                            except:
                                error_msg = "Access denied due to Virtual Network/Firewall rules"

                            raise Exception(f"Azure 網路存取限制: {error_msg}. "
                                          f"請檢查 Azure OpenAI 資源的網路設定。")
                        elif r.status == 401:
                            raise Exception("Azure 認證失敗: 請檢查 API 金鑰是否正確且有效。")
                        else:
                            # 嘗試使用配置的部署進行測試
                            deployment_mapping = config.get("deployment_mapping", {})
                            if deployment_mapping:
                                test_deployment = list(deployment_mapping.values())[0]
                                test_url = build_azure_openai_url(url, test_deployment, api_version)
                                test_payload = {
                                    "messages": [{"role": "user", "content": "test"}],
                                    "max_tokens": 1
                                }

                                async with session.post(
                                    test_url,
                                    json=test_payload,
                                    headers=get_azure_headers(key, user)
                                ) as test_r:
                                    if test_r.status in [200, 400]:
                                        return {
                                            "status": "success",
                                            "provider_type": "azure",
                                            "deployment_tested": test_deployment,
                                            "api_version": api_version,
                                            "message": "連線成功（使用測試部署驗證）"
                                        }
                                    else:
                                        error_detail = f"HTTP Error: {test_r.status}"
                                        try:
                                            error_response = await test_r.json()
                                            if "error" in error_response:
                                                error_detail = f"Azure Error: {error_response['error']}"
                                        except:
                                            pass
                                        raise Exception(error_detail)
                            else:
                                raise Exception("無法獲取部署清單，且未配置部署映射。")
            else:
                # 標準 OpenAI 驗證邏輯
                async with session.get(
                    f"{url}/models",
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                        **(
                            {
                                "X-OpenWebUI-User-Name": user.name,
                                "X-OpenWebUI-User-Id": user.id,
                                "X-OpenWebUI-User-Email": user.email,
                                "X-OpenWebUI-User-Role": user.role,
                            }
                            if ENABLE_FORWARD_USER_INFO_HEADERS
                            else {}
                        ),
                    },
                ) as r:
                    if r.status != 200:
                        error_detail = f"HTTP Error: {r.status}"
                        res = await r.json()
                        if "error" in res:
                            error_detail = f"External Error: {res['error']}"
                        raise Exception(error_detail)

                    response_data = await r.json()
                    return response_data

        except aiohttp.ClientError as e:
            log.exception(f"Client error: {str(e)}")
            raise HTTPException(
                status_code=500, detail="Open WebUI: Server Connection Error"
            )
        except Exception as e:
            log.exception(f"Unexpected error: {e}")
            error_detail = f"Unexpected error: {str(e)}"
            raise HTTPException(status_code=500, detail=error_detail)


@router.post("/chat/completions")
async def generate_chat_completion(
    request: Request,
    form_data: dict,
    user=Depends(get_verified_user),
    bypass_filter: Optional[bool] = False,
):
    if DEBUG_MODE:
        log.info(f"=== [MCP Tool 開發用] - [進入 async def generate_chat_completion 函式] form_data : {form_data}")

    if BYPASS_MODEL_ACCESS_CONTROL:
        bypass_filter = True

    idx = 0

    payload = {**form_data}
    metadata = payload.pop("metadata", None)

    model_id = form_data.get("model")
    model_info = Models.get_model_by_id(model_id)

    # Check model info and override the payload
    if model_info:
        if model_info.base_model_id:
            payload["model"] = model_info.base_model_id
            model_id = model_info.base_model_id

        params = model_info.params.model_dump()
        payload = apply_model_params_to_body_openai(params, payload)
        payload = apply_model_system_prompt_to_body(params, payload, metadata, user)

        # Check if user has access to the model
        if not bypass_filter and user.role == "user":
            if not (
                user.id == model_info.user_id
                or has_access(
                    user.id, type="read", access_control=model_info.access_control
                )
            ):
                raise HTTPException(
                    status_code=403,
                    detail="Model not found",
                )
    elif not bypass_filter:
        if user.role != "admin":
            raise HTTPException(
                status_code=403,
                detail="Model not found",
            )

    await get_all_models(request, user=user)
    model = request.app.state.OPENAI_MODELS.get(model_id)
    if model:
        idx = model["urlIdx"]
    else:
        raise HTTPException(
            status_code=404,
            detail="Model not found",
        )

    # Get the API config for the model
    api_config = request.app.state.config.OPENAI_API_CONFIGS.get(
        str(idx),
        request.app.state.config.OPENAI_API_CONFIGS.get(
            request.app.state.config.OPENAI_API_BASE_URLS[idx], {}
        ),  # Legacy support
    )

    prefix_id = api_config.get("prefix_id", None)
    if prefix_id:
        payload["model"] = payload["model"].replace(f"{prefix_id}.", "")

    # Add user info to the payload if the model is a pipeline
    if "pipeline" in model and model.get("pipeline"):
        payload["user"] = {
            "name": user.name,
            "id": user.id,
            "email": user.email,
            "role": user.role,
        }

    url = request.app.state.config.OPENAI_API_BASE_URLS[idx]
    key = request.app.state.config.OPENAI_API_KEYS[idx]

    # 檢查是否為 Azure OpenAI 提供者
    is_azure_provider = is_azure_openai_provider(api_config)

    # 根據提供者類型構建 URL 和標頭
    if is_azure_provider:
        # Azure OpenAI 處理邏輯
        deployment_name = get_azure_deployment_name(api_config, payload["model"])
        api_version = api_config.get("api_version", "2024-02-15-preview")
        final_url = build_azure_openai_url(url, deployment_name, api_version)
        headers = get_azure_headers(key, user)
    else:
        # 標準 OpenAI 處理邏輯
        final_url = f"{url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            **(
                {
                    "HTTP-Referer": "https://openwebui.com/",
                    "X-Title": "Open WebUI",
                }
                if "openrouter.ai" in url
                else {}
            ),
            **(
                {
                    "X-OpenWebUI-User-Name": user.name,
                    "X-OpenWebUI-User-Id": user.id,
                    "X-OpenWebUI-User-Email": user.email,
                    "X-OpenWebUI-User-Role": user.role,
                }
                if ENABLE_FORWARD_USER_INFO_HEADERS
                else {}
            ),
        }

    # Fix: o1,o3 does not support the "max_tokens" parameter, Modify "max_tokens" to "max_completion_tokens"
    is_o1_o3 = payload["model"].lower().startswith(("o1", "o3-"))
    if is_o1_o3:
        payload = openai_o1_o3_handler(payload)
    elif "api.openai.com" not in url and not is_azure_provider:
        # Remove "max_completion_tokens" from the payload for backward compatibility
        if "max_completion_tokens" in payload:
            payload["max_tokens"] = payload["max_completion_tokens"]
            del payload["max_completion_tokens"]

    if "max_tokens" in payload and "max_completion_tokens" in payload:
        del payload["max_tokens"]

    # Convert the modified body back to JSON
    if "logit_bias" in payload:
        payload["logit_bias"] = json.loads(
            convert_logit_bias_input_to_json(payload["logit_bias"])
        )

    payload = json.dumps(payload)

    r = None
    session = None
    streaming = False
    response = None

    try:
        session = aiohttp.ClientSession(
            trust_env=True, timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)
        )

        r = await session.request(
            method="POST",
            url=final_url,
            data=payload,
            headers=headers,
        )

        # Check if response is SSE
        if "text/event-stream" in r.headers.get("Content-Type", ""):
            streaming = True
            return StreamingResponse(
                r.content,
                status_code=r.status,
                headers=dict(r.headers),
                background=BackgroundTask(
                    cleanup_response, response=r, session=session
                ),
            )
        else:
            try:
                response = await r.json()
            except Exception as e:
                log.error(e)
                response = await r.text()

            r.raise_for_status()
            return response
    except Exception as e:
        log.exception(e)

        detail = None
        if isinstance(response, dict):
            if "error" in response:
                detail = f"{response['error']['message'] if 'message' in response['error'] else response['error']}"
        elif isinstance(response, str):
            detail = response

        raise HTTPException(
            status_code=r.status if r else 500,
            detail=detail if detail else "Open WebUI: Server Connection Error",
        )
    finally:
        if not streaming and session:
            if r:
                r.close()
            await session.close()


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy(path: str, request: Request, user=Depends(get_verified_user)):
    """
    Deprecated: proxy all requests to OpenAI API
    """

    body = await request.body()

    idx = 0
    url = request.app.state.config.OPENAI_API_BASE_URLS[idx]
    key = request.app.state.config.OPENAI_API_KEYS[idx]

    r = None
    session = None
    streaming = False

    try:
        session = aiohttp.ClientSession(trust_env=True)
        r = await session.request(
            method=request.method,
            url=f"{url}/{path}",
            data=body,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                **(
                    {
                        "X-OpenWebUI-User-Name": user.name,
                        "X-OpenWebUI-User-Id": user.id,
                        "X-OpenWebUI-User-Email": user.email,
                        "X-OpenWebUI-User-Role": user.role,
                    }
                    if ENABLE_FORWARD_USER_INFO_HEADERS
                    else {}
                ),
            },
        )
        r.raise_for_status()

        # Check if response is SSE
        if "text/event-stream" in r.headers.get("Content-Type", ""):
            streaming = True
            return StreamingResponse(
                r.content,
                status_code=r.status,
                headers=dict(r.headers),
                background=BackgroundTask(
                    cleanup_response, response=r, session=session
                ),
            )
        else:
            response_data = await r.json()
            return response_data

    except Exception as e:
        log.exception(e)

        detail = None
        if r is not None:
            try:
                res = await r.json()
                log.error(res)
                if "error" in res:
                    detail = f"External: {res['error']['message'] if 'message' in res['error'] else res['error']}"
            except Exception:
                detail = f"External: {e}"
        raise HTTPException(
            status_code=r.status if r else 500,
            detail=detail if detail else "Open WebUI: Server Connection Error",
        )
    finally:
        if not streaming and session:
            if r:
                r.close()
            await session.close()
