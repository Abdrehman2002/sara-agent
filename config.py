from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # LiveKit
    livekit_url: str
    livekit_api_key: str
    livekit_api_secret: str

    # STT
    deepgram_api_key: str

    # LLM / TTS
    openai_api_key: str

    # CRM — n8n webhook (swap for real CRM base URL later)
    crm_webhook_url: str = "https://vextria.app.n8n.cloud/webhook/d0c09586-a642-4fbc-a983-f10c7f7cc695"
    crm_api_key: str = ""  # set when you have a real CRM

    # Optional
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""


settings = Settings()
