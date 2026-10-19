import json
import re
import logging
from typing import MutableMapping

logger = logging.getLogger(__name__)

# Notice for malformed tool call JSON sent to clients and used by is_malformed_tool_result.
MALFORMED_TOOL_NOTICE = "(The tool call format returned by the model was invalid and has been ignored. Please retry or switch to gemini-flash.)"


def _set_parse_diagnostic(diagnostics: MutableMapping | None, **values) -> None:
    """Update optional parser diagnostics without changing the public parsed shape."""
    if diagnostics is not None:
        diagnostics.update(values)


def _json_error_details(exc: json.JSONDecodeError, text: str) -> dict:
    message = exc.msg or "JSON decode error"
    lowered = message.lower()
    if "unterminated string" in lowered:
        failure_class = "unterminated_string"
    elif "delimiter" in lowered or "property name" in lowered:
        failure_class = "delimiter"
    else:
        failure_class = "json_syntax"
    return {
        "error": message,
        "position": exc.pos,
        "failure_class": failure_class,
        "response_length": len(text),
    }


def _looks_truncated_json(text: str) -> bool:
    """Conservative diagnostic only; this function never repairs or accepts JSON."""
    if not text:
        return False
    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth < 0:
                return False
    return in_string or depth > 0


def _repair_complete_unescaped_quotes(candidate: str) -> str | None:
    """Repair only unescaped quotes inside a complete JSON object string.

    The repair is deliberately a bounded JSON-string state-machine, not a regex rescue parser. A quote is
    escaped only when doing so leads to a complete object accepted by ``json.loads(strict=False)``. The
    candidate must already have an outer closing brace; a missing brace/quote (the truncation case) is never
    synthesized. Existing valid JSON is returned untouched by the caller before this function is reached.
    """
    if not isinstance(candidate, str) or not candidate.startswith("{") or not candidate.rstrip().endswith("}"):
        return None
    if len(candidate) > 128 * 1024:
        return None

    # Stack-based DFS avoids Python recursion limits for large tool arguments. Only ambiguous quotes after a
    # forced inner quote create a second state; ordinary valid JSON is traversed once.
    stack = [(0, False, False, False, [])]
    branches = 0
    while stack and branches <= 256:
        pos, in_string, suspicious, changed, out = stack.pop()
        while pos < len(candidate):
            char = candidate[pos]
            if not in_string:
                if char == '"':
                    out.append(char)
                    pos += 1
                    in_string = True
                    suspicious = False
                    continue
                out.append(char)
                pos += 1
                continue

            if char == "\\":
                # Preserve valid escape pairs. Invalid escapes are outside this narrow repair's scope.
                if pos + 1 >= len(candidate):
                    break
                out.extend((char, candidate[pos + 1]))
                pos += 2
                continue
            if char != '"':
                out.append(char)
                pos += 1
                continue

            next_pos = pos + 1
            while next_pos < len(candidate) and candidate[next_pos].isspace():
                next_pos += 1
            can_close = next_pos < len(candidate) and candidate[next_pos] in ",}]:"
            if not can_close:
                # A quote followed by ordinary text cannot close a JSON string.
                out.extend(("\\", '"'))
                changed = True
                suspicious = True
                pos += 1
                continue

            # Try the grammar-preserving close first. If an earlier JS quote looked like a close, the escape
            # branch is considered as a second interpretation and must still pass json.loads below.
            if suspicious:
                branches += 1
                stack.append((pos + 1, True, True, True, out + ["\\", '"']))
            out.append('"')
            pos += 1
            in_string = False
            suspicious = False

        if pos >= len(candidate) and not in_string and changed:
            repaired = "".join(out)
            try:
                json.loads(repaired, strict=False)
            except (json.JSONDecodeError, TypeError):
                continue
            return repaired
    return None


