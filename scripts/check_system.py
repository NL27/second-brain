#!/usr/bin/env python3
"""Second Brain - portable system check + recommendation engine.

Run this FIRST on any machine (macOS / Linux / Windows). It uses only the
Python standard library, so it works on a fresh box before anything is
installed. It inspects the system, decides what Second Brain can do here, and
prints prioritized, copy-pasteable recommendations.

Usage:
  python3 scripts/check_system.py            # human report
  python3 scripts/check_system.py --json      # machine-readable
  python3 scripts/check_system.py --install    # attempt safe installs (best effort)

Exit code is 0 if the machine can at least run plan-only mode, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

# --- tiny ANSI helpers (auto-disabled when not a TTY or NO_COLOR set) --------
_USE_COLOR = sys.stdout.isatty() and not os.getenv("NO_COLOR")


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


def bold(s):  return _c(s, "1")
def green(s): return _c(s, "32")
def yellow(s): return _c(s, "33")
def red(s):   return _c(s, "31")
def cyan(s):  return _c(s, "36")
def dim(s):   return _c(s, "2")

OK = green("ok")
MISSING = yellow("missing")
WARN = yellow("warn")


# --- system probes -----------------------------------------------------------
def total_ram_gb() -> float:
    try:
        if hasattr(os, "sysconf") and "SC_PHYS_PAGES" in os.sysconf_names:
            return round(os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / (1024 ** 3), 1)
    except Exception:
        pass
    if platform.system() == "Windows":
        try:
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            stat = MS()
            stat.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return round(stat.ullTotalPhys / (1024 ** 3), 1)
        except Exception:
            return 0.0
    return 0.0


def is_apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() in ("arm64", "aarch64")


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def ollama_running(host: str) -> bool:
    try:
        import urllib.request

        with urllib.request.urlopen(host.rstrip("/") + "/api/tags", timeout=0.8) as r:
            return r.status == 200
    except Exception:
        return False


def env_keys(root: Path) -> dict:
    """Read provider keys from the process env and a local .env (if present)."""
    keys = {}
    names = ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GROQ_API_KEY",
             "DEEPSEEK_API_KEY", "GEMINI_API_KEY", "DASHSCOPE_API_KEY"]
    env_file = root / ".env"
    file_vals = {}
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            file_vals[k.strip()] = v.strip()
    for n in names:
        keys[n] = bool(os.getenv(n) or file_vals.get(n))
    return keys


# --- recommendation tiers ----------------------------------------------------
def recommend_local_models(ram: float) -> list:
    if ram >= 64:
        return ["ui-tars:7b (vision/grounding)", "qwen2.5:14b or llama3.1:8b (general)"]
    if ram >= 32:
        return ["ui-tars:7b (vision/grounding)", "qwen2.5:7b (general)"]
    if ram >= 16:
        return ["qwen2.5:7b (general)", "ui-tars:7b only if you close other apps"]
    if ram >= 8:
        return ["qwen2.5:3b or llama3.2:3b (small)", "lean on cloud models"]
    return ["(too little RAM for comfortable local models) - use cloud models"]


def gather(root: Path) -> dict:
    osname = platform.system()
    pyv = sys.version_info
    host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    info = {
        "os": osname,
        "os_release": platform.release(),
        "arch": platform.machine(),
        "apple_silicon": is_apple_silicon(),
        "python": f"{pyv.major}.{pyv.minor}.{pyv.micro}",
        "python_ok_planonly": pyv >= (3, 9),
        "python_ok_control": pyv >= (3, 11),
        "cpu_count": os.cpu_count() or 0,
        "ram_gb": total_ram_gb(),
        "tools": {t: have(t) for t in ("git", "python3", "pip3", "ollama",
                                       "brew", "node", "npx", "curl", "docker")},
        "ollama_host": host,
        "ollama_running": ollama_running(host),
        "keys": env_keys(root),
    }

    # Capability verdicts.
    if info["os"] == "Darwin" and info["apple_silicon"] and info["python_ok_control"]:
        info["host_control"] = "supported"
    elif info["os"] == "Darwin" and info["apple_silicon"]:
        info["host_control"] = "needs Python >= 3.11"
    elif info["os"] in ("Linux", "Windows"):
        info["host_control"] = "sandbox/VM path (cua), host driver is macOS-only"
    else:
        info["host_control"] = "plan-only"

    info["local_models"] = recommend_local_models(info["ram_gb"])
    info["any_cloud_key"] = any(info["keys"].values())
    info["can_run"] = info["python_ok_planonly"]
    return info


def build_recommendations(i: dict, root: Path) -> list:
    recs = []  # (priority, text, command-or-None)

    if not i["python_ok_planonly"]:
        recs.append(("blocker", "Install Python >= 3.9 (3.11+ recommended).", None))

    osname = i["os"]
    install_hint = {
        "Darwin": "brew install {pkg}",
        "Linux": "sudo apt-get install -y {pkg}  # or your distro's package manager",
        "Windows": "winget install {pkg}",
    }.get(osname, "install {pkg}")

    if not i["tools"]["git"]:
        recs.append(("high", "Install git (used for versioned run logs).",
                     install_hint.format(pkg="git")))

    # Host control readiness
    if osname == "Darwin" and i["apple_silicon"]:
        if not i["python_ok_control"]:
            recs.append(("high", "Install Python 3.11+ to enable real host control (cua).",
                         "brew install python@3.12  # then: PYTHON=python3.12 bash scripts/setup.sh"))
        recs.append(("high", "Grant Accessibility + Screen Recording to your terminal/IDE "
                     "(System Settings > Privacy & Security).", None))
        recs.append(("medium", "Install host control (cua-agent + Cua Driver).",
                     "pip install 'cua-agent[all]' && "
                     "/bin/bash -c \"$(curl -fsSL https://raw.githubusercontent.com/"
                     "trycua/cua/main/libs/cua-driver/scripts/install.sh)\""))
    elif osname in ("Linux", "Windows"):
        recs.append(("medium", "Background host control via the Cua Driver is macOS-only. "
                     "On this OS use the cua sandbox/VM path or stay in plan-only mode.",
                     "pip install 'cua-agent[all]'"))

    # Local models
    if not i["tools"]["ollama"]:
        cmd = "brew install ollama" if osname == "Darwin" else \
              ("curl -fsSL https://ollama.com/install.sh | sh" if osname == "Linux" else
               "winget install Ollama.Ollama")
        recs.append(("medium", "Install Ollama to run local models (free, private).", cmd))
    elif not i["ollama_running"]:
        recs.append(("medium", "Ollama is installed but not reachable - start it.", "ollama serve"))
    else:
        first = i["local_models"][0].split(" ")[0] if i["local_models"] else "qwen2.5:7b"
        recs.append(("low", f"Pull recommended local model(s) for {i['ram_gb']} GB RAM.",
                     "ollama pull ui-tars:7b && ollama pull qwen2.5:7b"))

    # Cloud keys
    if not i["any_cloud_key"]:
        recs.append(("low", "Add at least one cloud API key for model comparisons "
                     "(optional). Copy .env.example to .env and fill a key.",
                     "cp .env.example .env"))

    # Multi-LLM routing dependency
    recs.append(("medium", "Install Second Brain + multi-LLM routing (litellm).",
                 "python3 -m venv .venv && source .venv/bin/activate && pip install -e '.[llm]'"))

    order = {"blocker": 0, "high": 1, "medium": 2, "low": 3}
    recs.sort(key=lambda r: order.get(r[0], 9))
    return recs


# --- rendering ---------------------------------------------------------------
def render(i: dict, recs: list) -> None:
    print(bold("\nSecond Brain - system check\n" + "=" * 28))

    print(bold("\nSystem"))
    print(f"  OS            {i['os']} {i['os_release']} ({i['arch']})"
          + ("  Apple Silicon" if i["apple_silicon"] else ""))
    print(f"  Python        {i['python']}  "
          + (green("plan-only ok") if i["python_ok_planonly"] else red("too old")) + "  "
          + (green("host-control ok") if i["python_ok_control"] else yellow("needs 3.11 for cua")))
    print(f"  CPU / RAM     {i['cpu_count']} cores / {i['ram_gb']} GB")

    print(bold("\nTooling"))
    for tool, present in i["tools"].items():
        print(f"  {tool:<9}    " + (OK if present else MISSING))

    print(bold("\nLLM readiness"))
    print(f"  Ollama        " + (
        green("running") if i["ollama_running"] else
        (yellow("installed, not running") if i["tools"]["ollama"] else MISSING))
        + dim(f"  ({i['ollama_host']})"))
    cloud = [k.replace("_API_KEY", "") for k, v in i["keys"].items() if v]
    print(f"  Cloud keys    " + (green(", ".join(cloud)) if cloud else yellow("none set")))

    print(bold("\nCapabilities here"))
    hc = i["host_control"]
    hc_col = green(hc) if hc == "supported" else yellow(hc)
    print(f"  Host control  {hc_col}")
    print(f"  Plan-only     " + (green("yes") if i["can_run"] else red("no")))
    print(f"  Recommended local models:")
    for m in i["local_models"]:
        print(f"    - {m}")

    print(bold("\nRecommended next steps") + dim("  (in priority order)"))
    tag_col = {"blocker": red, "high": yellow, "medium": cyan, "low": dim}
    for n, (pri, text, cmd) in enumerate(recs, start=1):
        tag = tag_col.get(pri, str)(f"[{pri}]")
        print(f"  {n}. {tag} {text}")
        if cmd:
            print(f"       {dim('$')} {cmd}")

    print()
    if i["can_run"]:
        print(green("This machine can run Second Brain (at least plan-only)."))
        print(dim("Fastest path:  bash scripts/setup.sh   then   brain doctor"))
    else:
        print(red("This machine cannot run Second Brain yet - resolve blockers above."))
    print()


# --- optional best-effort installs ------------------------------------------
def try_install(i: dict, recs: list) -> None:
    print(yellow("\n--install: attempting safe installs (best effort)...\n"))
    cmds = [cmd for _, _, cmd in recs if cmd and not cmd.startswith(("cp ", "ollama pull"))]
    for cmd in cmds:
        print(cyan(f"$ {cmd}"))
        try:
            subprocess.run(cmd, shell=True, check=False)
        except Exception as exc:
            print(red(f"  failed: {exc}"))


def main() -> int:
    ap = argparse.ArgumentParser(description="Second Brain system check.")
    ap.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    ap.add_argument("--install", action="store_true", help="Attempt safe installs (best effort).")
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[1]
    info = gather(root)
    recs = build_recommendations(info, root)

    if args.json:
        print(json.dumps({"system": info,
                          "recommendations": [{"priority": p, "text": t, "command": c}
                                              for p, t, c in recs]}, indent=2))
        return 0 if info["can_run"] else 1

    render(info, recs)
    if args.install:
        try_install(info, recs)
    return 0 if info["can_run"] else 1


if __name__ == "__main__":
    sys.exit(main())
