import json
import time
import uuid
import asyncio
import logging
from typing import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.config import settings
from app.core.account_pool import account_pool as gemini_client
from app.core.api_forwarder import forward_to_provider, open_stream
from app.core.fallback import (
    fallback_enabled, is_empty_result, get_fallback_entries, openai_data_is_empty, is_blank_answer,
    EMPTY_RESPONSE_MESSAGE,
)
from app.core import argon_breaker, argon_fallback, agent_loop
from app.core.regeneration_budget import RegenerationBudget
from app.core.conversation_store import conversation_store
from app.core.gemini_client import (
    GEMINI_MODELS, MODEL_ALIASES, _resolve_model, HTTPStatusError, UPSTREAM_ERRORS, classify_error,
    GoogleWebError, ProtocolDriftError, GOOGLE_WEB_ERROR_PHRASES, match_google_web_error_prefix,
)
from app.core.stream import (
    InitialPrefixBuffer, split_into_chunks, format_sse, iter_with_keepalive,
    SSE_KEEPALIVE_INTERVAL, SSE_KEEPALIVE_FRAME, sse_keepalive_during,
)
from app.models.openai import (
    ChatRequest, ChatResponse, Choice, ChoiceMessage,
    StreamChunk, StreamChoice, StreamDelta,
    ModelList, ModelInfo, UsageInfo,
    ImageGenerationRequest, ImageData, ImageResponse,
)
from app.utils.tools import build_tool_prompt, parse_tool_response_with_retry, estimate_tokens, is_image_generation_intent, maybe_image_generation_intent
from app.utils.prompt import apply_argon_system, build_prompt_from_messages, extract_attachments, last_user_text
from app.core.limiter import limiter, dynamic_rate_limit, rate_limit_exempt

logger = logging.getLogger(__name__)
router = APIRouter(tags=["OpenAI"])


def _apply_model_whitelist(models: list[str]) -> list[str]:
    """按 MODEL_WHITELIST（逗号分隔）过滤模型列表；为空表示不过滤（放行全部）。
    让文档化的 MODEL_WHITELIST 真正生效——只暴露白名单内的模型。"""
    raw = (settings.model_whitelist or "").strip()
    if not raw:
        return models
    allowed = {m.strip() for m in raw.split(",") if m.strip()}
    if not allowed:
        return models
    return [m for m in models if m in allowed]


def _model_info_card(model_name: str, created: int, gem_mapping=None) -> ModelInfo:
    """Add modality hints to Gemini model cards without guessing for third-party models."""
    resolved_name = model_name
    if gem_mapping:
        gem_info = gem_mapping.resolve(model_name)
        if gem_info:
            resolved_name = gem_info.get("base_model") or "gemini-pro"
    capabilities = GEMINI_MODELS.get(_resolve_model(resolved_name))
    if not capabilities or not capabilities.get("supports_vision"):
        return ModelInfo(id=model_name, created=created)
    return ModelInfo(
        id=model_name,
        created=created,
        vision=bool(capabilities.get("vision")),
        supports_vision=bool(capabilities.get("supports_vision")),
        multimodal=bool(capabilities.get("multimodal")),
        input_modalities=list(capabilities.get("input_modalities", [])),
        output_modalities=list(capabilities.get("output_modalities", [])),
        capabilities=list(capabilities.get("capabilities", [])),
    )


def _is_google_soft_error(exc: Exception) -> bool:
    if isinstance(exc, ProtocolDriftError):
        return False
    return (isinstance(exc, GoogleWebError) or "google web error" in str(exc).lower()
            or bool(match_google_web_error_prefix(str(exc))))


def _safe_google_error_message(exc: Exception) -> str:
    if _is_google_soft_error(exc):
        return "Google web soft-error detected; regeneration/fallback failed"
    return str(exc)


def _image_base(request) -> str:
    """从请求推断中转站对外地址（不写死，适配任意部署）。"""
    try:
        base = str(request.base_url).rstrip("/")
        return base
    except Exception:
        return ""


def _images_md_from_base(images: list, base: str) -> str:
    """生成图片转 markdown。优先用本地托管 URL（客户端可渲染），无 id 降级 data URI。"""
    lines = []
    for im in images:
        if im.get("id") and base:
            lines.append(f"![generated image]({base}/images/{im['id']})")
        elif im.get("b64"):
            lines.append(f"![generated image](data:{im['mime']};base64,{im['b64']})")
        elif im.get("fallback"):
            lines.append("*(Generated image could not be downloaded in time; please retry.)*")
    return "\n".join(lines)


_SSE_KEEPALIVE_INTERVAL = SSE_KEEPALIVE_INTERVAL  # 单一来源见 app/core/stream.py


_sse_keepalive_during = sse_keepalive_during  # 实现下沉至 app/core/stream.py（供 claude 侧复用）


async def _sse_stream_chunks(text: str, completion_id: str, model_name: str, *, fast: bool = False):
    """伪流式切片输出，切片间隔期间也发 keepalive ping。"""
    chunk_delay = 0.0 if fast else 0.03
    async for kind, payload in iter_with_keepalive(
        split_into_chunks(text, delay=chunk_delay), interval=_SSE_KEEPALIVE_INTERVAL
    ):
        if kind == "ping":
            yield ": ping\n\n"
        elif kind == "evt":
            chunk = StreamChunk(
                id=completion_id, model=model_name,
                choices=[StreamChoice(delta=StreamDelta(content=payload))],
            )
            yield format_sse(chunk.model_dump())


def _images_to_markdown(images: list, request) -> str:
    """生成图片转 markdown。优先用本地托管 URL（客户端可渲染），无 id 降级 data URI。"""
    return _images_md_from_base(images, _image_base(request))


def _json_body(resp) -> dict | None:
    """把 forward_to_provider 的 JSONResponse body 解析成 dict（失败返回 None）。"""
    try:
        body = getattr(resp, "body", None)
        if body is None:
            return None
        if isinstance(body, (bytes, bytearray)):
            body = body.decode("utf-8", "replace")
        return json.loads(body)
    except Exception:
        return None


