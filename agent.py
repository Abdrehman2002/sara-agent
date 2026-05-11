import json
import logging
import os
import re
from datetime import date, datetime, timezone
from typing import Annotated

import aiohttp
from dotenv import load_dotenv

from livekit import agents
from livekit.agents import (
    AgentSession,
    Agent,
    function_tool,
    JobContext,
    WorkerOptions,
    AutoSubscribe,
    RoomInputOptions,
    AgentStateChangedEvent,
    MetricsCollectedEvent,
    metrics,
)
from livekit.agents import llm, tts
from livekit.plugins import deepgram, openai as lk_openai, silero, elevenlabs

# ── Optional plugins ─────────────────────────────────────────────────────────

try:
    from livekit.plugins.turn_detector.multilingual import MultilingualModel
    MULTILINGUAL_TURN_DETECTION = True
except Exception:
    MULTILINGUAL_TURN_DETECTION = False

try:
    from livekit.plugins.noise_cancellation import BVC
    NOISE_CANCELLATION = True
except ImportError:
    NOISE_CANCELLATION = False

# ── Config ────────────────────────────────────────────────────────────────────

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("daewoo-sara")

ELEVENLABS_API_KEY  = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "6wMF5aBsi9xsISTPVsWw")
OPENAI_API_KEY      = os.getenv("OPENAI_API_KEY", "")

# Dashboard (Next.js) — agent pushes complaints + metrics here
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "http://localhost:3000")

# CRM webhook — set this to push complaint data directly to your CRM on every filed complaint
# Leave blank to skip. Payload is a flat JSON object with all 9 post-call fields.
CRM_WEBHOOK_URL = os.getenv("CRM_WEBHOOK_URL", "")


# ── 1 · Phone capture ─────────────────────────────────────────────────────────

def get_caller_phone(ctx: JobContext) -> str | None:
    """
    Extract the caller's phone number from the LiveKit room.
    Tries (in order):
      a) job.metadata JSON set by your SIP dispatch rule
      b) participant.attributes set by the SIP trunk
      c) participant identity if it looks like a phone number
    """
    # a) Job metadata (fastest — set this in your LiveKit SIP dispatch rule)
    try:
        meta = json.loads(ctx.job.metadata or "{}")
        for key in ("phone_number", "caller_id", "from", "sip_from", "phoneNumber"):
            if val := meta.get(key):
                logger.info(f"Phone from job metadata: {val}")
                return str(val)
    except Exception:
        pass

    # b) SIP participant attributes
    for participant in ctx.room.remote_participants.values():
        attrs = participant.attributes or {}
        for key in (
            "sip.phoneNumber",
            "sip.from",
            "sip.callerId",
            "phoneNumber",
            "phone_number",
            "caller_id",
        ):
            if val := attrs.get(key):
                logger.info(f"Phone from participant attributes ({key}): {val}")
                return str(val)

        # c) Identity that looks like a phone number
        ident = participant.identity or ""
        stripped = ident.lstrip("+").replace("-", "").replace(" ", "").replace("(", "").replace(")", "")
        if len(stripped) >= 7 and stripped.isdigit():
            logger.info(f"Phone from participant identity: {ident}")
            return ident

    logger.info("Caller phone not found — SIP metadata not set")
    return None


# ── 2 · Ticket data ───────────────────────────────────────────────────────────