# 明确的图像生成意图关键词。收窄到「确实在要图」的表达，避免误判
# （如"生成一份报告""create a plan"不该触发生图）。
# 中文：必须是画图/生成图片类；英文：必须含 image/picture/photo/drawing 等图像词。
_IMAGE_INTENT_PATTERNS = (
    "画一", "画个", "画张", "画幅", "画副", "帮我画", "给我画", "画图", "画一张", "画一幅",
    "画个图", "画张图", "画幅图", "画一只", "画只", "画头", "画条",
    "生成图片", "生成一张图", "生成图像", "生成一幅", "生成一张照片", "生成张图", "生成个图",
    "生成海报", "生成插画", "生成一张海报",
    "做一张图", "做个图", "做张图", "做一张海报", "做个海报", "做张海报", "做一张照片",
    "设计一张海报", "设计海报", "设计一张图", "设计张海报", "设计个海报", "设计一幅",
    "画一张海报", "画张海报", "画个海报", "画海报",
    "绘制", "绘一", "画出", "出一张图", "出张图", "p一张", "p个图", "做幅",
    "一张海报", "张海报", "幅海报", "来张图", "来一张图", "来个图",
    "draw a", "draw an", "draw me", "generate an image", "generate a picture",
    "generate an picture", "generate a poster", "create an image", "create a picture",
    "create a photo", "create a poster", "design a poster", "make an image",
    "make a picture", "make a poster", "an image of", "a picture of", "a photo of",
    "a poster of", "image of a", "picture of a", "poster of",
)


# "draw a" / "draw an" 自身不含图像名词，是纯前缀片段。英文里它绝大多数时候就是在
# 要图（"draw a cat" / "draw an elephant"），只有一个收窄的习语宾语闭集是例外
# （"draw a conclusion/distinction/parallel/line/..."）。用负向前瞻排除这个闭集，
# 而不是反过来要求后随图像名词——后者会把 "draw a cat" 这类最常见的英文生图请求
# 也一并排除在外，导致 has_tools 常年为 True、四个 router 全部注入工具 prompt、
# 压制 Gemini 的生图（见 app/routers/openai.py:305 的注释），是本文件曾经出现过的
# 一次过度修正，务必不要重蹈。
#
# R4 曾经把 map/maps、card/cards、line/lines 移出这张闭集，理由是它们既是习语宾语
# 也是真实可画之物（"draw a map of the kingdom"）。该改动已被线上复核实测推翻并
# 回退——不是审美偏好，是两个判错方向的代价不对称：
#
# is_image_generation_intent 只在「客户端声明了 tools」时才有效果——它决定的是要不
# 要压制/丢弃这些 tools、改走工具模拟。于是：
#   - 假阳性（习语被误判成生图意图，如 "draw a line under this discussion" 这类
#     纯习语用法）→ 客户端的 tools 被静默丢弃，agent 的工具循环无声断掉，没有报错、
#     没有日志，调用方根本不知道发生了什么。SEVERE。
#   - 假阴性（真实生图请求未被识别成生图意图）→ 工具 prompt 被注入，这一次的生图
#     可能被压制成文本回复。MILD——用户看得见，换个说法即可重试。
# 因此安全的方向永远是排除更多习语而非更少，哪怕代价是漏掉"draw a map of the
# kingdom"这类真·生图请求的召回率。map/card/line 三组必须留在闭集里，请勿再次
# 移除。
_DRAW_ARTICLE_IDIOM_OBJECTS = (
    "conclusion", "conclusions", "distinction", "distinctions", "parallel", "parallels",
    "line", "lines", "inference", "inferences", "outline", "outlines", "comparison",
    "comparisons", "analogy", "analogies", "attention", "blank", "blanks", "breath",
    "card", "cards", "salary", "crowd", "crowds", "map", "maps",
)
_DRAW_ARTICLE_RE = (
    r"\bdraw an?\b(?!\s+(?:" + "|".join(_DRAW_ARTICLE_IDIOM_OBJECTS) + r")\b)"
)