async def _fallback_result(request, req: ChatRequest, messages_raw: list, exclude_model: str = "") -> dict | None:
    """Gemini 失败/空响应时的统一兜底：在候选第三方里按序（自动随机/或指定顺序）逐个
    用【非流式】探测，跳过报错或返回空的候选，直到拿到一个“有内容/有工具调用”的好结果。

    返回 OpenAI 风格响应 dict；未开启 / 无可用候选 / 全部失败时返回 None（调用方维持原有错误返回，零回归）。
    用非流式探测的好处：能在“真正发给客户端之前”验证候选是否可用，从而真正做到“一个不行换一个”，
    既不会把报错也不会把空响应当成功（流式直接转发无法回退，故统一走探测）。
    """
    if request is None or req is None or not fallback_enabled():
        return None
    pool = getattr(request.app.state, "api_key_pool", None)
    # 强制非流式探测（不改原 req 的 stream 语义）
    probe_req = req.model_copy(update={"stream": False})
    for entry in get_fallback_entries(request.app.state, exclude_model=exclude_model):
        try:
            resp = await forward_to_provider(entry, messages_raw, probe_req)
        except Exception as e:  # noqa: BLE001 — 单个候选失败不中断，换下一个
            logger.warning(f"[fallback] 第三方 {entry.model} 失败: {e}")
            continue
        if isinstance(resp, JSONResponse) and getattr(resp, "status_code", 200) >= 400:
            logger.warning(f"[fallback] 第三方 {entry.model} 返回 {getattr(resp, 'status_code', '?')}，换下一个")
            continue
        data = _json_body(resp)
        if data is None or openai_data_is_empty(data):
            logger.warning(f"[fallback] 第三方 {entry.model} 返回空/不可解析，换下一个")
            continue
        if pool:
            pool.update_last_used(entry.id)
        logger.info(f"[fallback] 已由第三方模型 {entry.model} 兜底")
        return data
    return None


async def _emit_openai_data_sse(data: dict, completion_id: str, model_name: str) -> AsyncGenerator[str, None]:
    """把第三方非流式 OpenAI 响应 dict 转成 SSE 帧（role 首帧已由调用方发出）。
    含工具调用则发原生 tool_calls 帧；否则把文本切片伪流式输出。"""
    message = {}
    try:
        message = (data.get("choices") or [{}])[0].get("message") or {}
    except Exception:
        message = {}
    tool_calls = message.get("tool_calls")
    if tool_calls:
        for tc in tool_calls:
            clean_tc = {
                "id": tc.get("id"),
                "type": tc.get("type", "function"),
                "function": tc.get("function", {}),
            }
            chunk = StreamChunk(
                id=completion_id, model=model_name,
                choices=[StreamChoice(delta=StreamDelta(tool_calls=[clean_tc]))],
            )
            yield format_sse(chunk.model_dump())
        final = StreamChunk(
            id=completion_id, model=model_name,
            choices=[StreamChoice(delta=StreamDelta(), finish_reason="tool_calls")],
        )
        yield format_sse(final.model_dump())
        yield "data: [DONE]\n\n"
        return
    text = message.get("content") or ""
    async for sse in _sse_stream_chunks(text, completion_id, model_name, fast=True):
        yield sse
    done = StreamChunk(
        id=completion_id, model=model_name,
        choices=[StreamChoice(delta=StreamDelta(), finish_reason="stop")],
    )
    yield format_sse(done.model_dump())
    yield "data: [DONE]\n\n"


async def _maybe_fallback_stream(request, req: ChatRequest, messages_raw: list, exclude_model: str, completion_id: str, model_name: str) -> AsyncGenerator[str, None]:
    """流式路径的兜底：探到好结果就转成 SSE 发出；没有则不产出任何 chunk。"""
    data = await _fallback_result(request, req, messages_raw, exclude_model)
    if data is None:
        return
    async for sse in _emit_openai_data_sse(data, completion_id, model_name):
        yield sse


def _thirdparty_ok(resp) -> bool:
    """非流式第三方 JSONResponse 成功判定：状态<400 且 body 非空（有 content 或 tool_calls）。"""
    if not isinstance(resp, JSONResponse):
        return True
    if getattr(resp, "status_code", 200) >= 400:
        return False
    return not openai_data_is_empty(_json_body(resp))


async def _dispatch_thirdparty(request, req: ChatRequest, resolved_model: str):
    """直连第三方故障切换：固定优先第一家，失败即冷却换下一家，全败返回最后错误。
    返回 None 表示该 model 非第三方/无候选 —— 落回 Gemini 路径，零回归。"""
    pool = getattr(request.app.state, "api_key_pool", None)
    if not pool:
        return None
    candidates = pool.get_entries_for_model(resolved_model)
    if not candidates:
        return None
    messages_raw = [m.model_dump() for m in req.messages]
    cooldown = settings.thirdparty_failover_cooldown
    last_error = None
    for entry in candidates:
        if req.stream:
            stream_resp, err = await open_stream(entry, messages_raw, req)
            if stream_resp is not None:
                pool.update_last_used(entry.id)
                return stream_resp
            last_error = err
        else:
            resp = await forward_to_provider(entry, messages_raw, req)
            if _thirdparty_ok(resp):
                pool.update_last_used(entry.id)
                return resp
            last_error = resp
        pool.mark_unhealthy(entry.id, cooldown)
    return last_error


