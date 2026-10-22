from __future__ import annotations

"""Central prompt registry for the unified LLM layer."""


ENTITY_EXTRACTION_PROMPT = """Extract entities and relationships from this content.

SOURCE TYPE: {source_type}
CONTENT:
{content}

Return ONLY valid JSON. No markdown, no explanation.

{{
  "entities": [
    {{"name": "exact name", "type": "Person|Project|Topic|Organisation|Place|Document|Concept", "confidence": 0.0-1.0}}
  ],
  "relationships": [
    {{"subject": "name", "predicate": "WORKS_ON|RELATES_TO|CREATED|PART_OF|REFERENCES|COLLABORATES_WITH", "object": "name", "confidence": 0.0-1.0}}
  ]
}}

Rules:
- Only extract entities clearly present. Confidence >= 0.7.
- Project names are often capitalized or in quotes.
- If unclear, return {{"entities": [], "relationships": []}}"""


QUERY_UNDERSTANDING_PROMPT = """Analyze this search query.

QUERY: {query}

Return ONLY valid JSON:
{{
  "intent": "find_file|find_email|find_topic|temporal_query|synthesis_query",
  "key_entities": ["entity names mentioned"],
  "time_expression": "any time reference or null",
  "file_types": ["pdf", "docx", "etc if specified"],
  "cleaned_query": "query with time expressions removed",
  "is_complex": true
}}"""


SYNTHESIS_PROMPT = """You are Recurdo, a personal memory assistant.
The user searched for: "{query}"

Here are the most relevant things from their files and emails:
{retrieved_content}

Write a clear, concise synthesis (3-5 sentences) that:
1. Directly answers what the user is looking for
2. Highlights the most relevant pieces
3. Notes important connections between items
4. Is written in second person ("You have...", "Your files show...")

Do NOT invent information. Only use what is provided."""


TEMPORAL_SYNTHESIS_PROMPT = """You are Recurdo, a personal memory assistant.
The user asked: "{query}"
Time period: {start} to {end}

ENTITIES ACTIVE THEN:
{active_entities}

RELEVANT FILES/EMAILS:
{episodes}

Write a 4-6 sentence briefing of what the user was working on during this period.
Be specific. Mention actual project names, people, and topics.
Write in second person. Only use information provided."""


EVOLUTION_PROMPT = """You are Recurdo, a personal memory assistant.
The user wants to understand how their thinking on "{topic}" has evolved.

CHRONOLOGICAL TIMELINE:
{timeline}

Write 4-6 sentences describing:
1. When this topic first appeared in their work
2. How engagement changed over time
3. Current state of their knowledge or work
4. Any notable shifts or pivots

Write in second person. Be specific about dates and changes."""


FRAME_EXTRACTION_PROMPT = """Extract structured information from this {frame_type} document.

CONTENT:
{content}

SLOTS TO FILL:
{slots_json}

Return ONLY valid JSON with one key per slot, null if not found:
{{
  "slot_name": "extracted_value_or_null"
}}

Rules:
- person slots: extract full name or email
- date slots: ISO format YYYY-MM-DD if possible
- amount slots: include currency symbol when present
- list slots: JSON array of strings
- status slots: pick the most specific word present"""


PROACTIVE_BRIEFING_PROMPT = """You are Recurdo. The user just opened: "{current_file}"

Here are the most relevant items from their memory:
{related_items}

Write 2-3 short bullet points with the most useful context.
Be specific. Name actual files and dates. No vague connections."""


SCRIPT_DETECTION_PROMPT = """Look at these documents from the past 30 days.

DOCUMENTS (chronological):
{documents}

Do these documents indicate an active work pattern (script)?
Choose from: fundraising, project_kickoff, hiring_process, client_outreach, none

Return ONLY valid JSON:
{{
  "detected_script": "script_name_or_none",
  "current_scene": "scene_name_or_null",
  "confidence": 0.0-1.0,
  "evidence": ["brief reason 1", "brief reason 2"]
}}"""


COMMUNITY_SUMMARY_PROMPT = """Summarize this cluster of related entities.

ENTITIES: {entities}
RELATIONSHIPS: {relationships}
RECENT ACTIVITY: {recent_episodes}

Write 2-3 sentences describing:
- What this cluster is about
- Key nodes
- Stage: active, dormant, or completed

This appears as a memory-cluster label in the UI."""


PROMPT_REGISTRY = {
    "entity_extraction": ENTITY_EXTRACTION_PROMPT,
    "query_understanding": QUERY_UNDERSTANDING_PROMPT,
    "search_synthesis": SYNTHESIS_PROMPT,
    "temporal_synthesis": TEMPORAL_SYNTHESIS_PROMPT,
    "idea_evolution": EVOLUTION_PROMPT,
    "frame_extraction": FRAME_EXTRACTION_PROMPT,
    "proactive_briefing": PROACTIVE_BRIEFING_PROMPT,
    "script_detection": SCRIPT_DETECTION_PROMPT,
    "community_summary": COMMUNITY_SUMMARY_PROMPT,
}