def _build_ascii_intent_re() -> re.Pattern[str]:
    """把英文模式编译成带 \\b 词边界的正则。
    裸子串匹配会在词内命中（"a photo of" 命中 "data photo of-mode"），\\b 挡掉这类噪声。

    "draw a" / "draw an" 单独处理，见 _DRAW_ARTICLE_RE 上方注释。

    alts 为空（理论上不会发生，除非关键词表被清空）时绝不能 re.compile("")——
    那会匹配任意字符串，等于把 has_tools 判定又打回「全体误判」的老路。
    """
    strong = []
    has_draw_article = False
    for p in _IMAGE_INTENT_PATTERNS:
        if not p.isascii():
            continue
        if p in ("draw a", "draw an"):
            has_draw_article = True
            continue
        strong.append(re.escape(p))

    alts = []
    if strong:
        alts.append(rf"\b(?:{'|'.join(strong)})\b")
    if has_draw_article:
        alts.append(_DRAW_ARTICLE_RE)

    if not alts:
        return re.compile(r"(?!)")  # 永不匹配，安全侧兜底
    return re.compile("|".join(alts))


_ASCII_INTENT_RE = _build_ascii_intent_re()
# 中文没有词边界概念，\b 对 CJK 无意义，保持原样的子串匹配。
_CJK_INTENT_PATTERNS = tuple(p for p in _IMAGE_INTENT_PATTERNS if not p.isascii())


def is_image_generation_intent(text: str) -> bool:
    """判断用户消息是否是明确的「生成图片」意图。
    用于：即使请求带 tools（agent 场景），只要是生图意图就跳过工具模拟、直接走生图。
    收窄匹配避免误判：单独的"生成/create/generate"不算，必须是明确的画图/生成图片表达。

    注意：调用方必须只传「最后一轮用户消息」的文本，不能传整段拍平的 prompt —— 后者
    含 system 提示词、历史轮次与 tool_result 正文，里面的图片字样并不是用户在要图，
    误判会静默丢掉客户端声明的 tools（见 app/utils/prompt.py:last_user_text）。
    """
    if not text:
        return False
    low = text.lower()
    if any(p in low for p in _CJK_INTENT_PATTERNS):
        return True
    return bool(_ASCII_INTENT_RE.search(low))


# 宽松版：图像名词 + 产出动词的组合。用于「流式分流」兜底——
# 误判只是让该请求走非流式 buffered（慢一点点），代价极小；
# 但能兜住关键词没精确覆盖的生图表达，避免漏网走真流式导致图在文字后。
_IMG_NOUNS = ("图", "图片", "图像", "海报", "插画", "照片", "壁纸", "logo", "头像", "封面",
              "image", "picture", "poster", "photo", "drawing", "illustration", "wallpaper", "avatar",
              # 严格判断（is_image_generation_intent）现在用负向前瞻排除 "draw a
              # conclusion/distinction/outline/..." 这一小撮习语闭集（见 _DRAW_ARTICLE_RE），
              # 对这些句子仍返回 False。宽松判断绝不能跟着收窄——它决定生图的加长 POST 超时
              # （180s vs 60s）与 buffered 分流，误判代价极小（多走 buffered），漏判代价是
              # 真·生图超时。补进这两个短语后，凡含 "draw a/an" 的文本（包括这些习语句）
              # has_noun+has_verb 仍必然为 True，宽松判断的结果与 efbdd1d 改动前逐例一致
              # （证据见 tests/unit/test_tools.py::TestMaybeImageGenerationIntentParity）。
              "draw a", "draw an")
_IMG_VERBS = ("画", "生成", "绘", "做", "设计", "出", "整", "来", "搞", "弄", "制作", "帮我", "给我",
              "想要", "要", "想", "需要", "求", "来一", "来个", "来张",
              "draw", "generate", "create", "make", "design", "render", "want", "need")