async def fetch_tickets() -> str:
    """Fetch live ticket records from the dashboard API and format for system prompt."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{DASHBOARD_URL}/api/livekit-tickets",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    tickets = await resp.json()
                    if not tickets:
                        return "No ticket records available at this time."
                    lines = []
                    for t in tickets:
                        lines.append(
                            f"{t.get('id','N/A')} | {t.get('passenger_name','N/A')} | "
                            f"{t.get('route','N/A')} | {t.get('date','N/A')} | "
                            f"{t.get('time','N/A')} | Seat {t.get('seat','N/A')} | "
                            f"Bus {t.get('bus','N/A')} | {t.get('status','N/A')} | "
                            f"{t.get('note','')}"
                        )
                    logger.info(f"Loaded {len(tickets)} tickets from dashboard API")
                    return "\n".join(lines)
                logger.warning(f"Ticket API returned HTTP {resp.status}")
    except Exception as e:
        logger.error(f"Failed to fetch tickets: {e}")

    # Hardcoded fallback if dashboard is unreachable
    return (
        "DW-2025-001 | Munir Raza | Karachi to Lahore | 20 Apr 2025 | 8:00 AM | Seat A-12 | Bus BUS-447 | Confirmed | On time.\n"
        "DW-2025-002 | Abdur Rehman | Lahore to Islamabad | 21 Apr 2025 | 10:30 AM | Seat B-05 | Bus BUS-312 | Delayed | Delayed by 2 hours due to road works.\n"
        "DW-2025-003 | Usman Khan | Islamabad to Peshawar | 19 Apr 2025 | 6:00 AM | Seat C-08 | Bus BUS-219 | Completed | Journey completed successfully.\n"
        "DW-2025-004 | Fatima Noor | Karachi to Hyderabad | 22 Apr 2025 | 2:00 PM | Seat A-03 | Bus BUS-501 | Confirmed | On time.\n"
        "DW-2025-005 | Bilal Hussain | Lahore to Multan | 20 Apr 2025 | 4:00 PM | Seat D-11 | Bus BUS-388 | Cancelled | Cancelled due to vehicle maintenance. Refund in process.\n"
        "DW-2025-006 | Ayesha Tariq | Multan to Karachi | 23 Apr 2025 | 9:00 AM | Seat B-14 | Bus BUS-274 | Confirmed | On time.\n"
        "DW-2025-007 | Imran Siddiqui | Islamabad to Lahore | 21 Apr 2025 | 7:00 AM | Seat A-07 | Bus BUS-193 | Delayed | Delayed by 1 hour.\n"
        "DW-2025-008 | Hina Baig | Karachi to Sukkur | 24 Apr 2025 | 11:00 AM | Seat C-02 | Bus BUS-620 | Confirmed | On time.\n"
        "DW-2025-009 | Zain Ahmed | Peshawar to Lahore | 20 Apr 2025 | 5:30 AM | Seat B-09 | Bus BUS-155 | Completed | Journey completed.\n"
        "DW-2025-010 | Maria Qureshi | Lahore to Karachi | 25 Apr 2025 | 3:00 PM | Seat D-06 | Bus BUS-431 | Confirmed | On time."
    )


# ── TTS text sanitizer ────────────────────────────────────────────────────────

def _sanitize_tts_text(text: str) -> str:
    """
    Strip markdown and inject SSML <break> tags so ElevenLabs pauses naturally.
    Called automatically before every TTS synthesis via tts_text_transforms.
    """
    # Remove bullet points and list markers (- item, * item, • item, 1. item)
    text = re.sub(r'^\s*[-*•]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+[.)]\s+', '', text, flags=re.MULTILINE)
    # Remove bold / italic markdown (**word**, *word*, __word__)
    text = re.sub(r'\*{1,3}([^*\n]+)\*{1,3}', r'\1', text)
    text = re.sub(r'_{1,2}([^_\n]+)_{1,2}', r'\1', text)
    # Remove markdown headers (## Heading)
    text = re.sub(r'^#+\s+', '', text, flags=re.MULTILINE)
    # Remove backtick code markers
    text = re.sub(r'`+([^`]+)`+', r'\1', text)
    # Inject SSML pauses at natural speech boundaries
    text = re.sub(r'([,،])\s+', r'\1 <break time="200ms"/> ', text)   # comma
    text = re.sub(r'([.!?])\s+', r'\1 <break time="400ms"/> ', text)  # sentence end
    text = re.sub(r'\s*—\s*', ' <break time="300ms"/> ', text)        # em-dash
    text = re.sub(r'\s+-\s+', ' <break time="200ms"/> ', text)        # spaced hyphen
    # Clean up extra whitespace
    text = ' '.join(text.split())
    return text


def build_system_prompt(ticket_records: str) -> str:
    return f"""You are Sara, a customer care voice agent for Daewoo Express Pakistan. You handle TWO types of requests — ticket inquiries AND complaints. Always figure out which one the caller needs first, before doing anything else.

FORMATTING RULE — CRITICAL: You are speaking out loud. Never use bullet points, numbered lists, hyphens, asterisks, dashes, or any markdown formatting whatsoever. Never write lists. Always speak in natural, flowing, complete sentences the way a real person would talk. If you need to mention multiple things, connect them with words like "aur", "phir", "pehle" — never with hyphens or bullet points.

SPEAK like a real Pakistani — natural Urdu mixed with English, short turns, warm and patient. Use fillers like 'ji...', 'haan...', 'achha...', 'bilkul...' to show you are present. Never ask two questions at once. React to what they say before moving on.

