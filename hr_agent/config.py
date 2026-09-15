from dataclasses import dataclass, field
from pathlib import Path
import os
from urllib.parse import urlparse
from dotenv import load_dotenv

@dataclass
class Config:
    data: Path
    credentials: Path
    root: str
    ollama: str = "http://127.0.0.1:11434"
    model: str = ""
    embed_model: str = ""
    provider: str = "ollama"
    router_url: str = "https://agentrouter.org/v1"
    router_key: str = field(default="", repr=False)
    codex_binary: str = "/usr/lib/chatgpt/resources/codex"
    router_max_turns: int = 6
    router_daily_requests: int = 12
    scan_seconds: int = 300
    timeout: int = 120
    job_seconds: int = 900
    max_file_mb: int = 20
    max_pages: int = 40
    context: int = 16384
    output_tokens: int = 2048
    threads: int = 6
    ocr_language: str = "eng"
    port: int = 8787
    hr_principals: tuple[str, ...] = ()

    def __post_init__(self):
        if self.provider not in {'ollama', 'agentrouter'}:
            raise ValueError('HR_MODEL_PROVIDER must be ollama or agentrouter')
        if not 1 <= self.router_max_turns <= 12 or not 1 <= self.router_daily_requests <= 1000:
            raise ValueError("Hosted limits: turns 1–12, daily requests 1–1000")
        if not Path(self.codex_binary).is_absolute():
            raise ValueError("HR_CODEX_BINARY must be an absolute path")
        remote = urlparse(self.router_url)
        if (remote.scheme != 'https' or remote.hostname not in {'agentrouter.org', 'co.agentrouter.org'}
                or remote.username or remote.password or remote.port not in {None, 443}
                or remote.path.rstrip('/') != '/v1' or remote.query or remote.fragment):
            raise ValueError('Agent Router URL must be an HTTPS /v1 endpoint on agentrouter.org')
        self.data = Path(self.data).resolve()
        self.credentials = Path(self.credentials).expanduser().resolve()
        import re
        if not re.fullmatch(r"[A-Za-z_+]+",self.ocr_language):
            raise ValueError("Invalid OCR language identifier")
        parsed = urlparse(self.ollama)
        if parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.scheme != "http":
            raise ValueError("Ollama must use a local HTTP endpoint")
        if not 1 <= self.threads <= 6 or not 4096 <= self.context <= 32768:
            raise ValueError("Threads must be 1–6 and context 4096–32768")
        if not 256 <= self.output_tokens <= self.context // 3:
            raise ValueError("Invalid output token budget")
        if min(self.scan_seconds, self.timeout, self.job_seconds, self.max_file_mb, self.max_pages) <= 0:
            raise ValueError("Resource/time limits must be positive")
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data.chmod(0o700)
        for name in ("sources", "reports"):
            (self.data / name).mkdir(exist_ok=True, mode=0o700)

    @classmethod
    def env(cls):
        load_dotenv()
        router_key = os.getenv('AGENTROUTER_API_KEY','').strip()
        key_file = Path(os.getenv('AGENTROUTER_API_KEY_FILE', str(Path(__file__).resolve().parent.parent/'AGENTROUTER_API_KEY.txt')))
        if not router_key and key_file.is_file():
            router_key = key_file.read_text().strip()
            if router_key.startswith('AGENTROUTER_API_KEY='):
                from dotenv import dotenv_values
                router_key = dotenv_values(key_file).get('AGENTROUTER_API_KEY','')

        names = {"scan_seconds":"SCAN_SECONDS", "timeout":"REQUEST_TIMEOUT", "job_seconds":"JOB_SECONDS",
                 "max_file_mb":"MAX_FILE_MB", "max_pages":"MAX_PAGES", "context":"CONTEXT",
                 "output_tokens":"OUTPUT_TOKENS", "threads":"THREADS", "port":"PORT"}
        vals = {key:int(os.environ["HR_"+env]) for key,env in names.items() if "HR_"+env in os.environ}
        return cls(data=Path(os.getenv("HR_DATA_DIR", "data")),
                   credentials=Path(os.getenv("HR_CREDENTIALS", "credentials.json")),
                   root=os.getenv("HR_ROOT_FOLDER", "14lhL4U0mzSOSvPUXIV6Sv20Kfx1aeTbr"),
                   ollama=os.getenv("HR_OLLAMA_URL", "http://127.0.0.1:11434"),
                   ocr_language=os.getenv("HR_OCR_LANGUAGE","eng"),
                   hr_principals=tuple(x.strip().lower() for x in os.getenv("HR_ALLOWED_PRINCIPALS", "").split(",") if x.strip()),
                   provider=os.getenv('HR_MODEL_PROVIDER','ollama'),
                   router_url=os.getenv('AGENTROUTER_BASE_URL','https://agentrouter.org/v1'),
                   router_key=router_key,
                   codex_binary=os.getenv("HR_CODEX_BINARY", "/usr/lib/chatgpt/resources/codex"),
                   router_max_turns=int(os.getenv("HR_ROUTER_MAX_TURNS", "6")),
                   router_daily_requests=int(os.getenv("HR_ROUTER_DAILY_REQUESTS", "12")),
                   model=os.getenv("HR_MODEL", ""), embed_model=os.getenv("HR_EMBED_MODEL", ""), **vals)