def maybe_image_generation_intent(text: str) -> bool:
    """宽松判断是否可能是生图意图（用于流式分流兜底，宁可多走 buffered 不漏网）。"""
    if not text:
        return False
    if is_image_generation_intent(text):
        return True
    low = text.lower()
    has_noun = any(n in low for n in _IMG_NOUNS)
    has_verb = any(v in low for v in _IMG_VERBS)
    return has_noun and has_verb


# Appended to the END of every tool-mode prompt. A prefix-only instruction is the first thing a chat model
# forgets in a long turn; the community fix for the "agent stalls after 1-2 tool calls" problem (issues #97
# and friends) is the same reminder restated right before the model answers. Kept in the model's own
# instruction voice and deliberately blunt: "promise" phrasing is what makes a harness end the turn.
TOOL_LOOP_REMINDER = (
    "[MANDATORY — READ BEFORE ANSWERING]\n"
    "You are inside a tool loop. If the task needs a command, a file read or a file write, ANSWER WITH THE "
    "tool_call JSON AND NOTHING ELSE — do it in this message.\n"
    "Never answer with a promise, a plan, a status update or a progress report ('I will inspect...', "
    "'Let me check...', 'Working on it...', 'I'll let you know...'). Those end the turn and the work stops.\n"
    "Text is only for the FINAL answer, after the work is done and verified, or for a question you truly "
    "cannot answer with the tools you have."
)


def _tool_loop_reminder_enabled() -> bool:
    """Read the setting lazily so tests/settings panels can toggle it at runtime."""
    try:
        from app.config import settings
        return bool(getattr(settings, "tool_loop_reminder", True))
    except Exception:  # config import must never break prompt building
        return True


def build_tool_prompt(prompt: str, tools: list[dict], tool_choice=None) -> str:
    if not tools:
        return prompt

    tool_descriptions = []
    for tool in tools:
        if isinstance(tool, dict):
            func = tool.get("function", tool)
            name = func.get("name", "")
            desc = func.get("description", "")
            params = func.get("parameters", func.get("input_schema", {}))
            tool_descriptions.append(
                f"- {name}: {desc}\n  Parameters: {json.dumps(params, ensure_ascii=False)}"
            )

    tools_text = "\n".join(tool_descriptions)

    choice_instruction = ""
    if tool_choice == "required":
        choice_instruction = "You MUST use one of the available tools. "
    elif tool_choice == "none":
        choice_instruction = "Do NOT use any tools. Respond with text only. "
    elif isinstance(tool_choice, dict):
        forced_name = tool_choice.get("function", {}).get("name", "")
        choice_instruction = f"You MUST use the tool: {forced_name}. "
    else:
        choice_instruction = (
            "When the user request requires taking actions, inspecting files, executing commands, "
            "or gathering information that can be obtained via tools, you MUST call the appropriate tool(s) "
            "rather than replying with plain text or explanations alone. "
            "Do NOT attempt to use Google Workspace extensions, Google Docs, Drive, or Gmail. "
            "Never ask for permission to turn on Workspace apps. "
        )

    system_block = (
        f"You have access to the following tools:\n{tools_text}\n\n"
        f"{choice_instruction}\n"
        "To CALL a tool, output a JSON object EXACTLY like this (tool_calls MUST be an array):\n"
        '{"status": "tool_use", "tool_calls": [{"name": "<tool_name>", "arguments": {<args>}}]}\n\n'
        "To reply with plain text instead, output:\n"
        '{"status": "text", "content": "<your reply>"}\n\n'
        "STRICT JSON RULES (follow exactly, or the call fails):\n"
        '1. Use double quotes for all keys and string values. Escape inner quotes as \\" and newlines as \\n.\n'
        "2. \"tool_calls\" is ALWAYS a JSON array [ ... ], even for a single call.\n"
        "3. \"arguments\" is a JSON object with the tool's parameters.\n"
        "4. Output ONLY the JSON (a ```json code block is allowed, nothing else).\n\n"
        "Example (calling a tool named run with a command argument):\n"
        '{"status": "tool_use", "tool_calls": [{"name": "run", "arguments": {"command": "ls -la"}}]}\n\n'
        "Example (plain text reply):\n"
        '{"status": "text", "content": "Hello, how can I help?"}'
    )

    tail = ""
    # tool_choice="none" means the caller explicitly wants text only — a reminder to call a tool would
    # contradict it.
    if tool_choice != "none" and _tool_loop_reminder_enabled():
        tail = f"\n\n{TOOL_LOOP_REMINDER}"

    return f"{system_block}\n\nUser message: {prompt}{tail}"


