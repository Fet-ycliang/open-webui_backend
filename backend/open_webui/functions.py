import logging
import sys
import inspect
import json
import asyncio

from pydantic import BaseModel
from typing import AsyncGenerator, Generator, Iterator
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from starlette.responses import Response, StreamingResponse


from open_webui.socket.main import (
    get_event_call,
    get_event_emitter,
)


from open_webui.models.functions import Functions
from open_webui.models.models import Models
from open_webui.models.chats import Chat, Chats

from open_webui.utils.plugin import load_function_module_by_id
from open_webui.utils.tools import get_tools, get_tool_servers_data
from open_webui.utils.access_control import has_access

from open_webui.env import SRC_LOG_LEVELS, GLOBAL_LOG_LEVEL

from open_webui.utils.misc import (
    add_or_update_system_message,
    get_last_user_message,
    prepend_to_first_user_message_content,
    openai_chat_chunk_message_template,
    openai_chat_completion_message_template,
)
from open_webui.utils.payload import (
    apply_model_params_to_body_openai,
    apply_model_system_prompt_to_body,
)


logging.basicConfig(stream=sys.stdout, level=GLOBAL_LOG_LEVEL)
log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["MAIN"])


import os
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"


def get_function_module_by_id(request: Request, pipe_id: str):
    # Check if function is already loaded
    if pipe_id not in request.app.state.FUNCTIONS:
        function_module, _, _ = load_function_module_by_id(pipe_id)
        request.app.state.FUNCTIONS[pipe_id] = function_module
    else:
        function_module = request.app.state.FUNCTIONS[pipe_id]

    if hasattr(function_module, "valves") and hasattr(function_module, "Valves"):
        valves = Functions.get_function_valves_by_id(pipe_id)
        function_module.valves = function_module.Valves(**(valves if valves else {}))
    return function_module


async def get_function_models(request):
    pipes = Functions.get_functions_by_type("pipe", active_only=True)
    pipe_models = []

    for pipe in pipes:
        function_module = get_function_module_by_id(request, pipe.id)

        # Check if function is a manifold
        if hasattr(function_module, "pipes"):
            sub_pipes = []

            # Handle pipes being a list, sync function, or async function
            try:
                if callable(function_module.pipes):
                    if asyncio.iscoroutinefunction(function_module.pipes):
                        sub_pipes = await function_module.pipes()
                    else:
                        sub_pipes = function_module.pipes()
                else:
                    sub_pipes = function_module.pipes
            except Exception as e:
                log.exception(e)
                sub_pipes = []

            log.debug(
                f"get_function_models: function '{pipe.id}' is a manifold of {sub_pipes}"
            )

            for p in sub_pipes:
                sub_pipe_id = f'{pipe.id}.{p["id"]}'
                sub_pipe_name = p["name"]

                if hasattr(function_module, "name"):
                    sub_pipe_name = f"{function_module.name}{sub_pipe_name}"

                pipe_flag = {"type": pipe.type}

                pipe_models.append(
                    {
                        "id": sub_pipe_id,
                        "name": sub_pipe_name,
                        "object": "model",
                        "created": pipe.created_at,
                        "owned_by": "openai",
                        "pipe": pipe_flag,
                    }
                )
        else:
            pipe_flag = {"type": "pipe"}

            log.debug(
                f"get_function_models: function '{pipe.id}' is a single pipe {{ 'id': {pipe.id}, 'name': {pipe.name} }}"
            )

            pipe_models.append(
                {
                    "id": pipe.id,
                    "name": pipe.name,
                    "object": "model",
                    "created": pipe.created_at,
                    "owned_by": "openai",
                    "pipe": pipe_flag,
                }
            )

    return pipe_models