@router.get("/models", response_model=ModelList, response_model_exclude_none=True)
async def list_models(request: Request):
    models = list(gemini_client.models)
    # Also include models from API key pool
    pool = getattr(request.app.state, 'api_key_pool', None)
    if pool:
        for entry in pool.entries.values():
            if entry.status == 'active' and entry.model not in models:
                models.append(entry.model)
    # 把已暴露的 gem 模型名并进列表
    gem_mapping = getattr(request.app.state, "gem_mapping", None)
    if gem_mapping:
        for gem_model_name in gem_mapping.get_all().keys():
            if gem_model_name not in models:
                models.append(gem_model_name)
    # MODEL_WHITELIST 过滤（为空则放行全部）
    models = _apply_model_whitelist(models)
    now = int(time.time())
    data = [_model_info_card(m, now, gem_mapping) for m in models]
    return ModelList(data=data)


@router.post("/chat/completions")
@limiter.limit(dynamic_rate_limit, exempt_when=rate_limit_exempt)
async def chat_completions(req: ChatRequest, request: Request):
    model_mapping = request.app.state.model_mapping
    resolved_model = model_mapping.resolve(req.model)
    resolved_model = MODEL_ALIASES.get(resolved_model, resolved_model)

    # gem 模型解析：命中则取出 gem_id/account_id，并把对话模型换成 base_model
    gem_mapping = getattr(request.app.state, "gem_mapping", None)
    gem_id = None
    gem_account_id = None
    if gem_mapping:
        gem_info = gem_mapping.resolve(resolved_model)
        if gem_info:
            gem_id = gem_info.get("gem_id")
            gem_account_id = gem_info.get("account_id") or None
            resolved_model = gem_info.get("base_model") or "gemini-pro"

    if resolved_model not in gemini_client.models and _resolve_model(resolved_model) not in GEMINI_MODELS:
        tp = await _dispatch_thirdparty(request, req, resolved_model)
        if tp is not None:
            return tp

    messages_raw = [m.model_dump() for m in req.messages]

    # 对话上下文持久化：检查是否有 conversation_id
    gemini_conv_id = ""
    conv = None
    if req.conversation_id:
        conv = await conversation_store.get(req.conversation_id)
        if conv and conv.gemini_conv_id:
            gemini_conv_id = conv.gemini_conv_id

    # ARGON_SYSTEM_PROMPT: injected on a *new* Argon conversation. Coding harnesses always send their own
    # developer prompt, so the default mode *merges* the Argon rules in front of it instead of skipping them
    # (see ARGON_SYSTEM_PROMPT_MODE). Must happen before the prompt is flattened and before build_tool_prompt
    # wraps it, otherwise the tool prompt is lost. A continuing conversation (gemini_conv_id) sends only the
    # last user message: the upstream chat already holds the system prompt from its first turn.
    _sys_text = None
    if not gemini_conv_id and argon_fallback.is_argon_model(req.model):
        messages_raw, _sys_text = apply_argon_system(
            messages_raw, getattr(settings, "argon_system_prompt", ""),
            getattr(settings, "argon_system_prompt_mode", "merge"))

    # If the server were able to continue the stored conversation, only the newest user message would need to
    # be sent. It is not: the 81-element StreamGenerate payload has no slot for a conversation id (spec §4.3),
    # so stripping the prompt used to lose the entire context silently — the client's `conversation_id` was
    # "accepted" and the model answered without history. Default is therefore the full history
    # (SERVER_SIDE_CONTINUATION=false); set it true only once the payload can carry the id again.
    if gemini_conv_id and messages_raw and getattr(settings, "server_side_continuation", False):
        last_user_msg = ""
        for msg in reversed(messages_raw):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = "\n".join(b.get("text", "") if isinstance(b.get("text", ""), str) else ""
                                        for b in content if isinstance(b, dict))
                last_user_msg = content
                break
        prompt = last_user_msg if last_user_msg else build_prompt_from_messages(messages_raw, system=_sys_text)
    else:
        gemini_conv_id = ""
        prompt = build_prompt_from_messages(messages_raw, system=_sys_text)

    has_tools = bool(req.tools)
    # 生图意图优先：即使带 tools（agent 每请求都带），只要是明确生图意图就跳过工具模拟，
    # 直接走生图，否则工具 prompt 会压制 Gemini 的图片生成能力。
    # 只看最后一轮用户消息：prompt 可能是整段历史（含 system 与 tool_result 正文），
    # 拿它判断会误判成生图并静默丢掉客户端 tools。
    if has_tools and is_image_generation_intent(last_user_text(messages_raw)):
        has_tools = False
        logger.info("检测到生图意图，跳过工具调用模拟，直接生图")
    if has_tools:
        tools_raw = [t.model_dump() for t in req.tools]
        prompt = build_tool_prompt(prompt, tools_raw, req.tool_choice)

    # 提取图片/文件附件（多模态），纯文本时为空列表
    attachments = extract_attachments(messages_raw)

    # reasoning_effort（非空）+ 全局开关 → 走扩展思维链路径
    _eff = (req.reasoning_effort or "").strip()
    is_thinking_model = any(k in req.model.lower() for k in ("thinking", "high", "argon"))
    if _eff.lower() == "none":
        extended_thinking = False
    else:
        extended_thinking = (bool(_eff) or is_thinking_model) and settings.extended_thinking_enabled

    # Argon plan: a substitution demanded up front (ARGON_ALWAYS_FALLBACK / open stall breaker) is reported
    # by name in the response headers instead of being silent.
    resolved_model, is_argon, force_thinking, argon_served_by = argon_fallback.plan(req.model, resolved_model)
    if force_thinking:
        extended_thinking = True
    _pre_headers = argon_fallback.plan_headers(argon_served_by)

    if req.stream:
        return StreamingResponse(
            _stream_response(prompt, resolved_model, has_tools, gemini_conv_id, conv, messages_raw, req.model, attachments, _image_base(request), request, req, gem_id, gem_account_id, extended_thinking=extended_thinking, is_argon=is_argon, system_text=_sys_text),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     **argon_fallback.stream_headers(is_argon), **_pre_headers},
        )

    try:
        # The Argon -> Flash rules live in exactly one place (app/core/argon_fallback.py): error, timeout,
        # empty reply and Google-web-error replies all degrade to the fallback model, and the substitution is
        # reported back through result["argon_fallback"] -> response headers.
        result = await argon_fallback.generate(
            gemini_client, prompt, resolved_model, attachments,
            is_argon=is_argon, thinking=extended_thinking, conversation_id=gemini_conv_id,
            gem_id=gem_id, account_id=gem_account_id)
    except UPSTREAM_ERRORS as e:
        err = e
        result = None
        # 扩展思维链路径失败：先原样退回非思维链重试一次，再走既有兜底（会话过期/第三方）
        if extended_thinking:
            logger.warning(f"extended-thinking request failed, retrying normal: {e}")
            try:
                result = await gemini_client.generate(prompt, resolved_model, gemini_conv_id, attachments,
                                                      gem_id=gem_id, account_id=gem_account_id,
                                                      extended_thinking=False)
            except UPSTREAM_ERRORS as e2:
                err = e2

        if result is None:
            # Fallback: 如果 conversation_id 过期，用完整 prompt 重试
            if gemini_conv_id:
                prompt = build_prompt_from_messages(messages_raw, system=_sys_text)
                if has_tools:
                    tools_raw = [t.model_dump() for t in req.tools]
                    prompt = build_tool_prompt(prompt, tools_raw, req.tool_choice)
                try:
                    result = await gemini_client.generate(prompt, resolved_model,
                                                           gem_id=gem_id, account_id=gem_account_id,
                                                           extended_thinking=False)
                    gemini_conv_id = ""
                except Exception:
                    fb = await _fallback_result(request, req, messages_raw, resolved_model)
                    if fb is not None:
                        return JSONResponse(content=fb)
                    status, err_type, retry_after = classify_error(err)
                    headers = {"Retry-After": str(retry_after)} if retry_after else None
                    return JSONResponse(
                        status_code=status,
                        content={"error": {"message": _safe_google_error_message(err), "type": err_type}},
                        headers=headers,
                    )
            else:
                fb = await _fallback_result(request, req, messages_raw, resolved_model)
                if fb is not None:
                    return JSONResponse(content=fb)
                status, err_type, retry_after = classify_error(err)
                headers = {"Retry-After": str(retry_after)} if retry_after else None
                return JSONResponse(
                    status_code=status,
                    content={"error": {"message": _safe_google_error_message(err), "type": err_type}},
                    headers=headers,
                )

    # Gemini 返回空响应（无文本、无图）或 Google web 错误 → 第三方兜底（Argon 的 Flash 兜底已在上面做过）
    res_text = (result.get("text") or "").strip() if result else ""
    is_err_text = bool(match_google_web_error_prefix(res_text))
    if is_empty_result(result) or is_err_text:
        fb = await _fallback_result(request, req, messages_raw, resolved_model)
        if fb is not None:
            return JSONResponse(content=fb)
        if is_err_text:
            error_message = "Google web soft-error detected; regeneration/fallback failed"
            status, err_type, retry_after = classify_error(GoogleWebError(error_message))
            headers = {"Retry-After": str(retry_after)} if retry_after else None
            return JSONResponse(
                status_code=status,
                content={"error": {"message": error_message, "type": err_type}},
                headers=headers,
            )
        if is_blank_answer(result):
            status, err_type, retry_after = classify_error(GoogleWebError(EMPTY_RESPONSE_MESSAGE))
            headers = {"Retry-After": str(retry_after)} if retry_after else None
            return JSONResponse(
                status_code=status,
                content={"error": {"message": EMPTY_RESPONSE_MESSAGE, "type": err_type}},
                headers=headers,
            )

    text = result.get("text", "")
    # AI 生成图片：图片在前，紧跟文字描述（单换行，不留多余空行）
    gen_images = result.get("images") or []
    if gen_images:
        md = _images_to_markdown(gen_images, request)
        text = (md + "\n" + text.strip()) if text.strip() else md
    new_conv_id = result.get("conversation_id", "")
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"

    # 持久化对话
    if new_conv_id:
        if not conv:
            conv_store_id = req.conversation_id or completion_id
            conv = await conversation_store.create(conv_store_id, resolved_model)
        conv.gemini_conv_id = new_conv_id
        last_user = messages_raw[-1].get("content", "") if messages_raw else ""
        if isinstance(last_user, list):
            last_user = str(last_user)
        conv.add_message("user", last_user)
        conv.add_message("assistant", text)
        await conversation_store.update(conv)

    if has_tools:
        regeneration_budget = RegenerationBudget()

        async def _regenerate_for_tools(nudge: str = "") -> str:
            r = await gemini_client.generate(prompt + nudge, resolved_model, gemini_conv_id, attachments,
                                             gem_id=gem_id, account_id=gem_account_id,
                                             extended_thinking=extended_thinking)
            return r.get("text", "")

        # Agent-loop guard: a reply that only promises to inspect/edit files ends the harness' turn, so the
        # model is asked once more with the correction appended (app/core/agent_loop.py).
        text, _stall_corrected = await agent_loop.guard_tool_loop(
            text, _regenerate_for_tools, enabled=getattr(settings, "tool_loop_stall_guard", True),
            budget=regeneration_budget)
        parsed = await parse_tool_response_with_retry(
            text, _regenerate_for_tools, budget=regeneration_budget, tool_choice=req.tool_choice)
        if parsed["type"] == "tool_calls":
            tool_calls = []
            for i, tc in enumerate(parsed["tool_calls"]):
                call_id = f"call_{uuid.uuid4().hex[:8]}"
                tool_calls.append({
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc.get("arguments", {})),
                    },
                })
            _hdr = {**_pre_headers, **argon_fallback.result_headers(result)}
            _resp = ChatResponse(
                id=completion_id,
                model=req.model,
                choices=[Choice(
                    message=ChoiceMessage(role="assistant", tool_calls=tool_calls),
                    finish_reason="tool_calls",
                )],
                usage=UsageInfo(
                    prompt_tokens=estimate_tokens(prompt),
                    completion_tokens=estimate_tokens(text),
                    total_tokens=estimate_tokens(prompt) + estimate_tokens(text),
                ),
                conversation_id=conv.id if conv else None,
            )
            if _hdr:
                return JSONResponse(content=_resp.model_dump(), headers=_hdr)
            return _resp
        text = parsed.get("content", text)

    _resp = ChatResponse(
        id=completion_id,
        model=req.model,
        choices=[Choice(
            message=ChoiceMessage(role="assistant", content=text,
                                  reasoning_content=(result.get("thoughts") or None)),
            finish_reason="stop",
        )],
        usage=UsageInfo(
            prompt_tokens=estimate_tokens(prompt),
            completion_tokens=estimate_tokens(text),
            total_tokens=estimate_tokens(prompt) + estimate_tokens(text),
        ),
        conversation_id=conv.id if conv else None,
    )
    _hdr = {**_pre_headers, **argon_fallback.result_headers(result)}
    if _hdr:
        return JSONResponse(content=_resp.model_dump(), headers=_hdr)
    return _resp