def _strip_code_fence(text: str) -> str:
    """剥离 markdown 代码块包裹：```json ... ``` 或 ``` ... ```。"""
    s = text.strip()
    if s.startswith("```"):
        # 去掉首行 ``` 或 ```json
        s = re.sub(r"^```[a-zA-Z0-9]*\s*\n?", "", s)
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    return s.strip()


def _extract_json_object(text: str) -> str | None:
    """从文本中提取第一个完整的 JSON 对象子串（括号深度扫描，处理字符串与转义）。
    用于模型在 JSON 前后夹带了解释文字的情况。"""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
    return None


def _normalize_tool_calls(tc) -> list | None:
    """把 tool_calls 归一成 [{"name","arguments"}] 列表；容忍单对象、容忍 arguments 是字符串。"""
    if isinstance(tc, dict):
        tc = [tc]
    if not isinstance(tc, list):
        return None
    out = []
    for item in tc:
        if not isinstance(item, dict):
            continue
        # 兼容 OpenAI 风格 {"function":{"name","arguments"}} 和简单 {"name","arguments"}
        fn = item.get("function") if isinstance(item.get("function"), dict) else item
        name = fn.get("name")
        if not name:
            continue
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args, strict=False)
            except Exception:
                args = {"_raw": args}
        out.append({"name": name, "arguments": args if isinstance(args, dict) else {}})
    return out or None


def _unwrap_text_status_envelope(text: str) -> dict | None:
    """If the model wrapped its reply in a {"status": "text", "content": "..."} envelope
    that failed strict json.loads (e.g. unescaped control characters or unescaped quotes),
    extract and unwrap the content string so raw JSON envelopes do not leak to clients."""
    s = text.strip()
    if s.startswith("```"):
        s = _strip_code_fence(s)

    if not ('"status"' in s and '"text"' in s and '"content"' in s):
        return None

    # First attempt: match "content": "(...)" ending before next field or closing brace
    m = re.search(
        r'"content"\s*:\s*"(.*?)(?<!\\)(?:"\s*,\s*"[a-zA-Z0-9_]+"\s*:|"\s*\}\s*$)',
        s,
        re.DOTALL,
    )
    if m:
        content = m.group(1).replace('\\"', '"').replace('\\\\', '\\')
        return {"type": "text", "content": content}

    # Second attempt: fallback to boundary search between "content": " and the final quote
    m_start = re.search(r'"content"\s*:\s*"', s)
    if not m_start:
        return None
    content_start = m_start.end()
    last_brace = s.rfind("}")
    if last_brace == -1:
        last_brace = len(s)
    content_end = s.rfind('"', 0, last_brace)
    if content_end > content_start:
        content = s[content_start:content_end]
        content = content.replace('\\"', '"').replace('\\\\', '\\')
        return {"type": "text", "content": content}
    return None


