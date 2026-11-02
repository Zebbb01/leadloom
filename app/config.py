import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    # Shared by n8n and this service: every /v1 call carries it.
    api_key: str
    # Signs a person in to review live leads.
    reviewer_key: str
    public_url: str = "http://localhost:8400"
    # Base of n8n's production webhooks, used by the demo and the retry button.
    n8n_webhook_url: str = ""
    llm_endpoint: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    llm_api_key: str = ""
    llm_model: str = "gemini-3.5-flash-lite"
    # Sent as reasoning_effort to reasoning models; empty leaves it out.
    llm_reasoning_effort: str = "low"
    daily_llm_cap: int = 200
    hubspot_token: str = ""
    hubspot_base_url: str = "https://api.hubapi.com"
    hubspot_portal_id: str = ""
    cal_webhook_secret: str = ""
    inbound_email_secret: str = ""
    demo_daily_per_workspace: int = 15
    # Local development only: lets enrichment fetch sites on localhost.
    enrich_allow_private: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        return cls(
            database_url=e("DATABASE_URL", ""),
            api_key=e("LEADLOOM_API_KEY", ""),
            reviewer_key=e("REVIEWER_KEY", ""),
            public_url=e("PUBLIC_URL", cls.public_url).rstrip("/"),
            n8n_webhook_url=e("N8N_WEBHOOK_URL", "").rstrip("/"),
            llm_endpoint=e("LLM_ENDPOINT", cls.llm_endpoint).rstrip("/"),
            llm_api_key=e("LLM_API_KEY", ""),
            llm_model=e("LLM_MODEL", cls.llm_model),
            llm_reasoning_effort=e("LLM_REASONING_EFFORT", cls.llm_reasoning_effort),
            daily_llm_cap=int(e("DAILY_LLM_CAP", str(cls.daily_llm_cap))),
            hubspot_token=e("HUBSPOT_TOKEN", ""),
            hubspot_base_url=e("HUBSPOT_BASE_URL", cls.hubspot_base_url).rstrip("/"),
            hubspot_portal_id=e("HUBSPOT_PORTAL_ID", ""),
            cal_webhook_secret=e("CAL_WEBHOOK_SECRET", ""),
            inbound_email_secret=e("INBOUND_EMAIL_SECRET", ""),
            demo_daily_per_workspace=int(e("DEMO_DAILY_PER_WORKSPACE", str(cls.demo_daily_per_workspace))),
            enrich_allow_private=e("ENRICH_ALLOW_PRIVATE", "") == "true",
        )
