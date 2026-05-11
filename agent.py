import json
import logging
import os
import re
from datetime import date, datetime, timezone
from typing import Annotated

import aiohttp
from dotenv import load_dotenv

from livekit import agents, api as lk_server_api
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
try:
    from livekit.agents.voice import TurnHandlingOptions
    HAS_TURN_HANDLING = True
except ImportError:
    HAS_TURN_HANDLING = False
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

# ── Recording / Egress ────────────────────────────────────────────────────────
# LiveKit Egress writes the audio file to S3-compatible storage (AWS S3, Cloudflare R2, etc.)
# Leave RECORDING_S3_BUCKET empty to disable recordings.
#
# Cloudflare R2 example:
#   RECORDING_S3_BUCKET    = my-recordings
#   RECORDING_S3_REGION    = auto
#   RECORDING_S3_KEY       = <R2 Access Key ID>
#   RECORDING_S3_SECRET    = <R2 Secret Access Key>
#   RECORDING_S3_ENDPOINT  = https://<account_id>.r2.cloudflarestorage.com
#   RECORDING_PUBLIC_BASE  = https://recordings.yourdomain.com   (or R2 public bucket URL)

LIVEKIT_HTTP_URL       = os.getenv("LIVEKIT_URL", "").replace("wss://", "https://").replace("ws://", "http://")
LIVEKIT_API_KEY        = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET     = os.getenv("LIVEKIT_API_SECRET", "")

RECORDING_S3_BUCKET    = os.getenv("RECORDING_S3_BUCKET", "")
RECORDING_S3_REGION    = os.getenv("RECORDING_S3_REGION", "auto")
RECORDING_S3_KEY       = os.getenv("RECORDING_S3_KEY", "")
RECORDING_S3_SECRET    = os.getenv("RECORDING_S3_SECRET", "")
RECORDING_S3_ENDPOINT  = os.getenv("RECORDING_S3_ENDPOINT", "")   # e.g. https://xxxx.r2.cloudflarestorage.com
RECORDING_PUBLIC_BASE  = os.getenv("RECORDING_PUBLIC_BASE", "")   # public base URL (no trailing slash)


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


# ── 2 · Call recording helpers ───────────────────────────────────────────────

async def start_recording(room_name: str, filepath: str) -> str | None:
    """
    Start a LiveKit Room Composite Egress (audio-only OGG) for the given room.
    Returns the egress_id to pass to stop_recording(), or None if not configured.
    """
    if not RECORDING_S3_BUCKET:
        logger.info("Recording: RECORDING_S3_BUCKET not set — skipping")
        return None
    try:
        lkapi = lk_server_api.LiveKitAPI(
            url=LIVEKIT_HTTP_URL,
            api_key=LIVEKIT_API_KEY,
            api_secret=LIVEKIT_API_SECRET,
        )
        s3 = lk_server_api.S3Upload(
            access_key=RECORDING_S3_KEY,
            secret=RECORDING_S3_SECRET,
            bucket=RECORDING_S3_BUCKET,
            region=RECORDING_S3_REGION,
            endpoint=RECORDING_S3_ENDPOINT,  # empty string = AWS; set for R2/B2/Minio
        )
        info = await lkapi.egress.start_room_composite_egress(
            lk_server_api.StartRoomCompositeEgressRequest(
                room_name=room_name,
                audio_only=True,
                file_outputs=[lk_server_api.EncodedFileOutput(
                    file_type=lk_server_api.EncodedFileType.OGG,
                    filepath=filepath,
                    s3=s3,
                )],
            )
        )
        await lkapi.aclose()
        logger.info(f"Recording started → egress_id={info.egress_id}  file={filepath}")
        return info.egress_id
    except Exception as e:
        logger.error(f"start_recording failed: {e}")
        return None


async def stop_recording(egress_id: str) -> None:
    """Stop a running egress. Call this in on_shutdown before pushing metrics."""
    try:
        lkapi = lk_server_api.LiveKitAPI(
            url=LIVEKIT_HTTP_URL,
            api_key=LIVEKIT_API_KEY,
            api_secret=LIVEKIT_API_SECRET,
        )
        await lkapi.egress.stop_egress(
            lk_server_api.StopEgressRequest(egress_id=egress_id)
        )
        await lkapi.aclose()
        logger.info(f"Recording stopped → egress_id={egress_id}")
    except Exception as e:
        logger.error(f"stop_recording failed: {e}")


# ── 3 · Ticket data ───────────────────────────────────────────────────────────

