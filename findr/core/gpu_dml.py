"""The reranker on a Windows GPU that is not NVIDIA (AMD Radeon, Intel Arc or UHD, integrated or not), through DirectML.

findr's GPU paths are CUDA and Apple's; a Windows laptop with integrated AMD graphics ran everything on the CPU (field,
11-13: Ryzen 7 7735U with Radeon graphics). onnxruntime's DirectML build runs the same int8 reranker on any DirectX 12 GPU.
Whether that is faster depends on the GPU: a Radeon 680M has several times the CPU's arithmetic, an Intel UHD 620 has less.
So it is measured, once, and used only where it wins:

- in a separate process (a GPU driver that crashes takes that process down, not the engine), with a time limit;
- the same questions and files on both: the GPU must rank them exactly as the CPU does and be clearly faster (SPEEDUP).
  The two int8 runtimes round differently: on an RTX 5070 Ti through DirectML the largest probability difference to the CPU
  was 0.24, yet each was as close to the full-precision model as the other (0.10 and 0.14) and every order was the same
  (4 questions x 5 files, 11-13). So the order is what must agree; PROB_TOL only catches a GPU that computes nonsense;
- no single GPU pass may take longer than PASS_MAX_S: Windows resets a display driver that is busy for 2 s, and the
  person may be watching a video on the same GPU;
- the result is kept in the data folder (dml_check.json) with the runtime version and the model's size, and measured again
  when either changes. FINDR_DML=0 turns it off, FINDR_DML=1 skips the measurement and uses the GPU.

The engine starts on the CPU until a measurement says otherwise; the measurement runs in the background on the first start
and takes effect from the next one."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

SPEEDUP = 1.33      # the GPU must take at most 1/1.33 of the CPU's time for the same batch
PROB_TOL = 0.30     # largest probability difference to the CPU (a broken driver gives far more; int8 rounding gave 0.24)
PASS_MAX_S = 1.0    # one GPU pass longer than this: not used (display driver reset at 2 s)
CHECK_SECONDS = 240  # the measurement process is stopped after this long
BATCH, TOKENS = 5, 384   # the CPU path's own first pass: 5 candidates of up to 384 tokens


def data_dir():
    return Path(os.environ.get("FINDR_DATA") or os.environ.get("FINDR_HOME") or (Path.home() / ".findr"))


def check_file():
    return data_dir() / "dml_check.json"


def available():
    """DirectML can be tried here: Windows, onnxruntime's DirectML build, and not turned off"""
    if os.name != "nt" or os.environ.get("FINDR_DML") == "0":
        return False
    try:
        import onnxruntime as ort
        return "DmlExecutionProvider" in ort.get_available_providers()
    except Exception:   # noqa: BLE001
        return False


def _key(model):
    try:
        import onnxruntime as ort
        ver = ort.__version__
    except Exception:   # noqa: BLE001
        ver = "?"
    try:
        size = Path(model).stat().st_size
    except OSError:
        size = 0
    return f"{ver}|{size}"


def decided(model):
    """'dml' or 'cpu' from an earlier measurement for this runtime and model; None when there is none yet"""
    if not available():
        return "cpu"
    if os.environ.get("FINDR_DML") == "1":
        return "dml"
    try:
        r = json.loads(check_file().read_text(encoding="utf-8"))
        if r.get("key") == _key(model):
            return "dml" if r.get("use") == "dml" else "cpu"
    except (OSError, ValueError, AttributeError):
        pass
    return None