async def generate_function_chat_completion(
    request, form_data, user, models: dict = {}
):
    if DEBUG_MODE:
        log.info(f"=== [MCP Tool 開發用] - [進入 async def generate_function_chat_completion 函式] form_data : {form_data}")
    async def execute_pipe(pipe, params):
        if inspect.iscoroutinefunction(pipe):
            return await pipe(**params)
        else:
            return pipe(**params)

    async def get_message_content(res: str | Generator | AsyncGenerator) -> str:
        if isinstance(res, str):
            return res
        if isinstance(res, Generator):
            return "".join(map(str, res))
        if isinstance(res, AsyncGenerator):
            return "".join([str(stream) async for stream in res])

    def process_line(form_data: dict, line):
        if isinstance(line, BaseModel):
            line = line.model_dump_json()
            line = f"data: {line}"
        if isinstance(line, dict):
            line = f"data: {json.dumps(line)}"

        try:
            line = line.decode("utf-8")
        except Exception:
            pass

        if line.startswith("data:"):
            return f"{line}\n\n"
        else:
            line = openai_chat_chunk_message_template(form_data["model"], line)
            return f"data: {json.dumps(line)}\n\n"

    def get_pipe_id(form_data: dict) -> str:
        pipe_id = form_data["model"]
        if "." in pipe_id:
            pipe_id, _ = pipe_id.split(".", 1)
        return pipe_id

    def get_function_params(function_module, form_data, user, extra_params=None):
        if extra_params is None:
            extra_params = {}

        pipe_id = get_pipe_id(form_data)

        # Get the signature of the function
        sig = inspect.signature(function_module.pipe)
        params = {"body": form_data} | {
            k: v for k, v in extra_params.items() if k in sig.parameters
        }

        if "__user__" in params and hasattr(function_module, "UserValves"):
            user_valves = Functions.get_user_valves_by_id_and_user_id(pipe_id, user.id)
            try:
                params["__user__"]["valves"] = function_module.UserValves(**user_valves)
            except Exception as e:
                log.exception(e)
                params["__user__"]["valves"] = function_module.UserValves()

        return params

    model_id = form_data.get("model")
    model_info = Models.get_model_by_id(model_id)

    metadata = form_data.pop("metadata", {})

    files = metadata.get("files", [])
    tool_ids = metadata.get("tool_ids", [])
    # Check if tool_ids is None
    if tool_ids is None:
        tool_ids = []

    __event_emitter__ = None
    __event_call__ = None
    __task__ = None
    __task_body__ = None

    if metadata:
        if all(k in metadata for k in ("session_id", "chat_id", "message_id")):
            __event_emitter__ = get_event_emitter(metadata)
            __event_call__ = get_event_call(metadata)
        __task__ = metadata.get("task", None)
        __task_body__ = metadata.get("task_body", None)

    extra_params = {
        "__event_emitter__": __event_emitter__,
        "__event_call__": __event_call__,
        "__chat_id__": metadata.get("chat_id", None),
        "__session_id__": metadata.get("session_id", None),
        "__message_id__": metadata.get("message_id", None),
        "__task__": __task__,
        "__task_body__": __task_body__,
        "__files__": files,
        "__user__": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
        },
        "__metadata__": metadata,
        "__request__": request,
    }
    extra_params["__tools__"] = get_tools(
        request,
        tool_ids,
        user,
        {
            **extra_params,
            "__model__": models.get(form_data["model"], None),
            "__messages__": form_data["messages"],
            "__files__": files,
        },
    )

    if model_info:
        if model_info.base_model_id:
            form_data["model"] = model_info.base_model_id

        params = model_info.params.model_dump()
        form_data = apply_model_params_to_body_openai(params, form_data)
        form_data = apply_model_system_prompt_to_body(params, form_data, metadata, user)

    pipe_id = get_pipe_id(form_data)
    function_module = get_function_module_by_id(request, pipe_id)

    pipe = function_module.pipe
    params = get_function_params(function_module, form_data, user, extra_params)

    if form_data.get("stream", False):

        async def stream_content():
            try:
                res = await execute_pipe(pipe, params)

                # Directly return if the response is a StreamingResponse
                if isinstance(res, StreamingResponse):
                    async for data in res.body_iterator:
                        yield data
                    return
                if isinstance(res, dict):
                    yield f"data: {json.dumps(res)}\n\n"
                    return

            except Exception as e:
                log.error(f"Error: {e}")
                yield f"data: {json.dumps({'error': {'detail':str(e)}})}\n\n"
                return

            if isinstance(res, str):
                message = openai_chat_chunk_message_template(form_data["model"], res)
                yield f"data: {json.dumps(message)}\n\n"

            if isinstance(res, Iterator):
                for line in res:
                    yield process_line(form_data, line)

            if isinstance(res, AsyncGenerator):
                async for line in res:
                    yield process_line(form_data, line)

            if isinstance(res, str) or isinstance(res, Generator):
                finish_message = openai_chat_chunk_message_template(
                    form_data["model"], ""
                )
                finish_message["choices"][0]["finish_reason"] = "stop"
                yield f"data: {json.dumps(finish_message)}\n\n"
                yield "data: [DONE]"

        return StreamingResponse(stream_content(), media_type="text/event-stream")
    else:
        try:
            res = await execute_pipe(pipe, params)

        except Exception as e:
            log.error(f"Error: {e}")
            return {"error": {"detail": str(e)}}

        if isinstance(res, StreamingResponse) or isinstance(res, dict):
            return res
        if isinstance(res, BaseModel):
            return res.model_dump()

        message = await get_message_content(res)
        return openai_chat_completion_message_template(form_data["model"], message)



