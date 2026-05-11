"""
Quick API connectivity test — runs outside LiveKit, tests each provider independently.
Usage: python test_apis.py
"""
import asyncio, os, sys
from dotenv import load_dotenv

load_dotenv("/opt/vextria/.env.prod")   # VPS path; override with --env if needed

OPENAI_KEY    = os.getenv("OPENAI_API_KEY", "")
DEEPGRAM_KEY  = os.getenv("DEEPGRAM_API_KEY", "")
ELEVEN_KEY    = os.getenv("ELEVENLABS_API_KEY", "")
ELEVEN_VOICE  = os.getenv("ELEVENLABS_VOICE_ID", "")

OK   = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"

# ── 1. OpenAI ─────────────────────────────────────────────────────────────────
async def test_openai():
    print("\n── OpenAI (gpt-4o-mini) ──────────────────────────────────────")
    try:
        import openai
        client = openai.AsyncOpenAI(api_key=OPENAI_KEY)
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "Reply with just: OK"}],
            max_tokens=5,
        )
        reply = resp.choices[0].message.content.strip()
        print(f"{OK} OpenAI responded: {reply!r}")
        return True
    except Exception as e:
        print(f"{FAIL} OpenAI error: {e}")
        return False

# ── 2. Deepgram ───────────────────────────────────────────────────────────────
async def test_deepgram():
    print("\n── Deepgram (nova-3) ────────────────────────────────────────")
    try:
        import httpx
        # Use a tiny public wav to test transcription
        url = "https://storage.googleapis.com/bucket-999-12345/hello.wav"
        headers = {"Authorization": f"Token {DEEPGRAM_KEY}"}
        params  = {"model": "nova-3", "language": "ur"}
        # Just test auth — hit the usage endpoint instead (no audio needed)
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get("https://api.deepgram.com/v1/projects", headers=headers)
        if r.status_code == 200:
            print(f"{OK} Deepgram auth OK — projects accessible")
            return True
        else:
            print(f"{FAIL} Deepgram returned {r.status_code}: {r.text[:200]}")
            return False
    except Exception as e:
        print(f"{FAIL} Deepgram error: {e}")
        return False

# ── 3. ElevenLabs ─────────────────────────────────────────────────────────────
async def test_elevenlabs():
    print("\n── ElevenLabs (eleven_multilingual_v2) ──────────────────────")
    try:
        import httpx
        headers = {"xi-api-key": ELEVEN_KEY, "Content-Type": "application/json"}
        payload = {
            "text": "Hello, this is a test.",
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        }
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}",
                headers=headers,
                json=payload,
            )
        if r.status_code == 200 and len(r.content) > 1000:
            print(f"{OK} ElevenLabs returned {len(r.content)} bytes of audio")
            return True
        else:
            print(f"{FAIL} ElevenLabs returned {r.status_code}: {r.text[:300]}")
            return False
    except Exception as e:
        print(f"{FAIL} ElevenLabs error: {e}")
        return False

# ── Main ──────────────────────────────────────────────────────────────────────
async def main():
    print("=" * 60)
    print("Sara API connectivity test")
    print("=" * 60)

    if not OPENAI_KEY:   print(f"{FAIL} OPENAI_API_KEY not set")
    if not DEEPGRAM_KEY: print(f"{FAIL} DEEPGRAM_API_KEY not set")
    if not ELEVEN_KEY:   print(f"{FAIL} ELEVENLABS_API_KEY not set")

    r1 = await test_openai()
    r2 = await test_deepgram()
    r3 = await test_elevenlabs()

    print("\n" + "=" * 60)
    print(f"OpenAI:     {'PASS' if r1 else 'FAIL'}")
    print(f"Deepgram:   {'PASS' if r2 else 'FAIL'}")
    print(f"ElevenLabs: {'PASS' if r3 else 'FAIL'}")
    print("=" * 60)

asyncio.run(main())
