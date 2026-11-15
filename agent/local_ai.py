"""Local AI: run the cheap, high-volume steps (triage, inbox extraction, JD checks, ranking people, browsing steps, daily plan, reflection) on your own
GPU/CPU with llama.cpp's OpenAI-compatible server, and keep Claude (or another hosted model) for anything a human will read.

  detect()     what this machine has (GPU, VRAM, RAM, disk, OS)
  recommend()  which model fits, or none
  install()    download the matching llama.cpp release (CUDA / Metal / Vulkan / CPU build) into the workspace; the model downloads on first start
  start()      run llama-server on 127.0.0.1:8080 in the background (silent); stop() ends it
  bench()      agreement test: local vs your main model on your own triaged jobs; routing switches only if it is good enough
Nothing here runs unless you ask (CLI `local-setup`, GUI Settings, or the setup wizard)."""
import json, os, platform, re, shutil, subprocess, sys, tarfile, time, urllib.request, zipfile
import common
from common import HOME, LOGS, log

DIR = os.path.join(HOME, "local-ai")
PORT = 8080
MODELS = [  # (min VRAM GB on a discrete GPU or unified memory on Apple, hf repo:quant, label, size GB)
    (7.5, "unsloth/Qwen3.5-9B-GGUF:Q4_K_M", "Qwen3.5-9B (Q4_K_M)", 5.7),
    (5.0, "unsloth/Qwen3.5-4B-GGUF:Q4_K_M", "Qwen3.5-4B (Q4_K_M)", 2.7),
]
STARTED = []  # servers started by this process (stopped again after a scheduled run unless keep_running)
LOCAL_KINDS = ["triage", "ingest", "today", "reflect", "scout", "rank", "browse"]


def _run(args, timeout=15):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def detect():
    hw = {"os": platform.system(), "arch": platform.machine().lower(), "gpu": "", "vram_gb": 0.0, "ram_gb": 0.0, "disk_free_gb": round(shutil.disk_usage(HOME).free / 1e9, 1), "apple": False}
    out = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"]) if shutil.which("nvidia-smi") else ""
    if out.strip():
        name, mem = out.strip().splitlines()[0].rsplit(",", 1)
        hw.update(gpu=name.strip(), vram_gb=round(float(mem) / 1024, 1), backend="cuda")
    try:
        if hw["os"] == "Windows":
            import ctypes

            class M(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong), ("avail", ctypes.c_ulonglong)] + [(f"x{i}", ctypes.c_ulonglong) for i in range(5)]
            m = M()
            m.l = ctypes.sizeof(M)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            hw["ram_gb"] = round(m.total / 2 ** 30, 1)
        elif hw["os"] == "Darwin":
            hw["ram_gb"] = round(int(_run(["sysctl", "-n", "hw.memsize"]) or 0) / 2 ** 30, 1)
            if hw["arch"] in ("arm64", "aarch64"):
                hw.update(apple=True, gpu=hw["gpu"] or "Apple Silicon (Metal)", vram_gb=round(hw["ram_gb"] * 0.65, 1), backend="metal")
        else:
            m = re.search(r"MemTotal:\s+(\d+)", open("/proc/meminfo").read())
            hw["ram_gb"] = round(int(m.group(1)) / 2 ** 20, 1) if m else 0
    except Exception:
        pass
    if not hw.get("backend"):
        hw["backend"] = "vulkan" if hw["os"] == "Linux" and shutil.which("vulkaninfo") else "cpu"
    return hw


def recommend(hw=None):
    hw = hw or detect()
    for need, repo, label, size in MODELS:
        if hw["vram_gb"] >= need and hw["disk_free_gb"] > size + 3:
            return {"repo": repo, "label": label, "size_gb": size, "why": f"fits fully in {hw['vram_gb']} GB of {'unified memory' if hw['apple'] else 'VRAM'} with room for a 16k context"}
    if hw["ram_gb"] >= 16 and hw["disk_free_gb"] > 6:
        return {"repo": MODELS[-1][1], "label": MODELS[-1][2] + " on CPU", "size_gb": MODELS[-1][3], "why": "no suitable GPU: a small model on the CPU is slow but works for overnight triage"}
    return None


# ------------------------------------------------------------------ install / run
def _asset_pattern(hw):
    if hw["os"] == "Windows":
        return [r"llama-b\d+-bin-win-cuda-13\.\d-x64\.zip", r"cudart-llama-bin-win-cuda-13\.\d-x64\.zip"] if hw["backend"] == "cuda" else [r"llama-b\d+-bin-win-vulkan-x64\.zip" if hw["gpu"] else r"llama-b\d+-bin-win-cpu-x64\.zip"]
    if hw["os"] == "Darwin":
        return [r"llama-b\d+-bin-macos-arm64\.tar\.gz" if hw["apple"] else r"llama-b\d+-bin-macos-x64\.tar\.gz"]
    if hw["backend"] == "cuda":
        return [r"llama-b\d+-bin-ubuntu-cuda-13\.\d-x64\.tar\.gz"]
    return [r"llama-b\d+-bin-ubuntu-vulkan-x64\.tar\.gz" if hw["backend"] == "vulkan" else r"llama-b\d+-bin-ubuntu-x64\.tar\.gz"]


def server_bin():
    if shutil.which("llama-server"):
        return shutil.which("llama-server")
    for root, _, files in os.walk(DIR):
        for f in files:
            if f in ("llama-server", "llama-server.exe"):
                return os.path.join(root, f)
    return None


