"""Photo meaning server (2026-11-07): a local model that turns photos and requests into comparable vectors.
    scan time:   POST /photo {"paths": [...]}  -> {"vecs": [base64 float16, ...]}   (the photo alone; no request seen)
    search time: POST /text  {"q": "..."}      -> {"vec": base64 float16, "scale": s}
similarity = scale * (photo_vec . text_vec); runs in its own venv (kanana-venv), local only (127.0.0.1).
    ~/kanana-venv/bin/python photo_srv.py [--port 8099]"""
import argparse, base64, json, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import numpy as np
import torch
from transformers import AutoModel

INSTR = "이 사진을 찾으려는 사람이 할 말은?"   # fixed instruction: the same for every photo
ap = argparse.ArgumentParser(); ap.add_argument("--port", type=int, default=8099)
import os
_M = Path(os.environ.get("FINDR_MODELS") or (Path.home() / ".findr" / "models")) / "kanana-2-v-decide-3b-2611"
ap.add_argument("--model", default=str(_M) if _M.exists() else "kakaocorp/kanana-2-v-decide-3b-2611")
a = ap.parse_args()
M = AutoModel.from_pretrained(a.model, trust_remote_code=True, dtype=torch.bfloat16).to("cuda" if torch.cuda.is_available() else "cpu").eval()
M.config.show_criteria = False
D = sys.modules[type(M).__module__.replace("modeling_", "decision_")]
SCALE = 1.0 / float(M.config.decision_temperature)
LOCK = threading.Lock()


def b64(v):
    return base64.b64encode(v.cpu().numpy().astype(np.float16).tobytes()).decode()


def photo_vecs(paths):
    cfg, tok, proc = M.config, M.processor.tokenizer, M.processor
    q = {"type": "choice", "instructions": INSTR, "criteria": {"1": "사진"}}
    out = []
    for p in paths:
        try:
            pairs = D.build_pairs("사진", {"m": q}, False, None, images=[str(p)])
            msgs = [D.query_messages(payload, tok, proc, cfg.max_length, "", truncate=True) for payload, _, _ in pairs.values()]
            with LOCK, torch.no_grad():
                z, _ = M.embed_messages(msgs)
            out.append(b64(z[0]))
        except Exception as e:   # unreadable or odd image: no vector, the file stays searchable by name
            out.append(None); print("photo skipped:", type(e).__name__, flush=True)
    return out


def text_vec(q):
    tok, proc = M.processor.tokenizer, M.processor
    docs = D.validate_candidates([f"1: {q}"], tok, proc, M.config.max_length, D.DOC_INSTRUCTION, truncate=True)
    with LOCK, torch.no_grad():
        z, _ = M._embed_candidates(docs)
    return b64(z[0])


class H(BaseHTTPRequestHandler):
    def log_message(self, *x):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if self.path == "/photo":
            r = {"vecs": photo_vecs(body.get("paths") or [])}
        elif self.path == "/text":
            r = {"vec": text_vec(body.get("q") or ""), "scale": SCALE}
        else:
            self.send_response(404); self.end_headers(); return
        data = json.dumps(r).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data)))
        self.end_headers(); self.wfile.write(data)

    def do_GET(self):
        data = json.dumps({"ok": True, "scale": SCALE}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)


print(f"PHOTO_SRV ready on 127.0.0.1:{a.port}", flush=True)
ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
