"""hunter/gatherer: a research-grounded AI agent harness.

Submodules:
    config    – settings + model registry
    models    – LLM clients (cloud, local) and the cost-aware router
    memory    – NAS-backed memory (SQLite source-of-truth + Qdrant vectors),
                Park-scored recall, Mem0-style consolidation, sleep-time reflection
    tools     – tool/skill protocol + RAG-over-tools selector
    skills    – SKILL.md discovery
    agents    – tool-use agent loop + orchestrator-worker sub-agents
    voice     – Silero VAD + faster-whisper STT + pluggable TTS
    loop      – AgentLoop wiring everything together
    cost      – token + dollar accounting (first-class metric)
    cli       – REPL and subcommands
"""

__version__ = "0.1.0a2"