def _try_parse(text: str, diagnostics: MutableMapping | None = None) -> dict | None:
    """Try to parse a tool/text envelope and retain the JSON error for diagnostics."""
    try:
        parsed = json.loads(text, strict=False)
    except json.JSONDecodeError as exc:
        _set_parse_diagnostic(diagnostics, **_json_error_details(exc, text))
        parsed = None
    except TypeError as exc:
        _set_parse_diagnostic(diagnostics, error=str(exc), failure_class="json_type", response_length=len(text))
        parsed = None

    if isinstance(parsed, dict):
        status = parsed.get("status", "")
        # 有 tool_calls 即视为工具调用（容忍缺 status）
        if "tool_calls" in parsed or status == "tool_use":
            calls = _normalize_tool_calls(parsed.get("tool_calls"))
            if calls:
                return {"type": "tool_calls", "tool_calls": calls}
        if status == "text" and "content" in parsed:
            return {"type": "text", "content": parsed["content"]}
        # 整段就是一个工具调用对象 {"name":...,"arguments":...}
        if "name" in parsed and ("arguments" in parsed or "function" in parsed):
            calls = _normalize_tool_calls(parsed)
            if calls:
                return {"type": "tool_calls", "tool_calls": calls}
        return None

    return _unwrap_text_status_envelope(text)


def parse_tool_response(text: str, *, diagnostics: MutableMapping | None = None) -> dict:
    """Parse the model's prompt-simulated tool-call response.

    The return shape remains backwards-compatible. When ``diagnostics`` is supplied it receives the response
    length, the last JSON decoder reason and whether the narrow complete-string quote repair was used.
    Truncated JSON is never completed.
    """
    if diagnostics is not None:
        diagnostics.clear()
        diagnostics.update({
            "response_length": len(text) if isinstance(text, str) else 0,
            "repaired": False,
            "truncated_suspected": False,
        })
    if not isinstance(text, str) or not text.strip():
        return {"type": "text", "content": text or ""}

    def _clean_success(diag: MutableMapping | None, length: int) -> None:
        if diag is None:
            return
        diag.pop("failure_class", None)
        diag.pop("error", None)
        diag.pop("position", None)
        diag.update({
            "response_length": length,
            "repaired": False,
            "truncated_suspected": False,
        })

    # 1. 直接解析
    r = _try_parse(text, diagnostics)
    if r:
        _clean_success(diagnostics, len(text))
        return r
    # 2. 剥离 markdown 代码块后解析
    stripped = _strip_code_fence(text)
    if stripped != text:
        r = _try_parse(stripped, diagnostics)
        if r:
            _clean_success(diagnostics, len(text))
            return r
    # 3. 从文本中提取第一个完整 JSON 对象后解析
    candidate = _extract_json_object(stripped)
    if candidate:
        r = _try_parse(candidate, diagnostics)
        if r:
            _clean_success(diagnostics, len(text))
            return r

    # 3b. 尝试最外层大括号提取（防内层嵌套引号破坏深度扫描）
    start_brace = stripped.find("{")
    end_brace = stripped.rfind("}")
    outer_candidate = None
    if start_brace != -1 and end_brace > start_brace:
        outer_candidate = stripped[start_brace:end_brace + 1]
        if outer_candidate != candidate:
            r = _try_parse(outer_candidate, diagnostics)
            if r:
                _clean_success(diagnostics, len(text))
                return r

    # 4. A complete object with an otherwise valid structure may contain an unescaped quote in an argument
    # string. Try only the bounded state-machine repair; it cannot invent a closing brace or string quote.
    looks_like_tool = ('"tool_use"' in text or '"tool_calls"' in text
                       or ('"name"' in text and '"arguments"' in text))
    if looks_like_tool:
        repair_candidates = []
        for value in (outer_candidate, stripped):
            if value and value not in repair_candidates:
                repair_candidates.append(value)
        for value in repair_candidates:
            repaired = _repair_complete_unescaped_quotes(value)
            if repaired is None:
                continue
            repaired_result = _try_parse(repaired, diagnostics)
            if repaired_result:
                _set_parse_diagnostic(
                    diagnostics,
                    repaired=True,
                    failure_class="unescaped_quote_repaired",
                    response_length=len(text),
                    truncated_suspected=False,
                )
                logger.warning(
                    "Tool-call JSON contained an unescaped quote; repaired a complete argument string "
                    "(response_length=%d)", len(text)
                )
                return repaired_result

        truncated_suspected = _looks_truncated_json(text)
        _set_parse_diagnostic(
            diagnostics,
            response_length=len(text),
            truncated_suspected=truncated_suspected,
            failure_class=("truncated_json" if truncated_suspected else diagnostics.get("failure_class", "json_syntax")
                           if diagnostics is not None else "json_syntax"),
        )
        reason = diagnostics.get("error", "unknown JSON error") if diagnostics is not None else "unknown JSON error"
        failure_class = diagnostics.get("failure_class", "json_syntax") if diagnostics is not None else "json_syntax"
        logger.warning(
            "Tool-call JSON parse failed (%s; %s; response_length=%d; truncated_suspected=%s); "
            "malformed fragment will not be forwarded: %r",
            failure_class, reason, len(text), truncated_suspected, text[:120],
        )
        return {"type": "text", "content": MALFORMED_TOOL_NOTICE}

    # 5. Try to unwrap a text envelope so raw JSON does not leak to clients.
    envelope = _unwrap_text_status_envelope(text)
    if envelope:
        _clean_success(diagnostics, len(text))
        return envelope

    # 普通文本（非工具意图）原样返回
    _clean_success(diagnostics, len(text))
    return {"type": "text", "content": text}


