import base64
import datetime
import json
import logging
import os
from typing import Any, Callable, List, Optional

import aiohttp

from khoj.database.adapters import FileObjectAdapters
from khoj.database.models import Agent, FileObject, KhojUser
from khoj.processor.conversation import prompts
from khoj.processor.conversation.utils import (
    ChatEvent,
    clean_code_python,
    clean_json,
    construct_chat_history,
)
from khoj.routers.helpers import send_message_to_model_wrapper
from khoj.utils.helpers import is_none_or_empty, timer
from khoj.utils.rawconfig import LocationData

logger = logging.getLogger(__name__)


SANDBOX_URL = os.getenv("KHOJ_TERRARIUM_URL", "http://localhost:8080")


async def run_code(
    query: str,
    conversation_history: dict,
    context: str,
    location_data: LocationData,
    user: KhojUser,
    send_status_func: Optional[Callable] = None,
    query_images: List[str] = None,
    agent: Agent = None,
    sandbox_url: str = SANDBOX_URL,
    tracer: dict = {},
):
    # Generate Code
    if send_status_func:
        async for event in send_status_func(f"**Generate code snippet** for {query}"):
            yield {ChatEvent.STATUS: event}
    try:
        with timer("Chat actor: Generate programs to execute", logger):
            code, input_files, input_links = await generate_python_code(
                query,
                conversation_history,
                context,
                location_data,
                user,
                query_images,
                agent,
                tracer,
            )
    except Exception as e:
        raise ValueError(f"Failed to generate code for {query} with error: {e}")

    # Prepare Input Data
    input_data = []
    user_input_files: List[FileObject] = []
    for input_file in input_files:
        user_input_files += await FileObjectAdapters.async_get_file_objects_by_name(user, input_file)
    for f in user_input_files:
        input_data.append(
            {
                "filename": os.path.basename(f.file_name),
                "b64_data": base64.b64encode(f.raw_text.encode("utf-8")).decode("utf-8"),
            }
        )

    # Run Code
    if send_status_func:
        async for event in send_status_func(f"**Running code snippet**"):
            yield {ChatEvent.STATUS: event}
    try:
        with timer("Chat actor: Execute generated program", logger, log_level=logging.INFO):
            result = await execute_sandboxed_python(code, input_data, sandbox_url)
            code = result.pop("code")
            logger.info(f"Executed Code:\n--@@--\n{code}\n--@@--Result:\n--@@--\n{result}\n--@@--")
            yield {query: {"code": code, "results": result}}
    except Exception as e:
        raise ValueError(f"Failed to run code for {query} with error: {e}")


async def generate_python_code(
    q: str,
    conversation_history: dict,
    context: str,
    location_data: LocationData,
    user: KhojUser,
    query_images: list[str] = None,
    agent: Agent = None,
    tracer: dict = {},
) -> tuple[str, list[str], list[str]]:
    location = f"{location_data}" if location_data else "Unknown"
    username = prompts.user_name.format(name=user.get_full_name()) if user.get_full_name() else ""
    chat_history = construct_chat_history(conversation_history)

    utc_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    personality_context = (
        prompts.personality_context.format(personality=agent.personality) if agent and agent.personality else ""
    )

    code_generation_prompt = prompts.python_code_generation_prompt.format(
        current_date=utc_date,
        query=q,
        chat_history=chat_history,
        context=context,
        location=location,
        username=username,
        personality_context=personality_context,
    )

    response = await send_message_to_model_wrapper(
        code_generation_prompt,
        query_images=query_images,
        response_type="json_object",
        user=user,
        tracer=tracer,
    )

    # Validate that the response is a non-empty, JSON-serializable list
    response = clean_json(response)
    response = json.loads(response)
    code = response.get("code", "").strip()
    input_files = response.get("input_files", [])
    input_links = response.get("input_links", [])

    if not isinstance(code, str) or is_none_or_empty(code):
        raise ValueError
    return code, input_files, input_links


async def execute_sandboxed_python(code: str, input_data: list[dict], sandbox_url: str = SANDBOX_URL) -> dict[str, Any]:
    """
    Takes code to run as a string and calls the terrarium API to execute it.
    Returns the result of the code execution as a dictionary.
    """
    headers = {"Content-Type": "application/json"}
    cleaned_code = clean_code_python(code)
    data = {"code": cleaned_code, "files": input_data}

    async with aiohttp.ClientSession() as session:
        async with session.post(sandbox_url, json=data, headers=headers) as response:
            if response.status == 200:
                result: dict[str, Any] = await response.json()
                result["code"] = cleaned_code
                return result
            else:
                return {
                    "code": cleaned_code,
                    "success": False,
                    "std_err": f"Failed to execute code with {response.status}",
                }