async def fetch_tickets() -> str:
    """Fetch live ticket records from the dashboard API and format for system prompt."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{DASHBOARD_URL}/api/livekit-tickets",
                timeout=aiohttp.ClientTimeout(total=3),  # 3s max — fall back to hardcoded if Vercel is slow
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


# ── Pronunciation fix: Roman Urdu → Urdu script before ElevenLabs ────────────
# ElevenLabs mispronounces Roman Urdu words as English.
# This map converts the most common ones to proper Urdu script.
# Add any new mispronounced word here as: (r'\bword\b', 'اردو')

_PRONUNCIATION_MAP = [
    (r'\bsunn\b',       'سنیں'),
    (r'\bsuno\b',       'سنو'),
    (r'\bji\b',         'جی'),
    (r'\bhaan\b',       'ہاں'),
    (r'\bhan\b',        'ہاں'),
    (r'\bacha\b',       'اچھا'),
    (r'\bachha\b',      'اچھا'),
    (r'\bbilkul\b',     'بالکل'),
    (r'\bshukriya\b',   'شکریہ'),
    (r'\bshukria\b',    'شکریہ'),
    (r'\btheek\b',      'ٹھیک'),
    (r'\bnahi\b',       'نہیں'),
    (r'\bnahin\b',      'نہیں'),
    (r'\bzaroor\b',     'ضرور'),
    (r'\bkripya\b',     'کرپیا'),
    (r'\bforan\b',      'فوراً'),
    (r'\babhi\b',       'ابھی'),
    (r'\bphir\b',       'پھر'),
]

async def _fix_pronunciation(text_stream):
    """
    Async generator — correct form for tts_text_transforms in livekit-agents 1.5+.
    Replaces Roman Urdu words with Urdu script so ElevenLabs pronounces them right.
    """
    async for chunk in text_stream:
        fixed = chunk
        for pattern, replacement in _PRONUNCIATION_MAP:
            fixed = re.sub(pattern, replacement, fixed, flags=re.IGNORECASE)
        yield fixed


def build_system_prompt(ticket_records: str) -> str:
    return f"""You are Sara, a customer care voice agent for Daewoo Express Pakistan. You handle TWO types of requests — ticket inquiries AND complaints. Always figure out which one the caller needs first, before doing anything else.

FORMATTING RULE — CRITICAL: You are speaking out loud. Never use bullet points, numbered lists, hyphens, asterisks, dashes, or any markdown formatting whatsoever. Never write lists. Always speak in natural, flowing, complete sentences the way a real person would talk. If you need to mention multiple things, connect them with words like "aur", "phir", "pehle" — never with hyphens or bullet points.

LANGUAGE STYLE — CRITICAL: Speak in simple, everyday Urdu script. Not formal or heavy Urdu — simple conversational Urdu that anyone can understand. Write ALL Urdu words in Urdu script. The only words you may keep in English are technical terms that have no Urdu equivalent: ticket, booking, complaint, bus, route, seat, status, refund, delay, confirm, cancel, register. All other words must be in Urdu script.

EXAMPLES OF HOW YOU SHOULD SOUND:
- "جی، آپ کی booking confirm ہے۔ bus on time ہے۔"
- "اچھا، تو آپ complaint register کرنا چاہتے ہیں؟"
- "بالکل، میں ابھی آپ کی مدد کرتی ہوں۔"
- "آپ کا نام کیا ہے؟"
- "ٹھیک ہے، میں نے سمجھ لیا۔"

Keep sentences short and simple. Warm and natural, never stiff or formal.

Use natural fillers like "جی...", "okay so...", "acha...", "right...", "ہاں bilkul..." to show you are present. Never ask two questions at once. React to what they say before moving on.

GENDER RULE — CRITICAL: You are Sara (a woman), but you do NOT know the gender of the caller. Never assume the caller is male or female. Always use gender-neutral language when addressing them. Say "aap ne" not "aap ne kaha tha" with gendered assumptions. Avoid verb endings that assume caller gender. Use "aap" always — never "bhai", "behen", "sahib", "madam". If you must use a verb form referring to the caller, use the neutral/formal form.

If a caller is upset — slow down, acknowledge their feelings first. Never rush. Never dismiss.

TWO MODES — UNDERSTAND THIS CLEARLY:

1. TICKET INQUIRY MODE — Use this when the caller wants to know the status of their booking or journey. They might say things like: 'mera ticket check karo', 'DW-2025-001 ka update', 'main Munir Raza bol raha hoon, meri booking ka kya status hai', 'bus delay hai kya', 'meri seat confirm hai?'. In this case — do NOT start a complaint flow. Instead look up their ticket from the records below by name or ticket number and tell them the status naturally and conversationally.

