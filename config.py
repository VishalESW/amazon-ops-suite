"""Central configuration. Loads .env and exposes typed settings.

Nothing in the codebase reads os.environ directly except here — import `cfg`.
"""

import base64
import os
from dotenv import load_dotenv

# Load .env from the project root (no-op if the file is missing).
_ROOT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_ROOT, ".env"))


def _bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


class Config:
    ROOT = _ROOT

    # Flask
    SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-insecure-key")
    FERNET_KEY = os.getenv("FERNET_KEY", "")

    # Folders
    UPLOAD_FOLDER = os.path.join(_ROOT, "uploads")
    OUTPUT_FOLDER = os.path.join(_ROOT, "outputs")
    ARCHIVE_FOLDER = os.path.join(_ROOT, "archive")
    DATA_FOLDER = os.path.join(_ROOT, "data")
    DB_PATH = os.path.join(_ROOT, "data", "suite.db")

    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50 MB
    ALLOWED_EXTENSIONS = {"csv", "xlsx", "xls"}

    # Public origin used to build OAuth redirect URIs.
    APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:5000").rstrip("/")

    # Amazon LWA (shared by SP-API and Advertising API).
    LWA_CLIENT_ID = os.getenv("LWA_CLIENT_ID", "")
    LWA_CLIENT_SECRET = os.getenv("LWA_CLIENT_SECRET", "")
    LWA_TOKEN_URL = os.getenv("LWA_TOKEN_URL", "https://api.amazon.com/auth/o2/token")

    # SP-API uses the LWA client tied to the SP-API *solution* app. This may be
    # a different client pair than the one used for Advertising. Defaults to the
    # main LWA client; override SPAPI_CLIENT_ID/SECRET in .env if exchange 400s
    # with invalid_client.
    SPAPI_CLIENT_ID = os.getenv("SPAPI_CLIENT_ID") or os.getenv("LWA_CLIENT_ID", "")
    SPAPI_CLIENT_SECRET = os.getenv("SPAPI_CLIENT_SECRET") or os.getenv("LWA_CLIENT_SECRET", "")

    # SP-API
    # The consent URL uses the SP-API "Application ID". If not provided we fall
    # back to the LWA client id (some app setups use the same value).
    SPAPI_APPLICATION_ID = os.getenv("SPAPI_APPLICATION_ID") or os.getenv("LWA_CLIENT_ID", "")
    SPAPI_APP_DRAFT = _bool("SPAPI_APP_DRAFT", True)
    SPAPI_REGION = os.getenv("SPAPI_REGION", "NA")
    SPAPI_ENDPOINT = os.getenv("SPAPI_ENDPOINT", "https://sellingpartnerapi-na.amazon.com").rstrip("/")
    SPAPI_DEFAULT_MARKETPLACE_ID = os.getenv("SPAPI_DEFAULT_MARKETPLACE_ID", "ATVPDKIKX0DER")

    # Amazon Advertising API (legacy; the Ads section now runs on AdLabs)
    ADS_API_ENDPOINT = os.getenv("ADS_API_ENDPOINT", "https://advertising-api.amazon.com").rstrip("/")
    ADS_SCOPE = os.getenv("ADS_SCOPE", "advertising::campaign_management")

    # AdLabs MCP (Streamable HTTP JSON-RPC)
    ADLABS_MCP_URL = os.getenv("ADLABS_MCP_URL", "https://mcp.adlabs.app/mcp").rstrip("/")
    ADLABS_MCP_KEY = os.getenv("ADLABS_MCP_KEY", "")

    # Clerk authentication (Google-only, restricted to one email domain)
    CLERK_PUBLISHABLE_KEY = os.getenv("CLERK_PUBLISHABLE_KEY", "")
    CLERK_SECRET_KEY = os.getenv("CLERK_SECRET_KEY", "")
    AUTH_ALLOWED_DOMAIN = os.getenv("AUTH_ALLOWED_DOMAIN", "esellerworld.com").lower()

    @property
    def AUTH_ENABLED(self):
        # Auth turns on only once both Clerk keys are present, so the app still
        # runs locally before keys are configured.
        return bool(self.CLERK_SECRET_KEY and self.CLERK_PUBLISHABLE_KEY)

    @property
    def CLERK_FRONTEND_API(self):
        """Derive the Clerk Frontend API host from the publishable key."""
        pk = self.CLERK_PUBLISHABLE_KEY
        if not pk or "_" not in pk:
            return ""
        try:
            return base64.b64decode(pk.split("_", 2)[-1]).decode().rstrip("$")
        except Exception:  # noqa: BLE001
            return ""

    # AI summary endpoint (configurable; defaults assume OpenAI-compatible).
    AI_API_URL = os.getenv("AI_API_URL", "").rstrip("/")
    AI_API_KEY = os.getenv("AI_API_KEY", "")
    AI_CHAT_PATH = os.getenv("AI_CHAT_PATH", "/v1/chat/completions")
    AI_MODEL = os.getenv("AI_MODEL", "gpt-3.5-turbo")

    # Campaign Processor AI (NVIDIA-hosted GLM, OpenAI-compatible).
    CAMPAIGN_AI_URL = os.getenv("CAMPAIGN_AI_URL", "https://integrate.api.nvidia.com/v1").rstrip("/")
    CAMPAIGN_AI_KEY = os.getenv("CAMPAIGN_AI_KEY", "")
    CAMPAIGN_AI_MODEL = os.getenv("CAMPAIGN_AI_MODEL", "meta/llama-3.1-8b-instruct")

    # Campaign Processor v2 — managers (comma-separated emails). These get the
    # manager role (approve gates, see the approval queue); everyone else is an
    # operator by default.
    CAMPAIGN_MANAGER_EMAILS = [
        e.strip().lower() for e in os.getenv("CAMPAIGN_MANAGER_EMAILS", "").split(",")
        if e.strip()
    ]

    # The consent redirect URIs (must match what is registered with Amazon).
    # SP-API uses a top-level /callback (that is what is registered in Seller
    # Central). The app also keeps /inventory/callback working as an alias.
    @property
    def spapi_redirect_uri(self) -> str:
        return f"{self.APP_BASE_URL}/callback"

    @property
    def ads_redirect_uri(self) -> str:
        return f"{self.APP_BASE_URL}/ads/callback"


cfg = Config()