def is_malformed_tool_result(parsed) -> bool:
    """判断 parse_tool_response 的结果是不是「畸形工具 JSON」的降级提示。"""
    return (
        isinstance(parsed, dict)
        and parsed.get("type") == "text"
        and parsed.get("content") == MALFORMED_TOOL_NOTICE
    )


def _publish_parse_diagnostics(diagnostics: MutableMapping) -> None:
    try:
        from app.core.request_context import set_field
        set_field("parse_diagnostic", dict(diagnostics))
    except Exception:
        pass


async def parse_tool_response_with_retry(text: str, regenerate, *, budget=None,
                                         diagnostics: MutableMapping | None = None) -> dict:
    """Parse a tool response and, if necessary, regenerate at most once.

    ``budget`` is shared with ``agent_loop.guard_tool_loop`` by every router. A missing budget keeps the
    standalone helper's historical one-retry behaviour for callers that use it directly.
    """
    local_diagnostics = diagnostics if diagnostics is not None else {}
    parsed = parse_tool_response(text, diagnostics=local_diagnostics)
    if regenerate is None or not is_malformed_tool_result(parsed):
        _publish_parse_diagnostics(local_diagnostics)
        return parsed

    if budget is None:
        from app.core.regeneration_budget import RegenerationBudget
        budget = RegenerationBudget()
    if not budget.try_consume("parse_retry"):
        logger.warning("Tool-call retry skipped: the per-turn regeneration budget is exhausted")
        _publish_parse_diagnostics(local_diagnostics)
        return parsed

    logger.warning("Tool-call JSON is malformed; regenerating once (no truncated-JSON repair)")
    try:
        retry_text = await regenerate()
    except Exception as e:
        logger.warning(f"Tool-call retry failed; keeping the first degraded result: {type(e).__name__}")
        _publish_parse_diagnostics(local_diagnostics)
        return parsed

    if not isinstance(retry_text, str) or not retry_text.strip():
        _publish_parse_diagnostics(local_diagnostics)
        return parsed

    retried_diagnostics = {}
    retried = parse_tool_response(retry_text, diagnostics=retried_diagnostics)
    if diagnostics is not None:
        diagnostics["retry"] = retried_diagnostics
    _publish_parse_diagnostics(retried_diagnostics if not is_malformed_tool_result(retried) else (diagnostics or retried_diagnostics))
    if is_malformed_tool_result(retried):
        logger.warning("Tool-call retry is still malformed; no further retry")
        return parsed
    return retried


def estimate_tokens(text: str) -> int:
    return len(text) // 4