# --- NEW AGENTIC LOGIC APPENDED AT THE END ---

from open_webui.models.users import UserModel
from open_webui.constants import ERROR_MESSAGES
from open_webui.models.chats import Chat, Chats
from open_webui.models.tools import Tools
from open_webui.retrieval.utils import get_sources_from_files
from open_webui.routers.ollama import send_post_request, get_ollama_url, get_api_key
import os

DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"

async def _call_internal_llm(
    request: Request,
    model_id: str,
    messages: list[dict],
    user: UserModel,
    stream: bool = False,
    temperature: float = 0.0,
) -> str:
    """
    Helper function to make an internal LLM call for summarization or planning.
    """
    payload = {
        "model": model_id,
        "messages": messages,
        "stream": stream,
        "options": {"temperature": temperature},
    }

    try:
        url, url_idx = await get_ollama_url(request, model_id)
        api_config = request.app.state.config.OLLAMA_API_CONFIGS.get(
            str(url_idx),
            request.app.state.config.OLLAMA_API_CONFIGS.get(url, {})
        )
        key = get_api_key(url_idx, url, request.app.state.config.OLLAMA_API_CONFIGS)

        response = await send_post_request(
            url=f"{url}/v1/chat/completions",
            payload=json.dumps(payload),
            stream=stream,
            key=key,
            user=user,
        )

        if stream:
            full_content = ""
            async for chunk in response.body_iterator:
                try:
                    chunk_data = json.loads(chunk.decode("utf-8").replace("data: ", ""))
                    if "choices" in chunk_data and len(chunk_data["choices"]) > 0:
                        delta = chunk_data["choices"][0]["delta"]
                        if "content" in delta:
                            full_content += delta["content"]
                except json.JSONDecodeError:
                    log.warning(f"Failed to decode JSON chunk: {chunk}")
            return full_content
        else:
            if "choices" in response and len(response["choices"]) > 0:
                return response["choices"][0]["message"]["content"]
            else:
                log.error(f"Unexpected LLM response format: {response}")
                return ""
    except HTTPException as e:
        log.error(f"Internal LLM call failed: {e.detail}")
        raise
    except Exception as e:
        log.error(f"An unexpected error occurred during internal LLM call: {e}")
        raise HTTPException(
            status_code=500, detail=ERROR_MESSAGES.DEFAULT_ERROR
        )


