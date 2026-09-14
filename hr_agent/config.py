from dataclasses import dataclass
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
                   model=os.getenv("HR_MODEL", ""), embed_model=os.getenv("HR_EMBED_MODEL", ""), **vals)
