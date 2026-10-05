import os
import re
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    access_key: str
    groq_key: str
    azure_key: str
    azure_region: str
    origins: frozenset[str]
    llm_model: str = "openai/gpt-oss-120b"
    stt_model: str = "whisper-large-v3-turbo"
    tamil_voice: str = "ta-IN-PallaviNeural"
    english_voice: str = "en-IN-NeerjaNeural"

    @classmethod
    def from_env(cls):
        s = cls(
            access_key=os.environ.get("APP_ACCESS_KEY", ""),
            groq_key=os.environ.get("GROQ_API_KEY", ""),
            azure_key=os.environ.get("AZURE_SPEECH_KEY", ""),
            azure_region=os.environ.get("AZURE_SPEECH_REGION", ""),
            origins=frozenset(x.strip().rstrip("/") for x in
                              os.environ.get("ALLOWED_ORIGINS", "").split(",") if x.strip()),
            llm_model=os.environ.get("GROQ_LLM_MODEL", cls.llm_model),
            stt_model=os.environ.get("GROQ_STT_MODEL", cls.stt_model),
            tamil_voice=os.environ.get("TAMIL_VOICE", cls.tamil_voice),
            english_voice=os.environ.get("ENGLISH_VOICE", cls.english_voice),
        )
        if len(s.access_key) < 32:
            raise ValueError("APP_ACCESS_KEY must contain at least 32 random characters")
        if not s.groq_key or not s.azure_key or not s.origins:
            raise ValueError("Set GROQ_API_KEY, AZURE_SPEECH_KEY and ALLOWED_ORIGINS")
        if not re.fullmatch(r"[a-z0-9-]+", s.azure_region):
            raise ValueError("AZURE_SPEECH_REGION must be a region ID such as centralindia")
        if any(not x.startswith(("https://", "http://localhost:", "http://127.0.0.1:"))
               for x in s.origins):
            raise ValueError("Origins must use HTTPS, except loopback development URLs")
        return s
