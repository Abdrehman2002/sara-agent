# Sara — Daewoo Express Voice Agent

LiveKit is a fast-evolving project. Always refer to the latest documentation before modifying any LiveKit code.

Run `lk docs --help` to see available commands.
Key commands: `lk docs overview`, `lk docs search`, `lk docs get-page`, `lk docs code-search`, `lk docs changelog`.
Run `lk docs <command> --help` before using a command for the first time.
Prefer browsing (`overview`, `get-page`) over search, and `search` over `code-search`.

LiveKit MCP server is also available at `https://docs.livekit.io/mcp`.
Key tools: `get_docs_overview`, `get_pages`, `docs_search`, `code_search`, `get_changelog`.

## Project

- **Agent**: Sara, customer care voice agent for Daewoo Express Pakistan
- **SDK**: livekit-agents Python ≥1.5.0
- **STT**: Deepgram Nova-3 (`language="multi"` for Urdu/English)
- **LLM**: OpenAI GPT-4o → GPT-4o-mini fallback
- **TTS**: ElevenLabs eleven_multilingual_v2 → OpenAI nova fallback
- **VAD**: Silero (prewarmed)
- **Turn detection**: MultilingualModel (livekit-plugins-turn-detector)
- **Agent name**: `"sara"` — required for explicit dispatch from the dashboard token API

## Key files

- `agent.py` — single entrypoint, all agent logic
- `requirements.txt` — all Python dependencies
- `Dockerfile` — builds the agent image
- `docker-compose.prod.yml` — full VPS stack (postgres + dashboard + agent)

## Rules

- Never modify LiveKit API calls without checking `lk docs` first — APIs change frequently
- `agent_name="sara"` in WorkerOptions must stay — dashboard dispatches Sara by name
- `DASHBOARD_URL` env var points to the Next.js dashboard — agent posts complaints and metrics there
- Do not add manager escalation or consent flows — intentionally removed
