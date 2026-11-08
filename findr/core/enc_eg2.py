"""EmbeddingGemma 2 encoder with the same interface as jido's Enc (emb(texts) -> normalized torch tensor).
Questions use the search-query prompt, documents the document prompt. Text such as <|image|> is neutralized first
(it would otherwise be read as a media slot). Which encoder an index uses is recorded in its meta table (see encoder_of)."""
import re
from pathlib import Path

import torch

import os
_M = Path(os.environ.get("FINDR_MODELS") or (Path.home() / ".findr" / "models"))
PATH = str(_M / "embeddinggemma-2") if (_M / "embeddinggemma-2").exists() else "google/embeddinggemma-2"
_SAFE = re.compile(r"<\|([^|>]{0,40})\|>")


def encoder_of(con):
    """the encoder an index was built with (meta table), default bge-m3"""
    try:
        r = con.execute("SELECT value FROM meta WHERE key='encoder'").fetchone()
        return r[0] if r else "bge-m3"
    except Exception:
        return "bge-m3"


def pick_device(device=None):
    """FINDR_DEVICE, else NVIDIA (cuda), else Apple GPU (mps), else cpu; a device that is not there falls back to the CPU"""
    import os
    d = device or os.environ.get("FINDR_DEVICE")
    mps = getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available()
    if d == "cuda" and not torch.cuda.is_available() or d == "mps" and not mps:
        d = None
    return d or ("cuda" if torch.cuda.is_available() else "mps" if mps else "cpu")


class EG2:
    def __init__(self, device=None, max_len=512):   # no GPU on this PC: fall back to the CPU (FINDR_DEVICE can force it)
        import os
        device = pick_device(device)
        from sentence_transformers import SentenceTransformer
        # Apple GPU: float32 (M1 has no bfloat16 hardware, and float16 overflows in this model). CPU: float32 as well. The
        # config's bfloat16 runs at 0.1 pieces/s on a 4-core laptop CPU without bf16 support (#66); on a recent CPU the two
        # are within 10% (6.5 vs 5.9 pieces/s on 4 threads)
        kw = {"model_kwargs": {"torch_dtype": torch.float32}} if device in ("mps", "cpu") else {}
        self.st = SentenceTransformer(PATH, device=device, **kw); self.st.max_seq_length = max_len; self.dev = device
        self.bs = int(os.environ.get("FINDR_BATCH") or (16 if device == "mps" else 64))

    @torch.no_grad()
    def emb(self, texts, bs=None, query=True):
        bs = bs or getattr(self, "bs", 64)
        texts = [_SAFE.sub(r"< \1 >", t) for t in texts]
        fn = self.st.encode_query if query else self.st.encode_document
        return fn(texts, batch_size=bs, normalize_embeddings=True, convert_to_tensor=True).float()


class SmallEnc(EG2):
    """a small multilingual encoder for PCs without a GPU (multilingual-e5-small, 118M): ~7-10x faster on the CPU.
    Same interface as EG2 (emb(texts, query=...)); the index records which encoder built its vectors."""
    NAME = "e5-small"

    def __init__(self, device=None, max_len=512):
        import os
        device = pick_device(device)
        from sentence_transformers import SentenceTransformer
        p = _M / "multilingual-e5-small"
        self.st = SentenceTransformer(str(p) if p.exists() else "intfloat/multilingual-e5-small", device=device)
        self.st.max_seq_length = max_len; self.dev = device

    @torch.no_grad()
    def emb(self, texts, bs=8, query=True):
        texts = [("query: " if query else "passage: ") + _SAFE.sub(r"< \1 >", t) for t in texts]
        return self.st.encode(texts, batch_size=bs, normalize_embeddings=True, convert_to_tensor=True).float()


class OtherEnc(SmallEnc):
    """e5-base (query:/passage: prefixes) or ko-sroberta (no prefixes): candidates for PCs without a GPU"""
    REPO = {"e5-base": ("multilingual-e5-base", "intfloat/multilingual-e5-base", True),
            "ko-sroberta": ("ko-sroberta-multitask", "jhgan/ko-sroberta-multitask", False)}

    def __init__(self, name, device=None, max_len=512):
        import os
        device = pick_device(device)
        from sentence_transformers import SentenceTransformer
        local, hub, self.prefix = self.REPO[name]
        m = Path(os.environ.get("FINDR_MODELS") or (Path.home() / ".findr" / "models")) / local
        for c in (m,):
            if c.exists():
                hub = str(c); break
        self.st = SentenceTransformer(hub, device=device); self.dev = device
        self.st.max_seq_length = min(max_len, self.st.max_seq_length or max_len)

    @torch.no_grad()
    def emb(self, texts, bs=8, query=True):
        texts = [(("query: " if query else "passage: ") if self.prefix else "") + _SAFE.sub(r"< \1 >", t) for t in texts]
        return self.st.encode(texts, batch_size=bs, normalize_embeddings=True, convert_to_tensor=True).float()


ENCODERS = ("embeddinggemma-2", "e5-small", "e5-base", "ko-sroberta")


def make_encoder(name, device=None, max_len=512):
    """the encoder named in an index's meta table"""
    if name in OtherEnc.REPO:
        return OtherEnc(name, device, max_len)
    return SmallEnc(device, max_len) if name == "e5-small" else EG2(device, max_len)