async def _stream_response(prompt: str, model: str, has_tools: bool, gemini_conv_id: str = "", conv=None, messages_raw=None, display_model: str = "", attachments=None, base_url: str = "", request=None, req=None, gem_id=None, account_id=None, extended_thinking: bool = False, is_argon: bool = False, system_text: str | None = None) -> AsyncGenerator[str, None]:
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    model_name = display_model or model

    # 有工具调用或附件：需要完整文本才能解析 tool_calls / 处理上传，走非流式收集后再切片（零回归）。
    # 生图意图（宽松判断兜底）：图片在生成的最后才拿到，真流式会"文字先流、图最后补"导致割裂；
    #          走 buffered 收集完整结果，才能让图片排在文字前面。宽松判断宁可多走 buffered 不漏网。
    if has_tools or attachments or maybe_image_generation_intent(prompt):
        async for sse in _stream_response_buffered(
            prompt, model, has_tools, gemini_conv_id, conv, messages_raw,
            model_name, completion_id, attachments, base_url, request, req, gem_id, account_id,
            extended_thinking=extended_thinking, is_argon=is_argon, system_text=system_text,
        ):
            yield sse
        return

    # === 真流式路径 ===
    first = StreamChunk(
        id=completion_id, model=model_name,
        choices=[StreamChoice(delta=StreamDelta(role="assistant"))],
    )
    yield format_sse(first.model_dump())

    full_text = ""
    new_conv_id = ""
    final_images = []
    streamed_any = False
    emitted_thoughts = ""  # 已发出的思维链前缀，用于跟已知累积 thoughts 做前缀 diff 只发增量
    error_prefix_buffer = InitialPrefixBuffer(GOOGLE_WEB_ERROR_PHRASES)
    soft_error_prefix = None
    stream_exception = None
    stream_events = iter_with_keepalive(
        argon_fallback.generate_stream(gemini_client, prompt, model, attachments,
                                       is_argon=is_argon, thinking=extended_thinking,
                                       conversation_id=gemini_conv_id,
                                       gem_id=gem_id, account_id=account_id))
    try:
        # One implementation of the Argon -> Flash rules for every protocol (app/core/argon_fallback.py):
        # the helper re-raises as soon as content has gone out, retries Argon once on a dead-but-silent
        # stream, and emits a `serve_marker` event when it hands the request to Flash -- so a substitution
        # is never invisible in the stream.
        async for _kind, evt in stream_events:
            if _kind == "ping":
                yield SSE_KEEPALIVE_FRAME
                continue
            if evt.get("type") == "serve_marker":
                logger.warning(f"Argon -> {evt.get('served_by')} ({evt.get('reason')}): "
                               f"stream served by the fallback model")
                yield argon_fallback.marker_frame(evt)
                continue
            if evt.get("type") == "delta":
                # Buffer the initial prefix so a Google Workspace refusal is never sent as assistant text.
                delta = evt.get("text", "")
                full_text += delta
                safe_delta, matched = error_prefix_buffer.feed(delta)
                if matched:
                    soft_error_prefix = matched
                    logger.warning("Google web soft-error prefix suppressed before SSE emission: %r", matched)
                    break
                if safe_delta:
                    streamed_any = True
                    chunk = StreamChunk(
                        id=completion_id, model=model_name,
                        choices=[StreamChoice(delta=StreamDelta(content=safe_delta))],
                    )
                    yield format_sse(chunk.model_dump())
            elif evt.get("type") == "thoughts":
                t_delta = evt.get("text", "")
                if t_delta:
                    emitted_thoughts += t_delta
                    chunk = StreamChunk(
                        id=completion_id, model=model_name,
                        choices=[StreamChoice(delta=StreamDelta(reasoning_content=t_delta))],
                    )
                    yield format_sse(chunk.model_dump())
            elif evt.get("type") == "final":
                # 思维链目前只在 final 帧一次性拿到完整累积 thoughts（底层暂不逐帧增量），
                # 这里仍按「已发出前缀 diff」的方式只发新增部分，为未来逐帧增量留好口子；
                # 在文本尾部补发之前先发，保证客户端先看到 reasoning 再看到最终回答。
                final_thoughts = evt.get("thoughts", "") or ""
                if final_thoughts != emitted_thoughts and final_thoughts.startswith(emitted_thoughts):
                    thoughts_delta = final_thoughts[len(emitted_thoughts):]
                    emitted_thoughts = final_thoughts
                    if thoughts_delta:
                        chunk = StreamChunk(
                            id=completion_id, model=model_name,
                            choices=[StreamChoice(delta=StreamDelta(reasoning_content=thoughts_delta))],
                        )
                        yield format_sse(chunk.model_dump())
                # else: divergent/shrunk final thoughts — reasoning already streamed; do NOT re-emit whole (avoid duplicate)
                # final.text 是过滤完占位串的完整文本，可能比已流出的 full_text 多出
                # （流式时被 hold 住的尾部）。补发缺失尾部，保证客户端拿到完整内容。
                final_text = evt.get("text", full_text)
                matched = error_prefix_buffer.match(final_text)
                if matched:
                    soft_error_prefix = matched
                    full_text = final_text
                    logger.warning("Google web soft-error prefix suppressed before SSE emission: %r", matched)
                    break
                if final_text.startswith(full_text) and len(final_text) > len(full_text):
                    tail = final_text[len(full_text):]
                    safe_tail, matched = error_prefix_buffer.feed(tail)
                    full_text = final_text
                    if matched:
                        soft_error_prefix = matched
                        logger.warning("Google web soft-error prefix suppressed before SSE emission: %r", matched)
                        break
                    if safe_tail:
                        streamed_any = True
                        chunk = StreamChunk(
                            id=completion_id, model=model_name,
                            choices=[StreamChoice(delta=StreamDelta(content=safe_tail))],
                        )
                        yield format_sse(chunk.model_dump())
                else:
                    full_text = final_text
                new_conv_id = evt.get("conversation_id", "")
                final_images = evt.get("images") or []
    except Exception as e:
        stream_exception = e
        if isinstance(e, ProtocolDriftError) or "protocol drift" in str(e).lower():
            request.state.debug_error_class = "protocol_drift"
        elif _is_google_soft_error(e) and request is not None:
            request.state.debug_error_class = "google_web_soft_error"
        # Reached only when the Argon->Flash helper also gave up (or Argon content had already streamed):
        # the fallback itself is fully handled inside app/core/argon_fallback.py.
        # 流式失败：会话ID 过期等场景，用完整 prompt 非流式重试一次
        if gemini_conv_id and messages_raw and not streamed_any:
            try:
                retry_prompt = build_prompt_from_messages(messages_raw, system=system_text)
                result = await gemini_client.generate(retry_prompt, model, "", attachments,
                                                      gem_id=gem_id, account_id=account_id,
                                                      extended_thinking=False)
                full_text = result.get("text", "")
                new_conv_id = result.get("conversation_id", "")
                final_images = result.get("images") or []
            except Exception as e2:
                # 尚未流出任何内容（只发过 role 首帧）→ 可安全改用第三方兜底（流式）
                if not streamed_any:
                    emitted = False
                    async for chunk in _maybe_fallback_stream(request, req, messages_raw, model, completion_id, model_name):
                        emitted = True
                        yield chunk
                    if emitted:
                        return
                yield _err_chunk(e2)
                return
        else:
            if not streamed_any:
                emitted = False
                async for chunk in _maybe_fallback_stream(request, req, messages_raw, model, completion_id, model_name):
                    emitted = True
                    yield chunk
                if emitted:
                    return
            yield _err_chunk(e)
            return
    finally:
        await stream_events.aclose()

    # A short non-error prefix may remain buffered when the upstream stream ends; it is safe to flush now.
    if soft_error_prefix is None and stream_exception is None:
        safe_tail, matched = error_prefix_buffer.finish()
        if matched:
            soft_error_prefix = matched
            if request is not None:
                request.state.debug_error_class = "google_web_soft_error"
            logger.warning("Google web soft-error prefix suppressed at stream end: %r", matched)
        if safe_tail:
            streamed_any = True
            chunk = StreamChunk(
                id=completion_id, model=model_name,
                choices=[StreamChoice(delta=StreamDelta(content=safe_tail))],
            )
            yield format_sse(chunk.model_dump())

    if soft_error_prefix:
        if request is not None:
            request.state.debug_error_class = "google_web_soft_error"
        # Mark this generation attempt as soft-failed and regenerate before considering an SSE error frame.
        try:
            retry_task = asyncio.create_task(argon_fallback.generate(
                gemini_client, prompt, model, attachments,
                is_argon=is_argon, thinking=extended_thinking,
                conversation_id=gemini_conv_id, gem_id=gem_id, account_id=account_id))
            try:
                async for ping in _sse_keepalive_during(retry_task):
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
                yield argon_fallback.marker_frame({
                    "served_by": marker.get("served_by"), "reason": marker.get("reason")
                })
            full_text = retry_text
            new_conv_id = result.get("conversation_id", "")
            final_images = result.get("images") or []
            recovered_thoughts = result.get("thoughts") or ""
            if recovered_thoughts and not emitted_thoughts:
                thought_chunk = StreamChunk(
                    id=completion_id, model=model_name,
                    choices=[StreamChoice(delta=StreamDelta(reasoning_content=recovered_thoughts))],
                )
                yield format_sse(thought_chunk.model_dump())
            if full_text:
                text_chunk = StreamChunk(
                    id=completion_id, model=model_name,
                    choices=[StreamChoice(delta=StreamDelta(content=full_text))],
                )
                yield format_sse(text_chunk.model_dump())
                streamed_any = True
        except Exception as e_soft:
            logger.warning("Google web soft-error regeneration failed: %s", type(e_soft).__name__)
            emitted = False
            async for chunk in _maybe_fallback_stream(request, req, messages_raw, model, completion_id, model_name):
                emitted = True
                yield chunk
            if emitted:
                return
            yield _err_chunk(GoogleWebError(
                "Google web soft-error detected; regeneration and fallback were unavailable"))
            return


    # Gemini 真流式整条为空或 Google web 错误 → 第三方兜底
    stripped_full = (full_text or "").strip()
    is_err_text = bool(match_google_web_error_prefix(stripped_full))
    if is_err_text or (not streamed_any and not stripped_full and not final_images):
        emitted = False
        async for chunk in _maybe_fallback_stream(request, req, messages_raw, model, completion_id, model_name):
            emitted = True
            yield chunk
        if emitted:
            return
        if is_err_text:
            yield _err_chunk(RuntimeError(stripped_full))
            return
        # Nothing streamed, nothing to fall back to: say so instead of finishing with an empty "stop".
        yield _err_chunk(GoogleWebError(EMPTY_RESPONSE_MESSAGE))
        return

    # 生图兜底：极少数生图意图未被识别而走了真流式（文字已先流出，无法把图收回到最前）。
    # 此时图片在最后帧拿到，补发图片增量；用单换行紧凑拼接，保证图能独立成行正常显示。
    if final_images:
        md = _images_md_from_base(final_images, base_url)
        tail = ("\n" + md) if full_text.strip() else md
        full_text += tail
        chunk = StreamChunk(
            id=completion_id, model=model_name,
            choices=[StreamChoice(delta=StreamDelta(content=tail))],
        )
        yield format_sse(chunk.model_dump())

    # 持久化对话（与 buffered 子路径一致：流式只存 assistant，
    # 多轮续接靠 gemini_conv_id，user 消息已在请求里）
    if new_conv_id and conv:
        conv.gemini_conv_id = new_conv_id
        conv.add_message("assistant", full_text)
        await conversation_store.update(conv)

    done_chunk = StreamChunk(
        id=completion_id, model=model_name,
        choices=[StreamChoice(delta=StreamDelta(), finish_reason="stop")],
    )
    yield format_sse(done_chunk.model_dump())
    yield "data: [DONE]\n\n"


