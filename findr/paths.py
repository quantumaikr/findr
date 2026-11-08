"""Where findr keeps its data: one folder per person, never inside the files it searches.
    Windows: %LOCALAPPDATA%\\findr     macOS / Linux: ~/.findr      (FINDR_HOME overrides)"""
import os
import sys
from pathlib import Path


def home():
    h = os.environ.get("FINDR_HOME")
    if h:
        return Path(h).expanduser()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "findr"
    return Path.home() / ".findr"


HOME = home()
MODELS = Path(os.environ["FINDR_MODELS"]).expanduser() if os.environ.get("FINDR_MODELS") else HOME / "models"   # models can live on another drive
BIN = HOME / "bin"
DB = HOME / "index.db"
LOGS = HOME / "logs"
CORE = Path(__file__).resolve().parent / "core"


def setup_env():
    """the engine reads its settings from the environment; set the app's folders once, before it is imported"""
    for d in (HOME, MODELS, BIN, LOGS):
        d.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("FINDR_DATA", str(HOME))
    os.environ.setdefault("FINDR_MODELS", str(MODELS))
    quiet()


def quiet():
    """what people see: no developer messages from the libraries ('sending unauthenticated requests to the HF Hub',
    '`torch_dtype` is deprecated', 'Redirects are currently not supported', #67). Set in the environment, so the indexer
    and helper processes started from here are quiet too; FINDR_VERBOSE=1 keeps everything"""
    if os.environ.get("FINDR_VERBOSE") == "1":
        return
    for k, v in (("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1"), ("HF_HUB_VERBOSITY", "error"), ("HF_HUB_DISABLE_PROGRESS_BARS", "1"),
                 ("TRANSFORMERS_VERBOSITY", "error"), ("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1"), ("TOKENIZERS_PARALLELISM", "false"),
                 ("PYTHONWARNINGS", "ignore")):
        os.environ.setdefault(k, v)
    import logging
    import warnings
    warnings.filterwarnings("ignore")
    logging.getLogger("torch.distributed.elastic").setLevel(logging.ERROR)
    if str(CORE) not in sys.path:
        sys.path.insert(0, str(CORE))
