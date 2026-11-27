"""Ask AccordAI (feature F1): read-only questions about one company's books, behind AI_ASSIST (default off).

Modules:
    config       the AI_ASSIST flag, limits and the assistant's provider list (own model setting)
    facts        facts with per-answer ids (f1, f2, ...), app links and citations
    tools        the allowlisted read-only tools and their strict argument models
    runner       runs one tool inside a READ ONLY transaction (G1)
    prompts      the system prompt and the final-answer JSON schema
    refusals     questions refused before any model call
    fallback     the keyword router and the deterministic fact-table answer
    agent        the tool loop, number binding, audit row and response
    suggestions  starter questions per page

The guards added beside the extraction guards are in security/assistant_guard.py.
"""