def install(progress=print):
    """Download the right llama.cpp build into <workspace>/local-ai. macOS with Homebrew: `brew install llama.cpp` is used instead."""
    if server_bin():
        return server_bin()
    hw = detect()
    if hw["os"] == "Darwin" and shutil.which("brew"):
        progress("installing llama.cpp with Homebrew ...")
        subprocess.run(["brew", "install", "llama.cpp"])
        return server_bin()
    rels = json.loads(urllib.request.urlopen(urllib.request.Request("https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=5",
                                                                    headers={"User-Agent": "JobAgent"}), timeout=30).read())
    os.makedirs(DIR, exist_ok=True)
    for rel in rels:  # the newest release that has every asset we need
        names = {a["name"]: a["browser_download_url"] for a in rel["assets"]}
        picks = [next((n for n in names if re.fullmatch(p, n)), None) for p in _asset_pattern(hw)]
        if all(picks):
            break
    else:
        raise RuntimeError("no matching llama.cpp build found in the latest releases")
    for n in picks:
        dest = os.path.join(DIR, n)
        progress(f"downloading {n} ...")
        urllib.request.urlretrieve(names[n], dest)
        progress(f"extracting {n} ...")
        if n.endswith(".zip"):
            zipfile.ZipFile(dest).extractall(os.path.join(DIR, "bin"))
        else:
            tarfile.open(dest).extractall(os.path.join(DIR, "bin"))
        os.remove(dest)
    b = server_bin()
    if b and os.name != "nt":
        os.chmod(b, 0o755)
    return b


def running():
    try:
        return json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2).read()).get("status") == "ok"
    except Exception:
        return False


def start(model=None, ctx=None, wait=900):
    """Start llama-server (first start downloads the model, several minutes). Returns True when healthy."""
    if running():
        return True
    b = server_bin()
    if not b:
        raise RuntimeError("llama.cpp is not installed: run `python agent/run.py local-setup`")
    rec = common.SETTINGS.get("llm", {}).get("local", {})
    ctx = ctx or rec.get("ctx", 32768)
    model = model or rec.get("model") or (recommend() or {}).get("repo")
    if not model:
        raise RuntimeError("no local model fits this machine")
    hw = detect()
    args = [b, "-hf", model, "--host", "127.0.0.1", "--port", str(PORT), "-c", str(ctx), "--jinja", "--alias", "local", "-np", "1"]
    if hw["backend"] != "cpu":
        args += ["-ngl", "99", "-fa", "on", "--cache-type-k", "q8_0", "--cache-type-v", "q8_0"]
    logf = open(os.path.join(LOGS, "llama-server.log"), "a", encoding="utf-8")
    kw = {"creationflags": 0x08000000} if os.name == "nt" else {"start_new_session": True}
    p = subprocess.Popen(args, stdout=logf, stderr=subprocess.STDOUT, cwd=DIR if os.path.isdir(DIR) else None, **kw)
    common.meta_set(common.db(), "local_ai_pid", str(p.pid))
    STARTED.append(p.pid)
    t0 = time.time()
    while time.time() - t0 < wait:
        if running():
            log(f"local AI ready: {model} on port {PORT}")
            return True
        if p.poll() is not None:
            raise RuntimeError("llama-server exited; see logs/llama-server.log")
        time.sleep(3)
    return False


def stop():
    pid = common.meta_get(common.db(), "local_ai_pid")
    if not pid:
        return "not started by JobAgent"
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True)
        else:
            os.kill(int(pid), 15)
    except Exception as e:
        return f"could not stop: {e}"
    return "stopped"


# ------------------------------------------------------------------ quality gate
def bench(n=30, threshold=0.85, apply=False):
    """Re-triage n of your already-triaged jobs (full JDs only) with the local model and compare verdicts with your main model's.
    With apply=True and agreement >= threshold, routes the cheap kinds to the local model (main model stays as fallback)."""
    import pipeline, llm
    c = common.db()
    rows = c.execute("""SELECT j.* , t.verdict tv FROM jobs j JOIN triage t ON t.job_id=j.id WHERE j.jd_full=1 ORDER BY t.at DESC LIMIT ?""", (n,)).fetchall()
    if len(rows) < 8:
        return {"ok": False, "why": f"only {len(rows)} fully-read triaged jobs to compare; run a few cycles first"}
    agree = total = 0
    diffs = []
    t0 = time.time()
    for i in range(0, len(rows), 5):
        batch = rows[i:i + 5]
        prompt = pipeline.triage_prompt(batch)
        try:
            text, _ = llm.run(prompt, kind="bench", provider="local", timeout=600)
            res = common.extract_json(text)
        except Exception as e:
            return {"ok": False, "why": f"local model failed: {e}"}
        by = {o.get("id"): (o.get("verdict") or "").upper() for o in (res if isinstance(res, list) else [])}
        for r in batch:
            total += 1
            lv, mv = by.get(r["id"], ""), r["tv"]
            ok = (lv == mv) or (lv in ("APPLY", "STRETCH") and mv in ("APPLY", "STRETCH"))  # APPLY vs STRETCH both mean 'pursue'
            agree += ok
            if not ok:
                diffs.append(f"{r['company']} - {r['title'][:50]}: local {lv or '?'} vs main {mv}")
    rate = agree / max(total, 1)
    out = {"ok": rate >= threshold, "agreement": round(rate, 3), "jobs": total, "seconds": round(time.time() - t0), "threshold": threshold, "disagreements": diffs}
    common.meta_set(c, "local_bench", json.dumps(out))
    if apply and out["ok"]:
        routing = {k: {"provider": "local", "model": "local", "effort": ""} for k in LOCAL_KINDS}
        common.save_override("settings.json", {"llm": {"routing": routing}})
        common.reload_config()
        out["routed"] = LOCAL_KINDS
    log(f"local bench: {out}")
    return out