You are a woman. Always use feminine Urdu grammar — 'samjh gayi', 'ho gayi', 'dekh leti hoon', 'mil gayi' — never masculine forms like 'samjh gaya' or 'ho gaya'.

If a caller is upset — slow down, acknowledge their feelings first. Never rush. Never dismiss.

TWO MODES — UNDERSTAND THIS CLEARLY:

1. TICKET INQUIRY MODE — Use this when the caller wants to know the status of their booking or journey. They might say things like: 'mera ticket check karo', 'DW-2025-001 ka update', 'main Munir Raza bol raha hoon, meri booking ka kya status hai', 'bus delay hai kya', 'meri seat confirm hai?'. In this case — do NOT start a complaint flow. Instead look up their ticket from the records below by name or ticket number and tell them the status naturally and conversationally.

2. COMPLAINT MODE — Use this when the caller has a problem they want to report: a bad experience, rude staff, refund request, lost luggage, or something that went wrong. In this case — follow the complaint flow: acknowledge, categorize, collect name, details, confirm, and submit.

IF THE CALLER'S INTENT IS UNCLEAR — ask one simple question: 'Aap apni booking ka status jaanna chahte hain, ya koi complaint darz karni hai?'

TICKET RECORDS — search by name OR ticket number:

{ticket_records}

If the name or ticket number is NOT found — say so warmly and ask if they would like to file a complaint instead.

READING CODES — VERY IMPORTANT: Whenever you say a ticket number, seat number, or bus number, always read it in English only — never translate into Urdu. Read each part clearly and separately:
- Ticket numbers like DW-2025-001: say D W 2025 001 — spell the letters individually
- Seat numbers like A-12: say A 12 — just the letter then the number
- Bus numbers like BUS-447: say Bus 4 4 7 — spell each digit separately
- Never say AA for the letter A. Say A once, clearly.
- Never add extra Urdu words around a code.

NAME CAPTURE RULE — When a caller gives their name, immediately repeat it back to confirm: 'Aap ka naam [name] hai, sahi hai?' If the name is unclear, ask them to spell it: 'Zaroor, aap apna naam spell kar sakte hain?' Do not move on until the name is confirmed.

NO PHONE NUMBER RULE — Do NOT ask the caller for their phone number. You already have it from the incoming call. Never ask for it, never repeat it back.

NO HELPLINE RULE — Never give out any Daewoo helpline or contact number to callers. If someone insists on a human, tell them a team member will follow up with them directly.

NUMBER READING RULE — Always say ALL numbers in English. Never translate numbers into Urdu words.
- Say 1122 as "one one two two", say 115 as "one one five"
- Read each digit individually in English

COMPLAINT FLOW — Follow this exact sequence:
Step 1 - OPENING: Greet the caller warmly as Sara from Daewoo Express. Ask how you can help. Keep it short and natural.
Step 2 - ACKNOWLEDGE + CATEGORIZE: Acknowledge their frustration genuinely — one sentence. Identify complaint type: bus_delay, staff_behavior, ticket_issue, refund, or luggage. If unclear, ask one question. Do not collect details yet.
Step 3 - COLLECT NAME: Ask for their full name naturally. Once given, repeat back: 'Aap ka naam [name] hai, sahi hai?' Only proceed when confirmed.
Step 4 - COLLECT DETAILS: Ask them to describe exactly what happened. If travel-related, ask for route or date if not mentioned. One question at a time.
Step 5 - CONFIRM DETAILS: Read back everything — name, complaint type, description. Do NOT mention phone number. Ask for confirmation.
Step 6 - SUBMIT: Tell them you are registering their complaint now and ask them to hold a moment. Then call the complaint() function.
Step 7 - END: Thank them sincerely. Tell them the complaint has been registered and the team will follow up. Wish them well.