def _err_chunk(exc: Exception) -> str:
    """真实上游失败不能伪装成正常回答（与 v1.6.35 在 Claude 侧修的 defect ② 同类，
    这里补 OpenAI 协议的残留）：此前发 content:"Error: ..." + finish_reason="stop"，
    官方 SDK 看不到异常，会把错误文本当成模型说的话，client 的重试/故障转移/退避永远
    不会触发。改发 OpenAI 风格的 error 帧（data 帧带顶层 "error" 键）——openai SDK 的
    _streaming.py 专门检测这个键并抛 APIError，这才是流式中途报错的正确表达方式。
    复用与 Claude 侧同一份 classify_error，两个协议对同一类失败给出一致的 type。
    调用方在此之前已经先尝试过 _maybe_fallback_stream（第三方兜底），本函数只在
    兜底也未命中时才会被触达；已流出的内容不受影响（不回退已发内容）。"""
    if _is_google_soft_error(exc):
        exc = GoogleWebError("Google web soft-error detected; regeneration and fallback failed")
    status, err_type, _retry_after = classify_error(exc)
    chunk = {"error": {"message": str(exc), "type": err_type, "code": status}}
    return format_sse(chunk) + "data: [DONE]\n\n"


async def _stream_response_buffered(prompt: str, model: str, has_tools: bool, gemini_conv_id: str = "", conv=None, messages_raw=None, model_name: str = "", completion_id: str = "", attachments=None, base_url: str = "", request=None, req=None, gem_id=None, account_id=None, extended_thinking: bool = False, is_argon: bool = False, system_text: str | None = None) -> AsyncGenerator[str, None]:
    """非流式收集 + 切片伪流式：用于有工具调用/附件、需要完整文本的场景。

    整条生成走 ``argon_fallback.generate``：Argon 的失败/超时/空回复 → Flash 兜底、失败兜底会重新抛出、
    替换发生时结果里带上 ``argon_fallback`` 标记（``_result_headers`` 会把它如实写回响应头）。此前这里
    是第三份手写的兜底副本，三处规则不同步、兜底失败还会静默返回空响应。
    """
    # 立即发出首帧 SSE，避免生图/工具路径在 generate() 阻塞期间零字节导致前置代理超时。
    first = StreamChunk(
        id=completion_id, model=model_name,
        choices=[StreamChoice(delta=StreamDelta(role="assistant"))],
    )
    yield format_sse(first.model_dump())

    async def _argon_generate(p: str, m: str, c_id: str, *, argon: bool | None = None):
        return await argon_fallback.generate(
            gemini_client, p, m, attachments,
            is_argon=is_argon if argon is None else argon, thinking=extended_thinking,
            conversation_id=c_id, gem_id=gem_id, account_id=account_id)

    async def _keepalive_task(task: asyncio.Task):
        """yield keepalive frames while ``task`` runs; on disconnect cancel it and re-raise."""
        try:
            async for ping in _sse_keepalive_during(task):
                yield ping
        except BaseException:          # GeneratorExit / CancelledError on client disconnect
            task.cancel()
            if task.done() and not task.cancelled():
                task.exception()       # 取回异常，避免 asyncio 在 GC 时打印 "Task exception was never retrieved"
            raise
        if task.done() and not task.cancelled():
            task.exception()           # 同上：异常由调用方的 task.result() 统一映射

    gen_task = asyncio.create_task(_argon_generate(prompt, model, gemini_conv_id))
    async for ping in _keepalive_task(gen_task):
        yield ping

    try:
        result = gen_task.result()
    except Exception as e:
        # 兜底也已失败（或 Argon 已有内容流出后失败）：会话过期场景整段重发一次
        if gemini_conv_id and messages_raw:
            full_prompt = build_prompt_from_messages(messages_raw, system=system_text)
            retry_task = asyncio.create_task(_argon_generate(full_prompt, model, ""))
            async for ping in _keepalive_task(retry_task):
                yield ping
            try:
                result = retry_task.result()
            except Exception as e2:
                emitted = False
                async for chunk in _maybe_fallback_stream(request, req, messages_raw, model, completion_id, model_name):
                    emitted = True
                    yield chunk
                if emitted:
                    return
                yield _err_chunk(e2)
                return
        else:
            emitted = False
            async for chunk in _maybe_fallback_stream(request, req, messages_raw, model, completion_id, model_name):
                emitted = True
                yield chunk
            if emitted:
                return
            yield _err_chunk(e)
            return

    # 上游返回空响应或 Google web 错误：Flash 兜底已在 _argon_generate 里做过，这里只剩第三方兜底。
    raw_res_text = (result.get("text") or "").strip() if result else ""
    is_err_text = bool(match_google_web_error_prefix(raw_res_text))
    if is_err_text:
        if request is not None:
            request.state.debug_error_class = "google_web_soft_error"
        logger.warning("Buffered Google web soft-error suppressed before SSE emission")
    if is_empty_result(result) or is_err_text:
        emitted = False
        async for chunk in _maybe_fallback_stream(request, req, messages_raw, model, completion_id, model_name):
            emitted = True
            yield chunk
        if emitted:
            return
        if is_err_text:
            yield _err_chunk(RuntimeError(raw_res_text))
            return
        if is_blank_answer(result):
            yield _err_chunk(GoogleWebError(EMPTY_RESPONSE_MESSAGE))
            return

    text = result.get("text", "")
    gen_images = result.get("images") or []
    if gen_images:
        md = _images_md_from_base(gen_images, base_url)
        text = (md + "\n" + text.strip()) if text.strip() else md
    new_conv_id = result.get("conversation_id", "")

    if new_conv_id and conv:
        conv.gemini_conv_id = new_conv_id
        conv.add_message("assistant", text)
        await conversation_store.update(conv)

    if has_tools:
        regeneration_budget = RegenerationBudget()
        # 工具重试沿用「这次到底是谁答的」：已经被 Flash 兜底过的请求不要再去吃一遍 Argon 超时。
        served = (result.get("argon_fallback") or {}).get("served_by") if isinstance(result, dict) else None
        retry_model = settings.argon_fallback_model if served else model
        retry_is_argon = is_argon and not served

        # 此刻只发过 role 首帧 + keepalive，还没发任何 content 块，重试是安全的。
        async def _regenerate_for_tools(nudge: str = "") -> str:
            r = await _argon_generate(prompt + nudge, retry_model, gemini_conv_id, argon=retry_is_argon)
            return r.get("text", "")

        # Agent-loop guard: a promise-only reply ("I'll inspect the files...") would end the harness'
        # turn, so it is corrected once before the tool call is parsed.
        guard_task = asyncio.create_task(agent_loop.guard_tool_loop(
            text, _regenerate_for_tools, enabled=getattr(settings, "tool_loop_stall_guard", True),
            budget=regeneration_budget))
        async for ping in _keepalive_task(guard_task):
            yield ping
        text, _stall_corrected = guard_task.result()

        # 畸形工具 JSON 的重试要再跑一整轮上游 generate()——这条流早就开了（role 首帧已发），
        # 裸 await 会在这段已开的连接上制造一次远超单个 keepalive 间隔的死寂（issue #10
        # followup F2）。用与上面 gen_task 相同的 task + keepalive 套路续上心跳。
        parse_task = asyncio.create_task(parse_tool_response_with_retry(
            text, _regenerate_for_tools, budget=regeneration_budget,
            tool_choice=getattr(req, "tool_choice", None)))
        async for ping in _keepalive_task(parse_task):
            yield ping
        parsed = parse_task.result()
        if parsed["type"] == "tool_calls":
            for i, tc in enumerate(parsed["tool_calls"]):
                call_id = f"call_{uuid.uuid4().hex[:8]}"
                tool_call_data = {
                    "index": i,
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc.get("arguments", {})),
                    },
                }
                chunk = StreamChunk(
                    id=completion_id, model=model_name,
                    choices=[StreamChoice(delta=StreamDelta(tool_calls=[tool_call_data]))],
                )
                yield format_sse(chunk.model_dump())

            final = StreamChunk(
                id=completion_id, model=model_name,
                choices=[StreamChoice(delta=StreamDelta(), finish_reason="tool_calls")],
            )
            yield format_sse(final.model_dump())
            yield "data: [DONE]\n\n"
            return
        text = parsed.get("content", text)

    async for sse in _sse_stream_chunks(
        text, completion_id, model_name, fast=bool(gen_images),
    ):
        yield sse

    done_chunk = StreamChunk(
        id=completion_id, model=model_name,
        choices=[StreamChoice(delta=StreamDelta(), finish_reason="stop")],
    )
    yield format_sse(done_chunk.model_dump())
    yield "data: [DONE]\n\n"