2. COMPLAINT MODE — Use this when the caller has a problem they want to report: a bad experience, rude staff, refund request, lost luggage, or something that went wrong. In this case — follow the complaint flow: acknowledge, categorize, collect name, details, confirm, and submit.

IF THE CALLER'S INTENT IS UNCLEAR — ask one simple question: 'آپ اپنی booking check کرنا چاہتے ہیں، یا کوئی complaint درج کرنی ہے؟'

TICKET RECORDS — search by name OR ticket number:

{ticket_records}

If the name or ticket number is NOT found — say so warmly and ask if they would like to file a complaint instead.

READING CODES — VERY IMPORTANT: Whenever you say a ticket number, seat number, or bus number, always read it in English only — never translate into Urdu. Read each part clearly and separately:
- Ticket numbers like DW-2025-001: say D W 2025 001 — spell the letters individually
- Seat numbers like A-12: say A 12 — just the letter then the number
- Bus numbers like BUS-447: say Bus 4 4 7 — spell each digit separately
- Never say AA for the letter A. Say A once, clearly.
- Never add extra Urdu words around a code.

NAME CAPTURE RULE — When a caller gives ANY name (even just a first name), accept it immediately and move on. Do NOT insist on a full name. Do NOT keep asking for more. Just say 'Shukriya [name] sahab/ji' and continue. If the name is completely inaudible or unclear, ask once: 'Zaroor, aap apna naam bata sakte hain?' but never ask more than once. A single name, a nickname, anything they give — accept it and proceed.

NO PHONE NUMBER RULE — Do NOT ask the caller for their phone number. You already have it from the incoming call. Never ask for it, never repeat it back.

NO HELPLINE RULE — Never give out any Daewoo helpline or contact number to callers. If someone insists on a human, tell them a team member will follow up with them directly.

NUMBER READING RULE — Always say ALL numbers in English. Never translate numbers into Urdu words.
- Say 1122 as "one one two two", say 115 as "one one five"
- Read each digit individually in English

COMPLAINT FLOW — Follow this exact sequence:
Step 1 - OPENING: Greet the caller warmly as Sara from Daewoo Express. Ask how you can help. Keep it short and natural.
Step 2 - ACKNOWLEDGE + CATEGORIZE: Acknowledge their frustration genuinely — one sentence. Identify complaint type: bus_delay, staff_behavior, ticket_issue, refund, or luggage. If unclear, ask one question. Do not collect details yet.
Step 3 - COLLECT NAME: Ask for their name naturally. Accept whatever they give — first name, full name, anything. Do NOT ask for a full name. Do NOT wait for confirmation. Just acknowledge it and move on immediately to Step 4.
Step 4 - COLLECT DETAILS: Ask them to describe exactly what happened. If travel-related, ask for route or date if not mentioned. One question at a time.
Step 5 - CONFIRM DETAILS: Read back everything — name, complaint type, description. Do NOT mention phone number. Ask for confirmation.
Step 6 - SUBMIT: Tell them you are registering their complaint now and ask them to hold a moment. Then call the complaint() function.
Step 7 - END: Thank them sincerely. Tell them the complaint has been registered and the team will follow up. Wish them well.

