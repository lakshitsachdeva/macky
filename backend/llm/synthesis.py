from __future__ import annotations

import ast
import operator
import re
from datetime import datetime, timezone
from typing import Any

from .local_llm import LocalLLM
from .prompts import (
    COMMUNITY_SUMMARY_PROMPT,
    EVOLUTION_PROMPT,
    PROACTIVE_BRIEFING_PROMPT,
    SYNTHESIS_PROMPT,
    TEMPORAL_SYNTHESIS_PROMPT,
)


def _safe_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _clip(value: Any, *, limit: int = 240) -> str:
    text = _safe_text(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    cleaned = str(value).strip()
    if not cleaned:
        return None
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    parsed = datetime.fromisoformat(cleaned)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_date(value: str | None) -> str:
    parsed = _parse_iso(value)
    if parsed is None:
        return "unknown date"
    return parsed.date().isoformat()


def _result_title(item: dict[str, Any]) -> str:
    return _safe_text(item.get("title") or item.get("filename") or item.get("id") or "Untitled")


def _render_search_results(results: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for index, result in enumerate(results[:6], start=1):
        title = _result_title(result)
        preview = _clip(result.get("preview_text") or result.get("summary") or "", limit=180)
        source_type = _safe_text(result.get("source_type") or result.get("file_type") or "item")
        lines.append(f"{index}. {title} ({source_type})")
        if preview:
            lines.append(f"   {preview}")
    return "\n".join(lines)


def _render_temporal_entities(active_entities: list[dict[str, Any]]) -> str:
    if not active_entities:
        return "None detected."
    return "\n".join(
        f"- {entity['name']} ({entity['entity_type']}, mentions={entity.get('period_mentions', 0)})"
        for entity in active_entities[:8]
    )


def _render_temporal_episodes(episodes: list[dict[str, Any]]) -> str:
    if not episodes:
        return "No episodes in this window."
    return "\n".join(
        f"- {_safe_text(episode.get('title') or episode.get('item_id'))} on {_format_date(episode.get('timestamp'))}"
        for episode in episodes[:8]
    )


def _render_evolution_timeline(timeline: list[dict[str, Any]], episodes: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for entry in timeline[:8]:
        parts.append(
            f"- {_format_date(entry.get('valid_from'))}: {entry.get('subject')} {entry.get('predicate')} {entry.get('object')}"
        )
    for episode in episodes[:6]:
        parts.append(
            f"- {_format_date(episode.get('timestamp'))}: {_safe_text(episode.get('title') or episode.get('item_id'))}"
        )
    return "\n".join(parts) if parts else "No evolution data available."


def _render_community_relationships(community: dict[str, Any]) -> str:
    return f"edges={community.get('edge_count', 0)}, episodes={community.get('episode_count', 0)}"


def _stage_for_community(community: dict[str, Any]) -> str:
    last_activity = _parse_iso(str(community.get("last_activity") or ""))
    if last_activity is None:
        return "unknown"
    days_ago = max(0, int((datetime.now(timezone.utc) - last_activity).total_seconds() // 86400))
    if days_ago <= 30:
        return "active"
    if days_ago <= 120:
        return "dormant"
    return "completed"


def build_search_fallback_summary(query: str, results: list[dict[str, Any]]) -> str:
    if not results:
        return f'You do not have any strong matches yet for "{query}".'
    top_titles = [_result_title(item) for item in results[:3]]
    summary = f'You have {len(results)} relevant results for "{query}". The strongest matches are {", ".join(top_titles)}.'
    if len(results) > 1:
        summary += " These results cluster around the same topic and should be the best place to start."
    return summary


def build_temporal_fallback_summary(query: str, period_payload: dict[str, Any]) -> str:
    time_window = period_payload.get("time_window", {})
    episodes = list(period_payload.get("episodes") or [])
    active_entities = list(period_payload.get("active_entities") or [])
    start = _format_date(time_window.get("start_date"))
    end = _format_date(time_window.get("end_date"))
    if not episodes:
        return f'You do not have clear activity for "{query}" between {start} and {end}.'
    entity_names = ", ".join(entity["name"] for entity in active_entities[:3]) if active_entities else "a few related topics"
    top_titles = ", ".join(_safe_text(episode.get("title") or episode.get("item_id")) for episode in episodes[:3])
    return (
        f"Between {start} and {end}, you were working around {entity_names}. "
        f"The clearest items in that window are {top_titles}."
    )


def build_evolution_fallback_summary(topic: str, evolution_payload: dict[str, Any]) -> str:
    episodes = list(evolution_payload.get("episodes") or [])
    timeline = list(evolution_payload.get("timeline") or [])
    if not episodes and not timeline:
        return f'You do not have enough history yet to describe how "{topic}" evolved.'
    first_point = episodes[0]["timestamp"] if episodes else timeline[0].get("valid_from")
    last_point = episodes[-1]["timestamp"] if episodes else timeline[-1].get("valid_from")
    return (
        f'Your history on "{topic}" runs from {_format_date(first_point)} to {_format_date(last_point)}. '
        f"There are {len(episodes)} related episodes and {len(timeline)} timeline facts in the current graph snapshot."
    )


def build_community_fallback_summary(community: dict[str, Any]) -> str:
    top_entities = [entity["name"] for entity in community.get("top_entities", [])[:4]]
    if not top_entities:
        return "This cluster is connected, but it still needs richer entity extraction to summarize cleanly."
    return (
        f'This cluster is about {", ".join(top_entities)}. '
        f'It covers {community.get("episode_count", 0)} episodes and currently looks {_stage_for_community(community)}.'
    )


def build_proactive_fallback_briefing(current_file: str, related_items: list[dict[str, Any]]) -> str:
    if not related_items:
        return f'You do not have strong related context yet for "{current_file}".'
    bullets = [
        f"- {_result_title(item)}"
        for item in related_items[:3]
    ]
    return "\n".join(bullets)


def _normalize_response_text(text: str) -> str:
    """Preserve line structure — collapse runs of spaces within each line
    but keep newlines so bullet lists survive to the frontend renderer."""
    lines = str(text or "").split("\n")
    normalized: list[str] = []
    prev_blank = False
    for line in lines:
        stripped = " ".join(line.split()).strip()
        if stripped == "":
            # Collapse consecutive blank lines to one
            if not prev_blank:
                normalized.append("")
            prev_blank = True
        else:
            normalized.append(stripped)
            prev_blank = False
    return "\n".join(normalized).strip()


# ── Ask (conversational Q&A) synthesis ───────────────────────────────────────
# The /ask endpoint shifts the UX from "results list" to "answer + sources".
# The prompt is kept short and grounded — the model is told to refuse rather
# than invent, and to cite by source index so the frontend can render clickable
# source pills next to the answer text.

_ASK_PROMPT = """You are Recurdo. You live inside the user's computer and know everything they've worked on — every file, every note, every email. You're like their smartest friend who has a perfect memory of their entire work life. You're warm, direct, a little casual, and genuinely helpful. You actually care.

TODAY'S DATE: {today}
USER: {user_identity}

QUESTION: "{question}"

SOURCES (your memory — numbered, with file, date, and content):
{sources_block}

FORMATTING (the UI is plain text — follow exactly):
- No markdown. No * ** _ # — they show as literal characters.
- For lists, use • on its own line per item.
- Cite inline: [1], [2][4], etc. Every factual claim needs one.

HOW TO RESPOND:

For RECAP / ACTIVITY questions ("what was I doing", "recap my week", "what did I work on"):
  Write like you're catching them up over coffee. Lead with the big picture of what the week/period felt like, then • one bullet per distinct project or area — include the actual date, what specifically happened, and why it matters. Cover the full time range in the sources, not just one day. Be rich and specific.

For FACTUAL questions ("what did I decide", "what does X say"):
  Answer directly in 2-4 sentences. Pull the actual detail from the source — quote key phrases if helpful.

For TIMELINE / HISTORY questions ("since when", "how long have I been"):
  Look at the earliest modification dates in the sources. Estimate from there.

For SEARCH / LOOKUP ("find my notes on X"):
  Describe what you found and what's actually in it.

PERSONALITY:
  Warm but efficient. Talk like a person, not a database. Instead of "You updated the document titled X on date Y", say "You spent time on X — looks like you were working through Z". Show you understand what they were actually doing, not just that a file was modified.
  You know the user's name. If they ask "what's my name", answer directly from the USER field above — do NOT search through files for it.
  Address them by their first name occasionally when it feels natural (not every sentence).

RULES:
- If the user asks about themselves ("what's my name", "who am I", "what's my email"), answer from the USER field — never say you don't know their name.
- Use ONLY information from the numbered sources. Never invent details.
- Your answer must reflect the FULL date range shown in "Sources span" above. If sources cover multiple days or weeks, narrate across that entire period — do not fixate on only the most recent date.
- For relative time ("last week", "yesterday"), use TODAY'S DATE + source modification dates.
- If sources genuinely don't have the answer: "I don't have enough in your library for this." No guessing.
- Do NOT restate the question. Sources display separately — don't list them at the end.

ANSWER:"""


def _render_ask_sources(results: list[dict[str, Any]], *, limit: int = 10) -> str:
    """Renders retrieved items as a numbered [N] block suitable for citation
    in the ask prompt. Rich snippets give Gemini enough content to synthesize
    a meaningful, specific answer rather than a vague summary."""
    lines: list[str] = []
    date_objs: list[datetime] = []
    for index, result in enumerate(results[:limit], start=1):
        title = _result_title(result)
        filename = _safe_text(result.get("filename") or "")
        source_type = _safe_text(result.get("source_type") or result.get("file_type") or "item")
        modified = _format_date(result.get("modified_at") or result.get("created_at"))
        # Collect parsed dates for the span header
        parsed_dt = _parse_iso(result.get("modified_at") or result.get("created_at"))
        if parsed_dt is not None:
            date_objs.append(parsed_dt)
        # Prefer extracted_text (full doc content) then fallbacks
        raw_text = (
            result.get("extracted_text")
            or result.get("preview_text")
            or result.get("summary")
            or ""
        )
        snippet = _clip(raw_text, limit=320)
        header = f"[{index}] {title}"
        if filename and filename not in title:
            header += f" ({filename})"
        header += f" — {source_type}, modified {modified}"
        lines.append(header)
        if snippet:
            lines.append(f"    {snippet}")
        lines.append("")  # blank line between sources for readability

    if not lines:
        return "(no sources retrieved)"

    # Prepend a "Sources span:" header so the LLM knows the full date range
    if date_objs:
        min_date = min(date_objs).date()
        max_date = max(date_objs).date()
        fmt = "%B %-d, %Y"
        if min_date == max_date:
            span_line = f"Sources span: {min_date.strftime(fmt)}"
        else:
            span_line = f"Sources span: {min_date.strftime(fmt)} to {max_date.strftime(fmt)}"
        return span_line + "\n\n" + "\n".join(lines).strip()

    return "\n".join(lines).strip()


def _infer_answer_confidence(answer_text: str, results: list[dict[str, Any]]) -> str:
    """Rough heuristic for client-side styling. Frontend treats 'insufficient'
    specially. We grade by citations and answer length — a rich detailed answer
    with sources is high, a one-liner with no citations is low."""
    lowered = (answer_text or "").lower()
    if not results:
        return "insufficient"
    if (
        "don't have enough" in lowered
        or "do not have enough" in lowered
        or "cannot answer" in lowered
        or "can't answer" in lowered
        or "not enough in your library" in lowered
    ):
        return "insufficient"
    import re as _re
    citations = len(set(_re.findall(r"\[(\d+)\]", answer_text or "")))
    answer_len = len((answer_text or "").split())
    # Rich answer with multiple citations = high
    if citations >= 2 or (citations >= 1 and answer_len >= 40):
        return "high"
    # Short answer with one citation or long answer without citation = medium
    if citations == 1 or answer_len >= 30:
        return "medium"
    return "low"


def build_ask_fallback_answer(question: str, results: list[dict[str, Any]]) -> str:
    """Deterministic fallback when no LLM provider is configured. We still
    want to return something useful — a top-3 list framed as an answer —
    rather than failing the request entirely."""
    if not results:
        return f'I do not have matches in your library for "{question}".'
    top = [_result_title(item) for item in results[:3]]
    if len(top) == 1:
        return f'The most relevant item I found for "{question}" is {top[0]} [1].'
    return (
        f'The most relevant items in your library for "{question}" are '
        f"{top[0]} [1] and {top[1]} [2]"
        + (f", plus {top[2]} [3]." if len(top) >= 3 else ".")
    )


_ARITHMETIC_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _eval_safe_arithmetic(node: ast.AST) -> float | int:
    if isinstance(node, ast.Expression):
        return _eval_safe_arithmetic(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _ARITHMETIC_OPERATORS:
        left = _eval_safe_arithmetic(node.left)
        right = _eval_safe_arithmetic(node.right)
        if isinstance(node.op, ast.Pow) and abs(float(right)) > 8:
            raise ValueError("exponent too large")
        return _ARITHMETIC_OPERATORS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ARITHMETIC_OPERATORS:
        return _ARITHMETIC_OPERATORS[type(node.op)](_eval_safe_arithmetic(node.operand))
    raise ValueError("unsupported expression")


def _answer_simple_general_question(question: str) -> dict[str, Any] | None:
    """Handle obvious non-memory utility questions without pretending the
    user's files need to contain them. This keeps Recurdo honest for memory
    answers while avoiding silly failures like "what is 2+2?"."""
    cleaned = question.strip().lower()
    arithmetic = re.sub(r"^(what is|what's|calculate|solve)\s+", "", cleaned)
    arithmetic = arithmetic.rstrip("?= ")
    arithmetic = arithmetic.replace("×", "*").replace("÷", "/")
    if re.fullmatch(r"[0-9\s+\-*/().%]+", arithmetic or "") and re.search(r"\d", arithmetic):
        try:
            parsed = ast.parse(arithmetic, mode="eval")
            value = _eval_safe_arithmetic(parsed)
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            return {
                "text": f"{arithmetic} = {value}",
                "provider": "heuristic",
                "confidence": "high",
                "prompt_key": "ask_answer",
            }
        except Exception:
            return None
    return None


async def synthesize_ask_answer(
    question: str,
    results: list[dict[str, Any]],
    *,
    llm: LocalLLM | None = None,
    allow_model: bool = True,
    source_limit: int = 10,
    conversation_history: list[dict] | None = None,
    user_display_name: str | None = None,
) -> dict[str, Any]:
    """Produce an {answer, provider, confidence} dict for the /ask endpoint.

    The caller is responsible for pairing the answer with the source list
    (which it already has — this avoids the LLM needing to echo back source
    metadata and keeps the token budget focused on the answer itself).
    """
    simple_answer = _answer_simple_general_question(question)
    if simple_answer is not None:
        return simple_answer

    from datetime import date
    sources_block = _render_ask_sources(results, limit=source_limit)
    user_identity = user_display_name.strip() if user_display_name else "Unknown"
    base_prompt = _ASK_PROMPT.format(
        today=date.today().strftime("%A, %B %d, %Y"),
        user_identity=user_identity,
        question=question,
        sources_block=sources_block,
    )

    # Inject prior conversation turns between SOURCES and INSTRUCTIONS so
    # Gemini has context without letting history override the main instructions.
    if conversation_history:
        prior_lines: list[str] = ["PRIOR CONVERSATION (for context):"]
        for turn in conversation_history:
            q = (turn.get("question") or "").strip()
            a = (turn.get("answer") or "").strip()
            if q:
                prior_lines.append(f"[You asked]: {q}")
            if a:
                prior_lines.append(f"[Recurdo answered]: {a}")
            prior_lines.append("")
        prior_lines.append("---")
        prior_block = "\n".join(prior_lines)
        # Insert prior block after SOURCES block and before FORMATTING section
        prompt = base_prompt.replace(
            "\nFORMATTING",
            f"\n{prior_block}\n\nFORMATTING",
            1,
        )
    else:
        prompt = base_prompt

    # Use the quality tier for ask responses — these are front-and-center in
    # the UI and a tier bump costs ~200ms on MPS for the length we generate.
    model = llm or LocalLLM("quality")
    status = model.status()
    if allow_model and status["available"]:
        try:
            text = await model.generate(prompt, max_tokens=1500, temperature=0.15)
            answer_text = _normalize_response_text(text)
            if not answer_text:
                raise ValueError("LLM returned empty answer")
            return {
                "text": answer_text,
                "provider": status["provider"],
                "confidence": _infer_answer_confidence(answer_text, results),
                "prompt_key": "ask_answer",
            }
        except Exception as exc:
            # Fall through to heuristic rather than 500ing the endpoint —
            # a degraded answer is better than no answer for the beta demo.
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "synthesize_ask_answer LLM call failed, using fallback: %s", exc
            )

    fallback = build_ask_fallback_answer(question, results)
    return {
        "text": fallback,
        "provider": "heuristic",
        "confidence": "insufficient" if not results else "low",
        "prompt_key": "ask_answer",
    }


async def synthesize_search_briefing(
    query: str,
    results: list[dict[str, Any]],
    *,
    llm: LocalLLM | None = None,
    allow_model: bool = True,
) -> dict[str, Any]:
    prompt = SYNTHESIS_PROMPT.format(
        query=query,
        retrieved_content=_render_search_results(results),
    )
    model = llm or LocalLLM("quality")
    if allow_model and model.status()["available"]:
        try:
            text = await model.generate(prompt, max_tokens=320, temperature=0.1)
            return {"summary": _normalize_response_text(text), "provider": model.status()["provider"], "prompt_key": "search_synthesis"}
        except Exception:
            pass
    return {"summary": build_search_fallback_summary(query, results), "provider": "heuristic", "prompt_key": "search_synthesis"}


async def synthesize_temporal_briefing(
    query: str,
    period_payload: dict[str, Any],
    *,
    llm: LocalLLM | None = None,
    allow_model: bool = True,
) -> dict[str, Any]:
    time_window = period_payload.get("time_window", {})
    prompt = TEMPORAL_SYNTHESIS_PROMPT.format(
        query=query,
        start=_format_date(time_window.get("start_date")),
        end=_format_date(time_window.get("end_date")),
        active_entities=_render_temporal_entities(list(period_payload.get("active_entities") or [])),
        episodes=_render_temporal_episodes(list(period_payload.get("episodes") or [])),
    )
    model = llm or LocalLLM("quality")
    if allow_model and model.status()["available"]:
        try:
            text = await model.generate(prompt, max_tokens=360, temperature=0.1)
            return {"summary": _normalize_response_text(text), "provider": model.status()["provider"], "prompt_key": "temporal_synthesis"}
        except Exception:
            pass
    return {"summary": build_temporal_fallback_summary(query, period_payload), "provider": "heuristic", "prompt_key": "temporal_synthesis"}


async def synthesize_evolution_briefing(
    topic: str,
    evolution_payload: dict[str, Any],
    *,
    llm: LocalLLM | None = None,
    allow_model: bool = True,
) -> dict[str, Any]:
    prompt = EVOLUTION_PROMPT.format(
        topic=topic,
        timeline=_render_evolution_timeline(
            list(evolution_payload.get("timeline") or []),
            list(evolution_payload.get("episodes") or []),
        ),
    )
    model = llm or LocalLLM("quality")
    if allow_model and model.status()["available"]:
        try:
            text = await model.generate(prompt, max_tokens=360, temperature=0.1)
            return {"summary": _normalize_response_text(text), "provider": model.status()["provider"], "prompt_key": "idea_evolution"}
        except Exception:
            pass
    return {"summary": build_evolution_fallback_summary(topic, evolution_payload), "provider": "heuristic", "prompt_key": "idea_evolution"}


async def synthesize_community_briefing(
    community: dict[str, Any],
    *,
    llm: LocalLLM | None = None,
    allow_model: bool = True,
) -> dict[str, Any]:
    prompt = COMMUNITY_SUMMARY_PROMPT.format(
        entities=", ".join(entity["name"] for entity in community.get("top_entities", [])[:8]) or "None",
        relationships=_render_community_relationships(community),
        recent_episodes=", ".join(str(episode_id) for episode_id in community.get("sample_episode_ids", [])[:8]) or "None",
    )
    model = llm or LocalLLM("quality")
    if allow_model and model.status()["available"]:
        try:
            text = await model.generate(prompt, max_tokens=240, temperature=0.1)
            return {"summary": _normalize_response_text(text), "provider": model.status()["provider"], "prompt_key": "community_summary"}
        except Exception:
            pass
    return {"summary": build_community_fallback_summary(community), "provider": "heuristic", "prompt_key": "community_summary"}


async def synthesize_proactive_briefing(
    current_file: str,
    related_items: list[dict[str, Any]],
    *,
    llm: LocalLLM | None = None,
    allow_model: bool = True,
) -> dict[str, Any]:
    prompt = PROACTIVE_BRIEFING_PROMPT.format(
        current_file=current_file,
        related_items=_render_search_results(related_items),
    )
    model = llm or LocalLLM("standard")
    if allow_model and model.status()["available"]:
        try:
            text = await model.generate(prompt, max_tokens=200, temperature=0.1)
            return {"summary": text.strip(), "provider": model.status()["provider"], "prompt_key": "proactive_briefing"}
        except Exception:
            pass
    return {"summary": build_proactive_fallback_briefing(current_file, related_items), "provider": "heuristic", "prompt_key": "proactive_briefing"}