async def generate_agentic_chat_completion(
    request, form_data, user, models: dict = {}
):
    if DEBUG_MODE:
        log.info(f"=== [MCP Tool 開發用] - [進入 async def generate_agentic_chat_completion 函式] form_data : {form_data}")

    model_id = form_data.get("model")
    model_info = Models.get_model_by_id(model_id)
    metadata = form_data.pop("metadata", {})
    chat_id = metadata.get("chat_id")

    # 移除 execution_summary 相關的記憶管理功能
    # 直接使用當前對話訊息作為工作記憶
    working_memory = f"當前使用者問題：\n{json.dumps(form_data['messages'], indent=2, ensure_ascii=False)}"

    # 2. Planner Stage
    user_system_prompt = getattr(model_info.params, "system", "")

    PLANNER_PROMPT_TEMPLATE = """
        # Role: Expert Planning Assistant

        ## Core Principles to Follow
        You must adhere to the following core principles provided by the user during your planning process:
        ---
        {{user_system_prompt}}
        ---

        ## Your Task: Create a JSON Execution Plan
        You are an expert planning assistant. Your sole responsibility is to analyze a conversation and create a
        structured, step-by-step execution plan in JSON format to answer the user's latest query. You do not
        answer the user's question directly. You only generate the plan.

        ### 1. Available Resources
        You have access to two types of resources: **Knowledge Bases (for information retrieval)** and **Tools (for function execution)**.

        #### 1.A. Knowledge Bases (RAG)
        Use these to answer questions about general policies, regulations, procedures, or other descriptive information.
        {{knowledge_bases}}

        #### 1.B. Tools (MCP)
        Use these to perform actions, such as querying databases for specific, precise user data.
        {{mcp_tools}}

        ### 2. Instructions
        1.  **Analyze the History**: Carefully review the entire conversation history provided in `{{conversation_history}}`.
        2.  **Identify the Core Task**: Identify the user's core task in their most recent message.
        3.  **Decompose the Task**: If the task is complex, break it down into smaller, logical sub-questions.
        4.  **Assign Resources**: For each sub_question, decide whether it requires information retrieval from a **Knowledge Base** or function execution with a **Tool**.
        5.  **Construct the Plan**: Build the final plan in the JSON format specified below.

        ### 3. Conversation History
        {{conversation_history}}

        ### 4. Output Format
        You **MUST** respond with **ONLY** a valid JSON object that follows this exact structure. Do not add any text or explanation before or after the JSON object.
        {
          "plan": [
            {
              "step": 1,
              "sub_question": "The first sub-question to be answered.",
              "resource_type": "The type of resource needed ('knowledge_bases' or 'mcp_tools').",
              "resource_name": "The exact name of the resource to use for this step."
            }
          ]
        }
        """

    # On-demand loading of tool servers if not already in state
    if not request.app.state.TOOL_SERVERS:
        log.info("Tool servers not found in state, loading them on-demand for agentic chat...")
        # Passing session_token if available for tools that require authentication
        session_token = request.state.token.credentials if hasattr(request.state, "token") else None
        request.app.state.TOOL_SERVERS = await get_tool_servers_data(
            request.app.state.config.TOOL_SERVER_CONNECTIONS,
            session_token
        )

    # We need to pass extra_params to get_tools, let's build it
    __event_emitter__ = get_event_emitter(metadata)
    __event_call__ = get_event_call(metadata)

    extra_params = {
        "__event_emitter__": __event_emitter__,
        "__event_call__": __event_call__,
        "__chat_id__": metadata.get("chat_id", None),
        "__session_id__": metadata.get("session_id", None),
        "__message_id__": metadata.get("message_id", None),
        "__user__": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "role": user.role,
        },
        "__metadata__": metadata,
        "__request__": request,
        "__model__": models.get(form_data["model"], None),
        "__messages__": form_data["messages"],
        "__files__": metadata.get("files", []),
    }

    raw_knowledge_bases = getattr(model_info.meta, "knowledge", []) if model_info.meta else []
    knowledge_bases_list = [
        {"name": kb.get("name"), "description": kb.get("description")}
        for kb in raw_knowledge_bases
    ]

    mcp_tools_list = []
    if model_info.meta and hasattr(model_info.meta, "toolIds"):
        tool_ids = model_info.meta.toolIds
        if tool_ids:
            all_tools_dict = get_tools(request, tool_ids, user, extra_params)

            # Enrich the spec with high-level tool info for the planner
            for function_name, tool_info in all_tools_dict.items():
                tool_id = tool_info['tool_id']
                enriched_spec = tool_info['spec'].copy()

                if tool_id.startswith("server:"):
                    try:
                        server_idx = int(tool_id.split(":")[1])
                        tool_server_data = next((s for s in request.app.state.TOOL_SERVERS if s.get("idx") == server_idx), None)
                        if tool_server_data:
                            enriched_spec['tool_source_name'] = tool_server_data.get('info', {}).get('title', 'Unknown Server')
                            enriched_spec['tool_source_description'] = tool_server_data.get('info', {}).get('description', '')
                    except (ValueError, IndexError):
                        pass # Ignore if parsing fails
                else:
                    internal_tool = Tools.get_tool_by_id(tool_id)
                    if internal_tool:
                        enriched_spec['tool_source_name'] = internal_tool.name
                        enriched_spec['tool_source_description'] = internal_tool.meta.description if internal_tool.meta else ''

                mcp_tools_list.append(enriched_spec)

    final_planner_prompt = PLANNER_PROMPT_TEMPLATE.replace(
        "{{user_system_prompt}}", user_system_prompt
    ).replace(
        "{{knowledge_bases}}", json.dumps(knowledge_bases_list, indent=2, ensure_ascii=False)
    ).replace(
        "{{mcp_tools}}", json.dumps(mcp_tools_list, indent=2, ensure_ascii=False)
    ).replace(
        "{{conversation_history}}", working_memory
    )

    planner_payload_messages = [
        {"role": "system", "content": final_planner_prompt},
        {"role": "user", "content": "Please generate the execution plan in JSON format now based on the provided context."}
    ]

    log.info("PIPELINE: Calling internal LLM for plan generation...")
    planner_response_content = await _call_internal_llm(
        request=request, model_id=model_info.base_model_id, messages=planner_payload_messages, user=user, temperature=0.0
    )

    try:
        # Clean the LLM response to extract only the JSON object
        json_start = planner_response_content.find("{")
        json_end = planner_response_content.rfind("}") + 1

        if json_start == -1 or json_end == 0:
            raise ValueError("No JSON object found in the LLM's response.")

        clean_json_string = planner_response_content[json_start:json_end]
        execution_plan = json.loads(clean_json_string)

        if not isinstance(execution_plan, dict) or "plan" not in execution_plan or not isinstance(execution_plan.get("plan"), list):
            raise ValueError("Invalid execution plan format: 'plan' key missing or not a list.")
    except (json.JSONDecodeError, ValueError) as e:
        log.error(f"PIPELINE: Failed to generate or parse valid execution plan. Error: {e}")
        log.error(f"PIPELINE: Raw planner LLM response: {planner_response_content}")
        raise HTTPException(status_code=500, detail="Failed to generate a valid execution plan from the LLM.")

    # 3. Tool Execution Stage - 簡化版本，不記錄執行摘要
    tool_results = []

    for step in execution_plan.get("plan", []):
        sub_question = step.get("sub_question")
        resource_type = step.get("resource_type")
        resource_name = step.get("resource_name")

        tool_result = None

        if resource_type == "knowledge_bases":
            # RAG call logic
            tool_result = "RAG result placeholder"
        elif resource_type == "mcp_tools":
            # MCP Tool call logic
            tool_result = "MCP Tool result placeholder"

        tool_results.append({
            "step": step.get("step"),
            "resource_name": resource_name,
            "result": tool_result
        })

    # 4. Synthesizer Stage - 直接使用工具結果，不更新記憶
    synthesizer_messages = [{"role": "system", "content": user_system_prompt}] + form_data["messages"]
    for result in tool_results:
        synthesizer_messages.append({"role": "tool", "content": f"工具 {result['resource_name']} 執行結果：{result['result']}"})

    if DEBUG_MODE:
        log.info("PIPELINE: Calling internal LLM for final synthesis...")

    final_response_content = await _call_internal_llm(
        request=request, model_id=model_info.base_model_id, messages=synthesizer_messages, user=user, stream=form_data.get("stream", False), temperature=0.7
    )

    if form_data.get("stream", False):
        async def stream_final_response():
            yield openai_chat_chunk_message_template(form_data["model"], final_response_content)
            finish_message = openai_chat_chunk_message_template(form_data["model"], "")
            finish_message["choices"][0]["finish_reason"] = "stop"
            yield f"data: {json.dumps(finish_message)}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(stream_final_response(), media_type="text/event-stream")
    else:
        return openai_chat_completion_message_template(form_data["model"], final_response_content)
