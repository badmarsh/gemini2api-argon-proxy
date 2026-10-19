"""OpenAI Responses API（POST /responses）。挂载见 app/main.py（/v1、/openai/v1 前缀）。
Gemini 路径复用 account_pool + 现有工具模拟机制；第三方模型见 responses_thirdparty.py。"""
import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.config import settings
from app.core.account_pool import account_pool as gemini_client
from app.core.gemini_client import (
    GEMINI_MODELS, _resolve_model, GOOGLE_WEB_ERROR_PHRASES, match_google_web_error_prefix,
    MODEL_ALIASES, classify_error, ProtocolDriftError, GoogleWebError,
)
from app.core.fallback import is_empty_result, is_blank_answer, EMPTY_RESPONSE_MESSAGE
from app.core import agent_loop, argon_fallback
from app.core.regeneration_budget import RegenerationBudget
from app.core.responses_protocol import (
    parse_responses_input, build_responses_object, new_response_id, ResponsesStreamEncoder,
)
from app.core.stream import (
    InitialPrefixBuffer, iter_with_keepalive, SSE_KEEPALIVE_FRAME, sse_keepalive_during,
    split_into_chunks,
)
from app.routers.openai import _images_to_markdown
from app.utils.tools import build_tool_prompt, parse_tool_response_with_retry, estimate_tokens
from app.utils.prompt import build_prompt_from_messages, extract_attachments

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Responses"])


def _error(status: int, message: str, error_type: str = "invalid_request_error",
          param: str | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={
        "error": {"message": message, "type": error_type, "param": param, "code": None},
    })


def _safe_response_stream_error(exc: Exception) -> str:
    """Keep a Google web refusal out of the streamed error envelope as well as the text delta."""
    if not isinstance(exc, ProtocolDriftError) and (
            isinstance(exc, GoogleWebError) or "google web error" in str(exc).lower()
            or match_google_web_error_prefix(str(exc))):
        return "Google web soft-error detected; regeneration/fallback failed"
    return str(exc)


def _request_params(body: dict) -> dict:
    params = {}
    for key in ("tools", "tool_choice", "temperature", "top_p", "max_output_tokens",
               "store", "truncation", "parallel_tool_calls"):
        if key in body and body[key] is not None:
            params[key] = body[key]
    return params


def _normalize_tool_choice_for_prompt(tool_choice):
    """Responses 的 tool_choice 指定工具用扁平形状 {"type":"function","name":"x"}；
    build_tool_prompt 期望嵌套形状 {"type":"function","function":{"name":"x"}}。"""
    if isinstance(tool_choice, dict) and "function" not in tool_choice and "name" in tool_choice:
        return {"type": tool_choice.get("type", "function"),
               "function": {"name": tool_choice["name"]}}
    return tool_choice