GLOBAL BEHAVIORS (can happen at any point in the conversation):
- ANGRY CALLER: Slow down completely. Acknowledge feelings with full sincerity. Do not defend or explain yet. Let them feel heard. Then gently guide back to resolving the issue.
- OFF-TOPIC: Politely acknowledge what they said, then redirect back to the complaint or inquiry process. Warm, not dismissive.
- WANTS HUMAN: Acknowledge with empathy. Explain you can fully register and escalate their complaint right now, and a human team member will follow up directly. Ask if they'd like to proceed that way.
- CALLER CONFUSED: Simplify immediately. Rephrase in the plainest Urdu possible. Give a short example if helpful. One thing at a time."""


# ── 4 · Post-call analysis ────────────────────────────────────────────────────

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


# ── 5 · CRM push ─────────────────────────────────────────────────────────────

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
        # Use say() instead of generate_reply() — speaks instantly without LLM round trip
        await self.session.say(
            "السلام علیکم! میں سارہ ہوں، Daewoo Express کی طرف سے۔ "
            "آپ کی کیا مدد کر سکتی ہوں — booking check کرنی ہے یا کوئی complaint؟"
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
        lk_openai.LLM(model="gpt-4o"),       # Best quality for natural Urdu conversation
        lk_openai.LLM(model="gpt-4o-mini"),  # Fallback
    ])


def build_stt():
    return deepgram.STT(
        model="nova-3",
        language="ur",          # Urdu only — faster than multi-language detection
        punctuate=True,
        interim_results=True,
        endpointing=200,        # ms of silence before Deepgram finalises — default 500ms
    )


def build_tts():
    if ELEVENLABS_API_KEY:
        logger.info("TTS: ElevenLabs multilingual_v2 → turbo_v2_5 → OpenAI nova")
        return tts.FallbackAdapter([
            # multilingual_v2: highest quality, best Urdu pronunciation
            elevenlabs.TTS(
                voice_id=ELEVENLABS_VOICE_ID,
                model="eleven_multilingual_v2",
                api_key=ELEVENLABS_API_KEY,
                enable_ssml_parsing=True,
                voice_settings=elevenlabs.VoiceSettings(
                    stability=0.50,
                    similarity_boost=0.80,
                    style=0.25,
                    use_speaker_boost=True,
                ),
            ),
            # turbo_v2_5 as fallback (~300ms TTFB)
            elevenlabs.TTS(
                voice_id=ELEVENLABS_VOICE_ID,
                model="eleven_turbo_v2_5",
                api_key=ELEVENLABS_API_KEY,
                enable_ssml_parsing=True,
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
    proc.userdata["vad"] = silero.VAD.load(
        min_silence_duration=0.2,    # 200ms — snappier without cutting off speech
        activation_threshold=0.25,   # lower = more sensitive (good for WebRTC)
    )


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
        tts_text_transforms=[_fix_pronunciation],
    )

    # Faster turn detection — respond sooner after user stops speaking
    if HAS_TURN_HANDLING:
        session_kwargs["turn_handling"] = TurnHandlingOptions(
            min_delay=0.1,   # near-instant response after speech ends
            max_delay=1.5,   # cap wait at 1.5s
        )

    if MULTILINGUAL_TURN_DETECTION:
        logger.info("Turn detection: MultilingualModel")
        session_kwargs["turn_detection"] = MultilingualModel()

    session = AgentSession(**session_kwargs)

    # ── Recording state (set after session.start; read in on_shutdown) ──────────
    recording_egress_id: str | None = None
    recording_filepath:  str | None = None

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

    # ── Shutdown: stop recording → transcript → GPT analysis → PATCH → CRM → metrics ─
    async def on_shutdown():
        nonlocal recording_egress_id, recording_filepath
        call_end    = datetime.now(timezone.utc)
        duration_s  = int((call_end - call_start).total_seconds())
        summary     = usage_collector.get_summary()
        # sara is captured from the entrypoint closure — session.agent doesn't exist in 1.5.x

        # Stop egress recording ────────────────────────────────────────────────
        recording_url: str | None = None
        if recording_egress_id:
            await stop_recording(recording_egress_id)
            # Give LiveKit ~2s to finalize the file before we push metrics
            import asyncio as _asyncio
            await _asyncio.sleep(2)
            if RECORDING_PUBLIC_BASE and recording_filepath:
                recording_url = f"{RECORDING_PUBLIC_BASE.rstrip('/')}/{recording_filepath}"
                logger.info(f"Recording URL: {recording_url}")

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
            "recording_url": recording_url,   # None if egress not configured
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
    # BVC noise cancellation is designed for SIP/telephony — it over-filters WebRTC
    # mic audio and causes Sara to hear silence. Disabled for web call compatibility.
    room_input = RoomInputOptions(
        noise_cancellation=None
    )

    sara = DaewooAgent(system_prompt=system_prompt, caller_phone=caller_phone)

    await session.start(
        room=ctx.room,
        agent=sara,
        room_input_options=room_input,
    )
    # on_enter() handles the greeting via say() — no second generate_reply() needed

    # ── Start call recording via LiveKit Egress ───────────────────────────────
    # Filename: daewoo/YYYYMMDD-HHMMSS-<room8>.ogg (stored in your S3/R2 bucket)
    if RECORDING_S3_BUCKET:
        ts = call_start.strftime("%Y%m%d-%H%M%S")
        room_slug = ctx.room.name[:8].replace("/", "-")
        recording_filepath = f"daewoo/{ts}-{room_slug}.ogg"
        recording_egress_id = await start_recording(ctx.room.name, recording_filepath)


if __name__ == "__main__":
    agents.cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            # No agent_name — auto-dispatch mode: Sara picks up any new room automatically
        )
    )
