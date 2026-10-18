from datetime import time
from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Core
    database_url: str = "sqlite:///./local.db"
    vapi_secret: str = Field(default="", description="Shared secret Vapi sends in X-Vapi-Secret")
    admin_password: str = "change-me"
    jobs_token: str = Field(default="", description="Bearer token for /jobs/* (cron)")

    # Clinic profile (the agent only ever books inside these rules)
    clinic_name: str = "BrightSmile Dental"
    clinic_timezone: str = "America/New_York"
    clinic_phone: str = "+15555550100"
    clinic_address: str = "120 Main Street, Suite 4, Springfield"
    clinic_chairs: int = 2
    open_days: str = "0,1,2,3,4,5"  # Mon=0 ... Sun=6
    open_hour: time = time(8, 0)
    close_hour: time = time(18, 0)
    saturday_close_hour: time = time(13, 0)
    lunch_start: time = time(12, 0)
    lunch_end: time = time(13, 0)
    slot_step_minutes: int = 30
    min_notice_minutes: int = 120
    max_days_ahead: int = 60
    staff_alert_phone: str = ""  # emergencies and new leads are texted here

    # Public product site. DEMO_MODE=true makes the dashboard public (sample clinic, reset daily)
    # and rate-limits the browser call.
    demo_mode: bool = False
    public_url: str = ""
    cron_secret: str = ""  # Vercel Cron sends "Authorization: Bearer <CRON_SECRET>"
    demo_turns_per_ip_per_hour: int = 120

    # Optional LLM for the browser call (any OpenAI-compatible API: OpenAI, Groq, OpenRouter...).
    # Without a key, the browser call runs the deterministic dialogue engine on the same tools.
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4.1-mini"

    # Browser demo page (/demo). The public key is safe to expose; it can only start calls.
    vapi_public_key: str = ""
    vapi_assistant_id: str = ""

    # Twilio (SMS). Empty = dry-run, messages are logged instead of sent.
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_from_number: str = ""

    # Google Calendar. Empty = sync disabled.
    google_calendar_id: str = ""
    google_service_account_json: str = Field(default="", description="Inline JSON or path to the key file")

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.clinic_timezone)

    @property
    def open_weekdays(self) -> set[int]:
        return {int(d) for d in self.open_days.split(",") if d.strip()}

    @property
    def sms_enabled(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token and self.twilio_from_number)

    @property
    def calendar_enabled(self) -> bool:
        return bool(self.google_calendar_id and self.google_service_account_json)


@lru_cache
def get_settings() -> Settings:
    return Settings()