def session(model, dml, threads=None):
    """an onnxruntime session on the GPU (DirectML) or the CPU"""
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    if dml:   # what DirectML requires: no memory pattern, one node at a time
        so.enable_mem_pattern = False
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        return ort.InferenceSession(str(model), so, providers=["DmlExecutionProvider", "CPUExecutionProvider"])
    so.intra_op_num_threads = int(threads or os.environ.get("OMP_NUM_THREADS") or max(1, (os.cpu_count() or 2) // 2))
    return ort.InferenceSession(str(model), so, providers=["CPUExecutionProvider"])


# the agreement check: questions and candidate files as the engine sees them (name | folder, then the opening text)
PAIRS = {
    "지난달 영업 회의록": ["2025년 10월 영업팀 주간 회의록 | 영업/회의\n참석자: 김부장, 이대리. 신규 고객사 3곳 제안 현황과 견적 일정 논의.",
                     "연말 정산 안내 | 인사\n소득공제 서류 제출 기한은 1월 15일입니다.", "영업 회의 자료_v2.pptx | 영업\n분기 실적과 목표 대비 달성률",
                     "회사 워크숍 사진 목록", "고객사 미팅 메모 | 영업/고객\n견적서 수정 요청, 납기 조건"],
    "근로계약서 양식": ["표준근로계약서(기간의 정함이 없는 경우) | 인사/서식\n근로개시일, 근무장소, 업무내용, 소정근로시간", "출장비 정산서 양식 | 경리",
                  "근로계약 갱신 안내 메일 | 인사", "사업계획서 초안 | 기획", "연봉계약서_홍길동.pdf | 인사/계약"],
    "AI 반도체 시장 보고서": ["2026 AI 반도체 시장 전망 보고서 | 리서치\nNPU, GPU 수요와 국내 팹리스 동향", "사내 식당 메뉴표", "반도체 장비 견적 비교 | 구매",
                       "LLM 추론 가속기 비교 정리 | 연구", "시장조사 설문 결과.xlsx | 마케팅"],
    "invoice from last quarter": ["Invoice_2025Q3_ACME.pdf | finance\nInvoice number 1043, amount due, payment terms net 30",
                                  "quarterly report draft | finance", "travel photos", "purchase order template | procurement", "세금계산서 3분기 | 경리"],
}


def _pairs(model):
    """the agreement batch, tokenized with the reranker's own tokenizer (next to the model); None without one"""
    import numpy as np
    try:
        from tokenizers import Tokenizer
        tok = Tokenizer.from_file(str(Path(model).parent / "tokenizer.json"))
    except Exception:   # noqa: BLE001
        return None, None
    tok.enable_truncation(TOKENS); tok.enable_padding(pad_id=1)
    rows, groups = [], []
    for q, docs in PAIRS.items():
        groups.append((len(rows), len(rows) + len(docs))); rows += [(q, x) for x in docs]
    enc = tok.encode_batch(rows)
    return ({"input_ids": np.array([e.ids for e in enc], dtype=np.int64),
             "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64)}, groups)


def agree(cpu_out, gpu_out, groups):
    """(same order in every group, largest probability difference)"""
    import numpy as np
    c, g = cpu_out[:, 0].astype(np.float64), gpu_out[:, 0].astype(np.float64)
    same = all(list(np.argsort(-c[a:b], kind="stable")) == list(np.argsort(-g[a:b], kind="stable")) for a, b in groups)
    sig = lambda x: 1 / (1 + np.exp(-x))
    return same, float(np.max(np.abs(sig(c) - sig(g))))


def _inputs():
    import numpy as np
    rng = np.random.default_rng(7)
    ids = rng.integers(1000, 30000, size=(BATCH, TOKENS), dtype=np.int64)
    ids[:, 0] = 0   # sequence start
    mask = np.ones_like(ids)
    for i in range(BATCH):   # candidates of different lengths, padded as the engine pads them
        n = TOKENS - 60 * i
        ids[i, n - 1] = 2; ids[i, n:] = 1; mask[i, n:] = 0
    return {"input_ids": ids, "attention_mask": mask}


PROBE_TOKENS = 64   # one candidate of 64 tokens: about 1/30 of a full pass


def _probe(s):
    """seconds for one short pass (after a first one that compiles)"""
    feed = {k: v[:1, :PROBE_TOKENS].copy() for k, v in _inputs().items()}
    s.run(None, feed)
    t = time.perf_counter(); s.run(None, feed)
    return time.perf_counter() - t


def _time(s, feed, runs=3):
    s.run(None, feed)   # the first pass compiles or allocates
    ts = []
    out = None
    for _ in range(runs):
        t = time.perf_counter(); out = s.run(None, feed)[0]; ts.append(time.perf_counter() - t)
    return sorted(ts)[len(ts) // 2], max(ts), out


def measure(model):
    """both devices: speed on a full-length batch, agreement on real questions; the verdict with the numbers behind it"""
    feed = _inputs()
    c = session(model, False)
    cpu_s, _, _ = _time(c, feed)
    g = session(model, True)
    if "DmlExecutionProvider" not in g.get_providers():
        return {"use": "cpu", "why": "no DirectML provider", "cpu_s": round(cpu_s, 4)}
    # a short pass first: a slow GPU is told apart before it gets a full-length one. An Intel UHD 620 took 15 s per full pass,
    # four times in a row, where Windows resets a display driver busy for 2 s (no reset seen there, field 11-13; not risked)
    probe_s = _probe(g)
    est = probe_s * (BATCH * TOKENS) / PROBE_TOKENS
    if est > PASS_MAX_S:
        return {"use": "cpu", "why": f"too slow on a short pass (about {est:.1f}s for a full one)", "cpu_s": round(cpu_s, 4),
                "probe_s": round(probe_s, 4)}
    gpu_s, gpu_max, _ = _time(g, feed)
    pf, groups = _pairs(model)
    if pf is None:
        return {"use": "cpu", "why": "no tokenizer to check agreement", "cpu_s": round(cpu_s, 4), "gpu_s": round(gpu_s, 4)}
    same, diff = agree(c.run(None, pf)[0], g.run(None, pf)[0], groups)
    why = []   # every reason, so doctor says whether it was the speed, the order or both
    if not same:
        why.append("ranks files differently")
    if diff > PROB_TOL:
        why.append(f"scores differ ({diff:.3f})")
    if gpu_max > PASS_MAX_S:
        why.append(f"one GPU pass took {gpu_max:.2f}s")
    if gpu_s * SPEEDUP > cpu_s:
        why.append(f"not faster ({gpu_s:.3f}s vs {cpu_s:.3f}s on the CPU)")
    return {"use": "cpu" if why else "dml", "why": "; ".join(why) or "faster", "cpu_s": round(cpu_s, 4), "gpu_s": round(gpu_s, 4),
            "gpu_max_s": round(gpu_max, 4), "diff": round(diff, 4), "probe_s": round(probe_s, 4)}


def check(model, wait=False):
    """measure in a separate process and keep the verdict. With wait=False it runs in the background (the engine goes on on
    the CPU); returns the verdict when waited for"""
    if not available() or decided(model) is not None:
        return None
    cmd = [sys.executable, str(Path(__file__).resolve()), "measure", str(model)]
    flags = 0x0800_0000 | 0x0000_4000 if os.name == "nt" else 0   # no window, below normal priority

    def run():
        t = time.time()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=CHECK_SECONDS, creationflags=flags)
            line = (p.stdout or "").strip().splitlines()[-1] if p.returncode == 0 and (p.stdout or "").strip() else ""
            r = json.loads(line) if line else {"use": "cpu", "why": f"measurement failed (exit {p.returncode})"}
        except subprocess.TimeoutExpired:
            r = {"use": "cpu", "why": f"measurement took over {CHECK_SECONDS}s"}
        except Exception as e:   # noqa: BLE001
            r = {"use": "cpu", "why": f"measurement failed ({type(e).__name__})"}
        r.update(key=_key(model), seconds=round(time.time() - t, 1), at=int(time.time()))
        try:
            check_file().write_text(json.dumps(r), encoding="utf-8")
        except OSError:
            pass
        print(f"FINDR_DML {r['use']} ({r['why']})", flush=True)
        return r

    if wait:
        return run()
    import threading
    threading.Thread(target=run, daemon=True, name="findr-dml-check").start()
    return None


def describe():
    """(Korean, English) for doctor: what the reranker runs on here and why; None off Windows"""
    if os.name != "nt":
        return None
    if not available():
        return (("DirectML 끔 (FINDR_DML=0)", "DirectML off (FINDR_DML=0)") if os.environ.get("FINDR_DML") == "0"
                else ("DirectML 없음: CPU", "no DirectML: the CPU"))
    try:
        r = json.loads(check_file().read_text(encoding="utf-8"))
        nums = f" (GPU {r['gpu_s']}s, CPU {r['cpu_s']}s)" if "gpu_s" in r and "cpu_s" in r else ""
        if r.get("use") == "dml":
            return f"GPU (DirectML){nums}", f"the GPU (DirectML){nums}"
        return f"CPU: {r.get('why', '')}{nums}", f"the CPU: {r.get('why', '')}{nums}"
    except (OSError, ValueError, KeyError):
        return "GPU 측정 전 (검색 엔진이 처음 켜질 때 한 번)", "GPU not measured yet (once, when the engine first starts)"


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "measure":
        print(json.dumps(measure(sys.argv[2])), flush=True)