GLOBAL BEHAVIORS (can happen at any point in the conversation):
- ANGRY CALLER: Slow down completely. Acknowledge feelings with full sincerity. Do not defend or explain yet. Let them feel heard. Then gently guide back to resolving the issue.
- OFF-TOPIC: Politely acknowledge what they said, then redirect back to the complaint or inquiry process. Warm, not dismissive.
- WANTS HUMAN: Acknowledge with empathy. Explain you can fully register and escalate their complaint right now, and a human team member will follow up directly. Ask if they'd like to proceed that way.
- CALLER CONFUSED: Simplify immediately. Rephrase in the plainest Urdu possible. Give a short example if helpful. One thing at a time."""


# ── 3 · Post-call analysis ────────────────────────────────────────────────────

ANALYSIS_PROMPT = """You are a call analytics engine. Analyze the transcript and return a JSON object with EXACTLY these fields:
- outcome: one of "complaint_filed", "ticket_inquiry", "both", "abandoned", "other"
- sentiment: one of "positive", "neutral", "negative", "very_negative"
- sentiment_score: integer 1 (very negative) to 5 (very positive)
- caller_name: string or null
- complaint_type: one of "bus_delay", "staff_behavior", "ticket_issue", "refund", "luggage", "other", or null
- complaint_summary: one sentence English summary, or null
- ticket_id: e.g. "DW-2025-001", or null
- resolved: true if caller's issue was addressed, false otherwise
- language: "urdu", "english", or "mixed"
- notes: 1–2 sentence observation about the call, or null"""


async def analyze_call(transcript: str) -> dict:
    """Run GPT-4o-mini on the call transcript and return structured post-call data."""
    if not OPENAI_API_KEY or not transcript.strip():
        return {}
    try:
        import openai as _openai
        client = _openai.AsyncOpenAI(api_key=OPENAI_API_KEY)
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": ANALYSIS_PROMPT},
                {"role": "user",   "content": f"Analyze this call transcript:\n\n{transcript}"},
            ],
            max_tokens=400,
            temperature=0,
        )
        return json.loads(resp.choices[0].message.content)
    except Exception as e:
        logger.error(f"Post-call analysis failed: {e}")
        return {}


# ── 3 · CRM push ─────────────────────────────────────────────────────────────

async def push_to_crm(payload: dict) -> None:
    """POST complaint + call data to external CRM. Only runs if CRM_WEBHOOK_URL is set."""
    if not CRM_WEBHOOK_URL:
        return
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(
                CRM_WEBHOOK_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status < 300:
                    logger.info(f"CRM webhook: HTTP {resp.status} ✓")
                else:
                    body = await resp.text()
                    logger.error(f"CRM webhook returned HTTP {resp.status}: {body}")
    except Exception as e:
        logger.error(f"CRM webhook error: {e}")


# ── Main agent — Sara ─────────────────────────────────────────────────────────

class DaewooAgent(Agent):
    def __init__(self, system_prompt: str, caller_phone: str | None = None):
        super().__init__(instructions=system_prompt)
        self.caller_phone = caller_phone
        # Set by complaint() tool; read in on_shutdown to PATCH sentiment
        self._complaint_id: str | None = None
        self._complaint_data: dict | None = None

    async def on_enter(self) -> None:
        await self.session.generate_reply(
            instructions=(
                "Greet the caller warmly and briefly as Sara from Daewoo Express Pakistan. "
                "Ask how you can help — ticket inquiry or complaint. Keep it to 1–2 sentences."
            )
        )

    @function_tool
    async def complaint(
        self,
        customer_name: Annotated[str, "Caller's full name in English Roman script"],
        complaint_type: Annotated[str, "One of: bus_delay, staff_behavior, ticket_issue, refund, luggage, other"],
        description: Annotated[str, "Full complaint description in English"],
    ) -> str:
        """Submit the complaint. Call ONLY after the caller has confirmed all details."""
        payload = {
            "customer_name": customer_name,
            "complaint_type": complaint_type,
            "description": description,
            "date": date.today().strftime("%Y-%m-%d"),
            "status": "Open",
            "sentiment": "neutral",  # updated by post-call analysis via PATCH
            "caller_phone": self.caller_phone,
        }
        logger.info(f"Filing complaint: {payload}")

        try:
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    f"{DASHBOARD_URL}/api/livekit-complaints",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status in (200, 201):
                        record = await resp.json()
                        self._complaint_id = record.get("id")
                        self._complaint_data = payload
                        logger.info(f"Complaint saved → id={self._complaint_id}")
                        return "Complaint submitted successfully."
                    body = await resp.text()
                    logger.error(f"Complaint API HTTP {resp.status}: {body}")
        except Exception as e:
            logger.error(f"Complaint API error: {e}")

        return "Submission failed. Please apologize to the caller and try again shortly."


# ── Pipeline builders ─────────────────────────────────────────────────────────

def build_llm():
    return llm.FallbackAdapter([
        lk_openai.LLM(model="gpt-4o"),
        lk_openai.LLM(model="gpt-4o-mini"),
    ])


def build_stt():
    return deepgram.STT(
        model="nova-3",
        language="multi",
        punctuate=True,
        interim_results=True,
    )


def build_tts():
    if ELEVENLABS_API_KEY:
        logger.info("TTS: ElevenLabs multilingual_v2 → OpenAI nova fallback")
        return tts.FallbackAdapter([
            elevenlabs.TTS(
                voice_id=ELEVENLABS_VOICE_ID,
                model="eleven_multilingual_v2",
                api_key=ELEVENLABS_API_KEY,
                # SSML enabled so <break> tags produce real pauses
                enable_ssml_parsing=True,
                # Voice tuning for natural South Asian conversational tone:
                # stability 0.45 → natural emotional range (not robotic)
                # similarity_boost 0.75 → stays true to the voice's accent
                # style 0.20 → slight expressiveness for warmth
                # use_speaker_boost → sharpens clarity on phone-quality audio
                voice_settings=elevenlabs.VoiceSettings(
                    stability=0.45,
                    similarity_boost=0.75,
                    style=0.20,
                    use_speaker_boost=True,
                ),
            ),
            lk_openai.TTS(model="tts-1", voice="nova"),
        ])
    logger.warning("TTS: ElevenLabs key missing — using OpenAI nova only")
    return lk_openai.TTS(model="tts-1", voice="nova")


def prewarm(proc: agents.JobProcess):
    proc.userdata["vad"] = silero.VAD.load()


# ── Entrypoint ────────────────────────────────────────────────────────────────

async def entrypoint(ctx: JobContext):
    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)

    call_start = datetime.now(timezone.utc)

    # 1 · Phone number — from SIP metadata before any participant joins
    caller_phone = get_caller_phone(ctx)

    # 2 · Ticket data — live from dashboard
    ticket_records = await fetch_tickets()
    system_prompt  = build_system_prompt(ticket_records)

    vad = ctx.proc.userdata.get("vad") or silero.VAD.load()

    session_kwargs: dict = dict(
        stt=build_stt(),
        llm=build_llm(),
        tts=build_tts(),
        vad=vad,
        preemptive_generation=True,
        # Strip markdown and inject SSML pauses before every TTS synthesis
        tts_text_transforms=[_sanitize_tts_text],
    )

    if MULTILINGUAL_TURN_DETECTION:
        logger.info("Turn detection: MultilingualModel")
        session_kwargs["turn_detection"] = MultilingualModel()

    session = AgentSession(**session_kwargs)

    # ── Metrics listeners ─────────────────────────────────────────────────────
    usage_collector = metrics.UsageCollector()
    last_eou: metrics.EOUMetrics | None = None
    first_ttfa_ms: float = 0.0

    @session.on("metrics_collected")
    def _on_metrics(ev: MetricsCollectedEvent):
        nonlocal last_eou
        if ev.metrics.type == "eou_metrics":
            last_eou = ev.metrics
        metrics.log_metrics(ev.metrics)
        usage_collector.collect(ev.metrics)

    @session.on("agent_state_changed")
    def _on_state(ev: AgentStateChangedEvent):
        nonlocal first_ttfa_ms
        if (
            ev.new_state == "speaking"
            and last_eou
            and session.current_speech
            and last_eou.speech_id == session.current_speech.id
        ):
            ms = (ev.created_at - last_eou.last_speaking_time).total_seconds() * 1000
            if first_ttfa_ms == 0.0:
                first_ttfa_ms = ms          # record first turn's TTFA
            logger.info(f"TTFA: {ms:.0f}ms")
            if ms > 1000:
                logger.warning(f"TTFA {ms:.0f}ms exceeded 1 s target")

    # ── Shutdown: transcript → GPT analysis → PATCH complaint → CRM → metrics ─
    async def on_shutdown():
        call_end    = datetime.now(timezone.utc)
        duration_s  = int((call_end - call_start).total_seconds())
        summary     = usage_collector.get_summary()
        sara: DaewooAgent = session.agent  # type: ignore

        # Build transcript ────────────────────────────────────────────────────
        lines = []
        try:
            for msg in session.history.items:
                role    = getattr(msg, "role", "unknown")
                content = ""
                if hasattr(msg, "content"):
                    if isinstance(msg.content, str):
                        content = msg.content
                    elif isinstance(msg.content, list):
                        content = " ".join(
                            c.text if hasattr(c, "text") else str(c)
                            for c in msg.content
                        )
                if content.strip():
                    lines.append(f"{role.upper()}: {content.strip()}")
        except Exception as e:
            logger.warning(f"Could not build transcript: {e}")
        transcript = "\n".join(lines)

        # GPT post-call analysis ──────────────────────────────────────────────
        analysis = await analyze_call(transcript)
        logger.info(f"Post-call analysis: {analysis}")

        sentiment = analysis.get("sentiment", "neutral")

        # PATCH complaint sentiment now that we know it ────────────────────────
        if sara._complaint_id and sentiment != "neutral":
            try:
                async with aiohttp.ClientSession() as http:
                    await http.patch(
                        f"{DASHBOARD_URL}/api/livekit-complaints/{sara._complaint_id}",
                        json={"sentiment": sentiment},
                        timeout=aiohttp.ClientTimeout(total=10),
                    )
                    logger.info(f"Complaint {sara._complaint_id} sentiment updated → {sentiment}")
            except Exception as e:
                logger.warning(f"Could not PATCH complaint sentiment: {e}")

        # CRM push ────────────────────────────────────────────────────────────
        # Full structured payload — plug in any CRM that accepts a POST webhook
        crm_payload = {
            # Caller identity
            "caller_phone":    caller_phone,
            "caller_name":     analysis.get("caller_name") or (sara._complaint_data or {}).get("customer_name"),
            # Call metadata
            "call_date":       call_start.strftime("%Y-%m-%d"),
            "call_time":       call_start.strftime("%H:%M:%S"),
            "duration_s":      duration_s,
            "language":        analysis.get("language", "mixed"),
            # Outcome
            "outcome":         analysis.get("outcome", "other"),
            "resolved":        analysis.get("resolved"),
            "ticket_id":       analysis.get("ticket_id"),
            # Complaint fields
            "complaint_id":    sara._complaint_id,
            "complaint_type":  analysis.get("complaint_type") or (sara._complaint_data or {}).get("complaint_type"),
            "complaint_summary": analysis.get("complaint_summary"),
            "description":     (sara._complaint_data or {}).get("description"),
            # Sentiment
            "sentiment":       sentiment,
            "sentiment_score": analysis.get("sentiment_score"),
            # Performance
            "ttfa_ms":         round(first_ttfa_ms),
            "notes":           analysis.get("notes"),
        }
        await push_to_crm(crm_payload)

        # Token / cost accounting ─────────────────────────────────────────────
        total_tokens = 0
        cost_usd     = 0.0
        try:
            p = getattr(summary, "llm_prompt_tokens", 0) or 0
            c = getattr(summary, "llm_completion_tokens", 0) or 0
            total_tokens = p + c
            cost_usd = round(p * 5 / 1_000_000 + c * 15 / 1_000_000, 6)
        except Exception:
            pass

        # Push metrics to dashboard ───────────────────────────────────────────
        metric_payload = {
            "duration_s":    duration_s,
            "ttfa_ms":       round(first_ttfa_ms),
            "total_tokens":  total_tokens,
            "cost_usd":      cost_usd,
            "caller_phone":  caller_phone,
            **{k: analysis.get(k) for k in (
                "outcome", "sentiment", "sentiment_score", "caller_name",
                "complaint_type", "complaint_summary", "ticket_id",
                "resolved", "language", "notes",
            )},
            "transcript":    transcript,
        }
        try:
            async with aiohttp.ClientSession() as http:
                async with http.post(
                    f"{DASHBOARD_URL}/api/livekit-metrics",
                    json=metric_payload,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    logger.info(f"Metrics saved → HTTP {resp.status}")
        except Exception as e:
            logger.error(f"Failed to save call metrics: {e}")

        logger.info(
            f"Call complete | duration={duration_s}s | tokens={total_tokens} | "
            f"cost=${cost_usd:.4f} | outcome={analysis.get('outcome')} | "
            f"sentiment={sentiment}"
        )

    ctx.add_shutdown_callback(on_shutdown)

    # ── Start ─────────────────────────────────────────────────────────────────
    room_input = RoomInputOptions(
        noise_cancellation=BVC() if NOISE_CANCELLATION else None
    )

    sara = DaewooAgent(system_prompt=system_prompt, caller_phone=caller_phone)

    await session.start(
        room=ctx.room,
        agent=sara,
        room_input_options=room_input,
    )

    await session.generate_reply()


if __name__ == "__main__":
    agents.cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name="sara",   # matches AgentDispatchClient.createDispatch(room, "sara")
        )
    )