async def _build_output_items(text: str, has_tools: bool, regenerate=None, budget=None,
                              tool_choice=None) -> tuple[list[dict], str]:
    """把模型原始回复文本组装成 Responses output 数组。返回 (output_items, 剩余文本估算用)。

    ``regenerate`` 为可选的「重新取一次模型文本」闭包：工具调用 JSON 畸形时用它重试
    一次（仅一次，绝不修补截断的 JSON），见 app/utils/tools.parse_tool_response_with_retry。
    """
    output = []
    if has_tools:
        parsed = await parse_tool_response_with_retry(
            text, regenerate, budget=budget, tool_choice=tool_choice)
        if parsed["type"] == "tool_calls":
            for tc in parsed["tool_calls"]:
                output.append({
                    "id": f"fc_{uuid.uuid4().hex}", "type": "function_call", "status": "completed",
                    "call_id": f"call_{uuid.uuid4().hex[:24]}", "name": tc["name"],
                    "arguments": json.dumps(tc.get("arguments", {}), ensure_ascii=False),
                })
            return output, ""
        text = parsed.get("content", text)
    output.append({
        "id": f"msg_{uuid.uuid4().hex}", "type": "message", "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    })
    return output, text


@router.post("/responses")
async def create_response(request: Request):
    body = await request.json()
    model = body.get("model", "")
    input_data = body.get("input")
    instructions = body.get("instructions")
    stream = bool(body.get("stream", False))
    tools_raw = body.get("tools") or []
    tool_choice = _normalize_tool_choice_for_prompt(body.get("tool_choice"))
    previous_response_id = body.get("previous_response_id")

    _reasoning = body.get("reasoning") or {}
    _eff = ""
    if isinstance(_reasoning, dict):
        _eff = str(_reasoning.get("effort") or "").strip()
    if not _eff:
        _eff = str(body.get("reasoning_effort") or "").strip()
    is_thinking_model = any(k in model.lower() for k in ("thinking", "high", "argon"))
    if _eff.lower() == "none":
        extended_thinking = False
    else:
        extended_thinking = (bool(_eff) or is_thinking_model) and settings.extended_thinking_enabled

    if previous_response_id:
        return _error(400, "previous_response_id is not supported by this server "
                          "(no server-side conversation state); resend full history in 'input'.",
                     param="previous_response_id")

    if input_data is None or (isinstance(input_data, (list, str)) and len(input_data) == 0):
        return _error(400, "input must contain at least one message", param="input")

    messages_raw = parse_responses_input(input_data, instructions)
    if not any(m.get("role") == "user" for m in messages_raw):
        return _error(400, "input must contain at least one user message", param="input")

    model_mapping = request.app.state.model_mapping
    resolved_model = model_mapping.resolve(model)
    resolved_model = MODEL_ALIASES.get(resolved_model, resolved_model)
    gem_mapping = getattr(request.app.state, "gem_mapping", None)
    gem_id = None
    gem_account_id = None
    if gem_mapping:
        gem_info = gem_mapping.resolve(resolved_model)
        if gem_info:
            gem_id = gem_info.get("gem_id")
            gem_account_id = gem_info.get("account_id") or None
            resolved_model = gem_info.get("base_model") or "gemini-pro"

    resolved_model, is_argon, force_thinking, argon_served_by = argon_fallback.plan(model, resolved_model)
    if force_thinking:
        extended_thinking = True
    _pre_headers = argon_fallback.plan_headers(argon_served_by)

    request_params = _request_params(body)

    if resolved_model not in gemini_client.models and _resolve_model(resolved_model) not in GEMINI_MODELS:
        from app.core.responses_thirdparty import dispatch_thirdparty_responses
        result = await dispatch_thirdparty_responses(
            request, resolved_model, messages_raw, tools_raw, tool_choice, stream, request_params,
        )
        if result is not None:
            return result
        return _error(400, f"Model '{model}' is not a Gemini model and has no third-party "
                          f"entry configured", param="model")

    prompt = build_prompt_from_messages(messages_raw)
    has_tools = bool(tools_raw)
    if has_tools:
        prompt = build_tool_prompt(prompt, tools_raw, tool_choice)
    attachments = extract_attachments(messages_raw)

    if stream:
        return StreamingResponse(
            _stream_gemini_response(request, prompt, resolved_model, has_tools, attachments,
                                    gem_id, gem_account_id, request_params, extended_thinking,
                                    is_argon=is_argon, tool_choice=tool_choice),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        # Argon -> Flash rules live in app/core/argon_fallback.py (one implementation, marked substitutions).
        result = await argon_fallback.generate(
            gemini_client, prompt, resolved_model, attachments,
            is_argon=is_argon, thinking=extended_thinking, gem_id=gem_id, account_id=gem_account_id)
    except Exception as e:
        if extended_thinking:
            logger.warning(f"extended-thinking request failed, retrying normal: {e}")
            try:
                result = await gemini_client.generate(prompt, resolved_model, "", attachments,
                                                      gem_id=gem_id, account_id=gem_account_id,
                                                      extended_thinking=False)
            except Exception as e2:
                return _error(502, _safe_response_stream_error(e2), error_type="api_error")
        else:
            return _error(502, _safe_response_stream_error(e), error_type="api_error")

    text = result.get("text", "")
    res_text = (text or "").strip()
    is_err_text = bool(match_google_web_error_prefix(res_text))
    # Every fallback option is exhausted (argon_fallback above, or ARGON_FALLBACK_TO_FLASH=false): answer with a
    # retryable error instead of an HTTP 200 with empty content.
    if is_blank_answer(result):
        return _error(502, EMPTY_RESPONSE_MESSAGE, error_type="api_error")
    # Google's soft-error phrase ("Sorry, something went wrong...") is a failure, not an assistant reply. Fallback
    # (above) already had its chance; report it the way chat/completions does so a client can retry.
    if is_err_text:
        status, err_type, retry_after = classify_error(GoogleWebError(
            "Google web soft-error detected; regeneration/fallback failed"))
        resp = _error(status, "Google web soft-error detected; regeneration/fallback failed", error_type=err_type)
        if retry_after:
            resp.headers["Retry-After"] = str(retry_after)
        return resp
    # AI 生成图片：沿用 chat/completions 的既有做法，Markdown 图片链接嵌入文字内容（design §4.3）
    gen_images = result.get("images") or []
    if gen_images:
        md = _images_to_markdown(gen_images, request)
        text = (md + "\n" + text.strip()) if text.strip() else md
    _served = (result.get("argon_fallback") or {}).get("served_by") if isinstance(result, dict) else None
    _retry_model = settings.argon_fallback_model if _served else resolved_model
    _retry_is_argon = is_argon and not _served

    async def _regenerate_for_tools(nudge: str = "") -> str:
        r = await argon_fallback.generate(gemini_client, prompt + nudge, _retry_model, attachments,
                                          is_argon=_retry_is_argon, thinking=extended_thinking,
                                          gem_id=gem_id, account_id=gem_account_id)
        return r.get("text", "")

    regeneration_budget = RegenerationBudget() if has_tools else None
    if has_tools:
        # Agent-loop guard: a promise-only reply would end the harness' turn; corrected once.
        text, _stall_corrected = await agent_loop.guard_tool_loop(
            text, _regenerate_for_tools, enabled=getattr(settings, "tool_loop_stall_guard", True),
            budget=regeneration_budget)
    output, _ = await _build_output_items(
        text, has_tools, _regenerate_for_tools, regeneration_budget, tool_choice=tool_choice)
    thoughts = result.get("thoughts") or ""
    if thoughts:
        output = [{"type": "reasoning", "id": new_response_id().replace("resp_", "rs_"),
                  "summary": [{"type": "summary_text", "text": thoughts}]}] + output
    usage = {"input_tokens": estimate_tokens(prompt), "output_tokens": estimate_tokens(text)}
    obj = build_responses_object(model=resolved_model, status="completed", output=output,
                                 request_params=request_params, usage=usage,
                                 instructions=instructions)
    return JSONResponse(content=obj)


async def _stream_gemini_response(request, prompt, model, has_tools, attachments, gem_id,
                                  gem_account_id, request_params, extended_thinking=False,
                                  is_argon=False, tool_choice=None):
    response_id = new_response_id()
    enc = ResponsesStreamEncoder(response_id, model, request_params)
    yield enc.created()
    yield enc.in_progress()

    if has_tools or attachments:
        # 工具调用/附件：需要完整文本才能判断，走非流式收集（同 openai.py 的 buffered 门禁）。
        # Codex CLI 恒带 tools，这条分支必走——generate() 阻塞期间必须有心跳，否则首字节超时。
        gen_task = asyncio.create_task(gemini_client.generate(prompt, model, "", attachments,
                                                              gem_id=gem_id, account_id=gem_account_id,
                                                              extended_thinking=extended_thinking))
        try:
            async for ping in sse_keepalive_during(gen_task):
                yield ping
        except BaseException:          # GeneratorExit / CancelledError on client disconnect
            gen_task.cancel()
            if gen_task.done() and not gen_task.cancelled():
                gen_task.exception()   # 取回异常，避免 asyncio 在 GC 时打印 "Task exception was never retrieved"
            raise
        try:
            result = gen_task.result()
        except Exception as e:
            if (isinstance(e, ProtocolDriftError) or "protocol drift" in str(e).lower()) and request is not None:
                request.state.debug_error_class = "protocol_drift"
            is_google_soft_error = (
                not isinstance(e, ProtocolDriftError)
                and (isinstance(e, GoogleWebError) or "google web error" in str(e).lower())
            )
            if is_google_soft_error:
                if request is not None:
                    request.state.debug_error_class = "google_web_soft_error"
                logger.warning("Buffered Google web soft-error detected; attempting regeneration")
                retry_task = asyncio.create_task(argon_fallback.generate(
                    gemini_client, prompt, model, attachments,
                    is_argon=is_argon, thinking=extended_thinking,
                    gem_id=gem_id, account_id=gem_account_id))
                try:
                    try:
                        async for ping in sse_keepalive_during(retry_task):
                            yield ping
                    except BaseException:
                        retry_task.cancel()
                        if retry_task.done() and not retry_task.cancelled():
                            retry_task.exception()
                        raise
                    result = retry_task.result()
                except Exception as e_soft:
                    yield enc.failed(_safe_response_stream_error(e_soft))
                    return
            elif is_argon and getattr(settings, "argon_fallback_to_flash", True):
                logger.warning(f"Argon stream(buffered) failed ({e}), falling back to {settings.argon_fallback_model}")
                fb_task = asyncio.create_task(gemini_client.generate(
                    prompt, settings.argon_fallback_model, "", attachments,
                    gem_id=gem_id, account_id=gem_account_id, extended_thinking=True))
                try:
                    async for ping in sse_keepalive_during(fb_task):
                        yield ping
                except BaseException:
                    fb_task.cancel()
                    if fb_task.done() and not fb_task.cancelled():
                        fb_task.exception()
                    raise
                try:
                    result = fb_task.result()
                except Exception as e_fb:
                    yield enc.failed(_safe_response_stream_error(e_fb))
                    return
            elif extended_thinking:
                logger.warning(f"extended-thinking stream(buffered) failed, retrying normal: {e}")
                try:
                    result = await gemini_client.generate(prompt, model, "", attachments,
                                                          gem_id=gem_id, account_id=gem_account_id,
                                                          extended_thinking=False)
                except Exception as e2:
                    yield enc.failed(_safe_response_stream_error(e2))
                    return
            else:
                if request is not None and not getattr(request.state, "debug_error_class", ""):
                    request.state.debug_error_class = "upstream_stream_drop"
                yield enc.failed(_safe_response_stream_error(e))
                return
        text = result.get("text", "")
        res_text = (text or "").strip()
        is_err_text = bool(match_google_web_error_prefix(res_text))
        if is_err_text:
            if request is not None:
                request.state.debug_error_class = "google_web_soft_error"
            logger.warning("Buffered Google web soft-error suppressed before Responses SSE emission; regenerating")
            retry_task = asyncio.create_task(argon_fallback.generate(
                gemini_client, prompt, model, attachments,
                is_argon=is_argon, thinking=extended_thinking,
                gem_id=gem_id, account_id=gem_account_id))
            try:
                try:
                    async for ping in sse_keepalive_during(retry_task):
                        yield ping
                except BaseException:
                    retry_task.cancel()
                    if retry_task.done() and not retry_task.cancelled():
                        retry_task.exception()
                    raise
                retry_result = retry_task.result()
                retry_text = (retry_result.get("text") or "").strip()
                if not match_google_web_error_prefix(retry_text) and not is_blank_answer(retry_result):
                    result = retry_result
                    text = result.get("text", "") or ""
                    is_err_text = False
            except Exception as e_soft:
                logger.warning("Buffered Responses soft-error regeneration failed: %s", type(e_soft).__name__)
        if is_blank_answer(result):
            yield enc.failed(EMPTY_RESPONSE_MESSAGE)
            return
        if is_err_text:
            yield enc.failed("Google web soft-error detected; regeneration/fallback failed")
            return
        thoughts = result.get("thoughts") or ""
        gen_images = result.get("images") or []
        if gen_images:
            md = _images_to_markdown(gen_images, request)
            text = (md + "\n" + text.strip()) if text.strip() else md
        # 此处只发过 created/in_progress/keepalive，还没发任何 output 块，重试是安全的。
        _served = (result.get("argon_fallback") or {}).get("served_by") if isinstance(result, dict) else None
        _retry_model = settings.argon_fallback_model if _served else model
        _retry_is_argon = is_argon and not _served

        async def _regenerate_for_tools(nudge: str = "") -> str:
            r = await argon_fallback.generate(gemini_client, prompt + nudge, _retry_model, attachments,
                                              is_argon=_retry_is_argon, thinking=extended_thinking or is_argon,
                                              gem_id=gem_id, account_id=gem_account_id)
            return r.get("text", "")

        if has_tools:
            regeneration_budget = RegenerationBudget()
            # Agent-loop guard: a promise-only reply would end the harness' turn; corrected once.
            guard_task = asyncio.create_task(agent_loop.guard_tool_loop(
                text, _regenerate_for_tools, enabled=getattr(settings, "tool_loop_stall_guard", True),
                budget=regeneration_budget))
            try:
                async for ping in sse_keepalive_during(guard_task):
                    yield ping
            except BaseException:
                guard_task.cancel()
                if guard_task.done() and not guard_task.cancelled():
                    guard_task.exception()
                raise
            text, _stall_corrected = guard_task.result()

        # 畸形工具 JSON 的重试要再跑一整轮上游 generate()——这条流早就开了（created/
        # in_progress/keepalive 已发），裸 await 会在已开的连接上制造一次远超单个 keepalive
        # 间隔的死寂（issue #10 followup F2）。用与上面 gen_task 相同的 task + keepalive 续心跳。
        build_task = asyncio.create_task(_build_output_items(
            text, has_tools, _regenerate_for_tools, regeneration_budget if has_tools else None,
            tool_choice=tool_choice))
        try:
            async for ping in sse_keepalive_during(build_task):
                yield ping
        except BaseException:          # GeneratorExit / CancelledError on client disconnect
            build_task.cancel()
            if build_task.done() and not build_task.cancelled():
                build_task.exception()   # 取回异常，避免 asyncio 在 GC 时打印 "Task exception was never retrieved"
            raise
        output, _ = build_task.result()
        for idx, item in enumerate(output):
            if item["type"] == "function_call":
                for frame in enc.function_call(item["id"], idx, item["call_id"],
                                               item["name"], item["arguments"]):
                    yield frame
            else:
                msg_id = item["id"]
                for frame in enc.text_message_start(msg_id, idx):
                    yield frame
                full_text = item["content"][0]["text"]
                if full_text:
                    async for chunk in split_into_chunks(full_text, delay=0.005):
                        yield enc.text_delta(msg_id, idx, chunk)
                for frame in enc.text_message_end(msg_id, idx, full_text):
                    yield frame
        usage = {"input_tokens": estimate_tokens(prompt), "output_tokens": estimate_tokens(text)}
        completed_output = output
        if thoughts:
            completed_output = [{"type": "reasoning", "id": new_response_id().replace("resp_", "rs_"),
                                 "summary": [{"type": "summary_text", "text": thoughts}]}] + output
        yield enc.completed(completed_output, usage)
        return

    # 真流式路径：无工具/附件，逐块转发
    msg_id = f"msg_{uuid.uuid4().hex}"
    for frame in enc.text_message_start(msg_id, 0):
        yield frame
    full_text = ""
    final_images = []
    thoughts = ""
    streamed_any = False
    error_prefix_buffer = InitialPrefixBuffer(GOOGLE_WEB_ERROR_PHRASES)
    soft_error_prefix = None
    source_complete = False
    stream_events = iter_with_keepalive(
        gemini_client.generate_stream(prompt, model, "", attachments,
                                      gem_id=gem_id, account_id=gem_account_id,
                                      extended_thinking=extended_thinking))
    try:
        async for _kind, evt in stream_events:
            if _kind == "ping":
                yield SSE_KEEPALIVE_FRAME
                continue
            if evt.get("type") == "thoughts":
                t_delta = evt.get("text", "")
                if t_delta:
                    thoughts += t_delta
            elif evt.get("type") == "delta":
                delta = evt.get("text", "")
                if delta:
                    full_text += delta
                    safe_delta, matched = error_prefix_buffer.feed(delta)
                    if matched:
                        soft_error_prefix = matched
                        logger.warning("Google web soft-error prefix suppressed before Responses SSE emission: %r", matched)
                        break
                    if safe_delta:
                        streamed_any = True
                        yield enc.text_delta(msg_id, 0, safe_delta)
            elif evt.get("type") == "final":
                final_text = evt.get("text", full_text)
                matched = error_prefix_buffer.match(final_text)
                if matched:
                    soft_error_prefix = matched
                    full_text = final_text
                    logger.warning("Google web soft-error prefix suppressed before Responses SSE emission: %r", matched)
                    break
                if final_text.startswith(full_text) and len(final_text) > len(full_text):
                    tail = final_text[len(full_text):]
                    safe_tail, matched = error_prefix_buffer.feed(tail)
                    full_text = final_text
                    if matched:
                        soft_error_prefix = matched
                        logger.warning("Google web soft-error prefix suppressed before Responses SSE emission: %r", matched)
                        break
                    if safe_tail:
                        streamed_any = True
                        yield enc.text_delta(msg_id, 0, safe_tail)
                else:
                    full_text = final_text
                final_images = evt.get("images") or []
                thoughts = evt.get("thoughts") or thoughts
        source_complete = True
        # An Argon stream that ends with no answer at all (empty reply, sign-in page, malformed or truncated
        # frames) raises nothing, so it used to skip the Flash fallback and finish as an empty completion.
        if (is_argon and getattr(settings, "argon_fallback_to_flash", True)
                and not streamed_any and not (full_text or "").strip() and not final_images):
            raise RuntimeError("Argon stream ended without any answer")
    except Exception as e:
        is_google_soft_error = isinstance(e, GoogleWebError) or "google web error" in str(e).lower()
        if is_google_soft_error:
            soft_error_prefix = match_google_web_error_prefix(full_text) or "upstream Google web error"
            if request is not None:
                request.state.debug_error_class = "google_web_soft_error"
            logger.warning("Google web soft-error detected before Responses SSE emission; attempting regeneration")
        else:
            if (isinstance(e, ProtocolDriftError) or "protocol drift" in str(e).lower()) and request is not None:
                request.state.debug_error_class = "protocol_drift"
            if is_argon and getattr(settings, "argon_fallback_to_flash", True) and not streamed_any:
                logger.warning(f"Argon stream failed before first chunk ({e}), falling back to {settings.argon_fallback_model}")
                error_prefix_buffer = InitialPrefixBuffer(GOOGLE_WEB_ERROR_PHRASES)
                fallback_events = iter_with_keepalive(
                    gemini_client.generate_stream(prompt, settings.argon_fallback_model, "", attachments,
                                                  gem_id=gem_id, account_id=gem_account_id,
                                                  extended_thinking=True))
                try:
                    async for _kind, evt in fallback_events:
                        if _kind == "ping":
                            yield SSE_KEEPALIVE_FRAME
                            continue
                        if evt.get("type") == "thoughts":
                            t_delta = evt.get("text", "")
                            if t_delta:
                                thoughts += t_delta
                        elif evt.get("type") == "delta":
                            delta = evt.get("text", "")
                            if delta:
                                full_text += delta
                                safe_delta, matched = error_prefix_buffer.feed(delta)
                                if matched:
                                    soft_error_prefix = matched
                                    logger.warning("Google web soft-error prefix suppressed in Flash fallback stream: %r", matched)
                                    break
                                if safe_delta:
                                    streamed_any = True
                                    yield enc.text_delta(msg_id, 0, safe_delta)
                        elif evt.get("type") == "final":
                            final_text = evt.get("text", full_text)
                            matched = error_prefix_buffer.match(final_text)
                            if matched:
                                soft_error_prefix = matched
                                full_text = final_text
                                break
                            if final_text.startswith(full_text) and len(final_text) > len(full_text):
                                tail = final_text[len(full_text):]
                                safe_tail, matched = error_prefix_buffer.feed(tail)
                                full_text = final_text
                                if matched:
                                    soft_error_prefix = matched
                                    break
                                if safe_tail:
                                    streamed_any = True
                                    yield enc.text_delta(msg_id, 0, safe_tail)
                            else:
                                full_text = final_text
                            final_images = evt.get("images") or []
                            thoughts = evt.get("thoughts") or thoughts
                    source_complete = True
                except Exception as e_fb:
                    if isinstance(e_fb, GoogleWebError) or "google web error" in str(e_fb).lower():
                        soft_error_prefix = match_google_web_error_prefix(full_text) or "upstream Google web error"
                        if request is not None:
                            request.state.debug_error_class = "google_web_soft_error"
                    else:
                        yield enc.failed(_safe_response_stream_error(e_fb))
                        return
                finally:
                    await fallback_events.aclose()
            # Extended-thinking failure: retry non-thinking once, but gate its text before emitting it.
            elif extended_thinking and not streamed_any:
                logger.warning(f"extended-thinking stream failed, retrying normal: {e}")
                error_prefix_buffer = InitialPrefixBuffer(GOOGLE_WEB_ERROR_PHRASES)
                try:
                    result = await gemini_client.generate(prompt, model, "", attachments,
                                                          gem_id=gem_id, account_id=gem_account_id,
                                                          extended_thinking=False)
                except Exception as e2:
                    if isinstance(e2, GoogleWebError) or "google web error" in str(e2).lower():
                        soft_error_prefix = "upstream Google web error"
                        if request is not None:
                            request.state.debug_error_class = "google_web_soft_error"
                    else:
                        yield enc.failed(_safe_response_stream_error(e2))
                        return
                else:
                    full_text = result.get("text", "") or ""
                    final_images = result.get("images") or []
                    thoughts = result.get("thoughts") or thoughts
                    matched = match_google_web_error_prefix(full_text)
                    if matched:
                        soft_error_prefix = matched
                        if request is not None:
                            request.state.debug_error_class = "google_web_soft_error"
                    elif full_text:
                        yield enc.text_delta(msg_id, 0, full_text)
                        streamed_any = True
                    source_complete = True
            else:
                if request is not None and not getattr(request.state, "debug_error_class", ""):
                    request.state.debug_error_class = "upstream_stream_drop"
                yield enc.failed(_safe_response_stream_error(e))
                return
    finally:
        await stream_events.aclose()

    if source_complete and soft_error_prefix is None:
        safe_tail, matched = error_prefix_buffer.finish()
        if matched:
            soft_error_prefix = matched
            if request is not None:
                request.state.debug_error_class = "google_web_soft_error"
            logger.warning("Google web soft-error prefix suppressed at Responses stream end: %r", matched)
        elif safe_tail:
            streamed_any = True
            yield enc.text_delta(msg_id, 0, safe_tail)

    if soft_error_prefix:
        if request is not None:
            request.state.debug_error_class = "google_web_soft_error"
        try:
            retry_task = asyncio.create_task(argon_fallback.generate(
                gemini_client, prompt, model, attachments,
                is_argon=is_argon, thinking=extended_thinking,
                gem_id=gem_id, account_id=gem_account_id))
            try:
                async for ping in sse_keepalive_during(retry_task):
                    yield ping
            except BaseException:
                retry_task.cancel()
                if retry_task.done() and not retry_task.cancelled():
                    retry_task.exception()
                raise
            result = retry_task.result()
            retry_text = result.get("text", "") or ""
            if match_google_web_error_prefix(retry_text):
                raise GoogleWebError("Google web soft-error prefix persisted after regeneration")
            if is_blank_answer(result):
                raise GoogleWebError("Google web soft-error regeneration returned an empty response")
            marker = result.get("argon_fallback") if isinstance(result, dict) else None
            if marker:
                yield f": gemini2api argon_fallback served_by={marker.get('served_by')} reason={marker.get('reason')}\n\n"
            full_text = retry_text
            final_images = result.get("images") or []
            thoughts = result.get("thoughts") or thoughts
            if full_text:
                streamed_any = True
                yield enc.text_delta(msg_id, 0, full_text)
        except Exception as e_soft:
            logger.warning("Google web soft-error regeneration failed: %s", type(e_soft).__name__)
            yield enc.failed("Google web soft-error detected; regeneration/fallback failed")
            return

    if not streamed_any and not (full_text or "").strip() and not final_images and not thoughts:
        yield enc.failed(EMPTY_RESPONSE_MESSAGE)
        return

    # Defense in depth for a final snapshot that diverges from incremental deltas: never complete a Responses
    # stream with a recognized Google soft-error, even if an upstream implementation bypassed the prefix gate.
    _final_stripped = (full_text or "").strip()
    if match_google_web_error_prefix(_final_stripped):
        if request is not None:
            request.state.debug_error_class = "google_web_soft_error"
        yield enc.failed("Google web soft-error detected; regeneration/fallback failed")
        return

    if final_images:
        md = _images_to_markdown(final_images, request)
        full_text = (md + "\n" + full_text.strip()) if full_text.strip() else md

    for frame in enc.text_message_end(msg_id, 0, full_text):
        yield frame
    output = [{"id": msg_id, "type": "message", "role": "assistant", "status": "completed",
              "content": [{"type": "output_text", "text": full_text, "annotations": []}]}]
    if thoughts:
        output = [{"type": "reasoning", "id": new_response_id().replace("resp_", "rs_"),
                  "summary": [{"type": "summary_text", "text": thoughts}]}] + output
    usage = {"input_tokens": estimate_tokens(prompt), "output_tokens": estimate_tokens(full_text)}
    yield enc.completed(output, usage)