@router.post("/images/generations")
async def images_generations(req: ImageGenerationRequest):
    """OpenAI 兼容的图片生成接口。靠 prompt 触发 Gemini Web 生图，
    服务端代下载图片转 base64 返回（lh3 URL 客户端直接访问会 403）。"""
    raw_prompt = (req.prompt or "").strip()
    if not raw_prompt:
        return JSONResponse(status_code=400,
            content={"error": {"message": "prompt is required", "type": "invalid_request_error"}})

    # 确保 prompt 含生图意图，否则加前缀（复用公共意图检测）
    if not is_image_generation_intent(raw_prompt):
        prompt = f"Generate an image of {raw_prompt}"
    else:
        prompt = raw_prompt

    model = _resolve_model(req.model or "gemini-pro")
    try:
        result = await gemini_client.generate(prompt, model)
    except UPSTREAM_ERRORS as e:
        return JSONResponse(status_code=500,
            content={"error": {"message": str(e), "type": "api_error"}})

    images = result.get("images") or []
    if not images:
        return JSONResponse(status_code=502, content={"error": {
            "message": "未生成图片。可能账号无生图权限（地区/年龄限制），或 prompt 未触发生图。",
            "type": "image_generation_failed"}})

    n = max(1, min(req.n or 1, len(images)))
    fmt = (req.response_format or "b64_json").lower()
    data = []
    for img in images[:n]:
        if fmt == "url":
            # 无本地存储时降级为 data URI（保证可用）
            data.append(ImageData(url=f"data:{img['mime']};base64,{img['b64']}"))
        else:
            data.append(ImageData(b64_json=img["b64"]))
    return ImageResponse(data=data)

