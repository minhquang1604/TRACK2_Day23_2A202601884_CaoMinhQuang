"""Stretch Goal 5 (GUIDE.md) — Randomized chaos.

Random hoá thời điểm kill (5-15s sau khi loadgen bắt đầu) VÀ mode (`stop` SIGKILL vs
`netblock` SIGSTOP) qua N lần chạy độc lập, mỗi lần đi hết vòng
health_checker -> runbook -> failover thật, rồi báo cáo RTO trung bình + độ lệch chuẩn
(tổng thể và tách theo mode).

KHÔNG thuộc phạm vi chấm điểm (RUBRIC.md chỉ chấm Step 2 và Step 4 cụ thể) — đây là bài
tập mở rộng tự chọn. Không dùng log của script này làm evidence cho reports/rto-evidence.md
(measure_rto.py trong Step 4 đã tự lọc theo cửa sổ thời gian nên không bị lẫn, nhưng để
rõ ràng, mọi log của stretch goal này nằm riêng trong reports/stretch-random-chaos/).

Chạy:  python3 tools/randomized_chaos.py --runs 5
"""
import argparse
import json
import os
import pathlib
import random
import statistics
import subprocess
import sys
import time

import httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.measure_rto import measure  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
OUT_DIR = ROOT / "reports" / "stretch-random-chaos"


def wait_healthy(region: str, timeout: float) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            if httpx.get(f"{URL[region]}/healthz", timeout=1.0).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _respawn_region_a():
    """Region A vua bi SIGKILL (mode=stop) -> pid cu da chet, phai khoi dong uvicorn
    moi cho RIENG region-a (khong dung up_bare.sh vi no se dam vao cong 8002/8080
    dang co tien trinh khac giu)."""
    env = os.environ.copy()
    env.update(REGION="a", STATE_DIR="state/region-a", WARMUP_SECONDS="6")
    log_path = ROOT / "run" / "region-a.log"
    with log_path.open("ab") as log:
        p = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "serving.app:app",
             "--host", "127.0.0.1", "--port", "8001", "--log-level", "warning"],
            cwd=ROOT, env=env, stdout=log, stderr=log)
    (ROOT / "run" / "region-a.pid").write_text(str(p.pid))


def reset_env():
    """Dua stack ve trang thai on dinh truoc MOI lan chay: A song, active_region=a,
    B ve pool_state=warm (khong con du lieu "full" tu lan chay truoc) de moi lan
    failover deu phai lam that su tu dau, khong an theo trang thai cu."""
    if not wait_healthy("a", timeout=2.0):
        subprocess.run([sys.executable, "chaos/kill_region.py", "restore",
                        "--region", "a", "--backend", "bare"], cwd=ROOT)
        if not wait_healthy("a", timeout=5.0):
            _respawn_region_a()
            if not wait_healthy("a", timeout=15.0):
                raise SystemExit("khong khoi dong lai duoc region-a sau SIGKILL")
    (ROOT / "edge" / "active_region").write_text("a")
    (ROOT / "state" / "region-b" / "pool_state").write_text("warm")
    time.sleep(5.5)  # cho edge TTL cache (mac dinh 5s) het han, doc lai file moi


def one_run(i: int, n_total: int, duration: float) -> dict:
    reset_env()
    loadgen_path = OUT_DIR / f"loadgen-{i}.jsonl"
    mode = random.choice(["stop", "netblock"])
    kill_delay = round(random.uniform(5.0, 15.0), 1)

    print(f"\n=== run {i}/{n_total}: mode={mode} kill_at=+{kill_delay}s ===")

    loadgen = subprocess.Popen([sys.executable, "loadgen/traffic.py",
                                "--duration", str(duration), "--rps", "2",
                                "--out", str(loadgen_path)], cwd=ROOT)
    hc = subprocess.Popen([sys.executable, "dr/health_checker.py",
                           "--interval", "5", "--threshold", "3",
                           "--duration", str(duration),
                           "--out", "reports/health-events.jsonl"], cwd=ROOT)

    time.sleep(kill_delay)
    subprocess.run([sys.executable, "chaos/kill_region.py", "--region", "a",
                    "--mode", mode, "--mock"], cwd=ROOT, check=True)
    subprocess.run([sys.executable, "dr/runbook.py", "--primary", "a", "--target", "b",
                    "--backend", "fs", "--auto"], cwd=ROOT, check=True)

    loadgen.wait()
    hc.wait()

    m = measure(loadgen_path, ROOT / "chaos" / "chaos-events.jsonl",
               ROOT / "reports" / "health-events.jsonl",
               ROOT / "reports" / "failover-events.jsonl", 300.0)
    m["mode"], m["kill_delay_s"] = mode, kill_delay
    (OUT_DIR / f"measure-{i}.json").write_text(json.dumps(m, indent=2))
    print(json.dumps({"run": i, "mode": mode, "kill_delay_s": kill_delay,
                      "valid": m["valid"], "verdict": m["rto_verdict"],
                      "rto_measured_s": m["rto_measured_s"]}, indent=2))
    return m


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--runs", type=int, default=5)
    p.add_argument("--duration", type=float, default=60.0,
                   help="do loadgen phai du dai de bao trum ca kill_delay (5-15s) + "
                        "thoi gian phuc hoi (~20-30s thuc te) truoc khi ket thuc")
    a = p.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "state/snapshot.py", "put", "--region", "a",
                    "--backend", "fs"], cwd=ROOT, check=True)

    results = [one_run(i, a.runs, a.duration) for i in range(1, a.runs + 1)]
    reset_env()

    rtos = [r["rto_measured_s"] for r in results if r["rto_measured_s"] is not None]
    summary = {
        "runs": len(results),
        "valid_recoveries": len(rtos),
        "rto_mean_s": round(statistics.mean(rtos), 2) if rtos else None,
        "rto_stddev_s": round(statistics.stdev(rtos), 2) if len(rtos) > 1 else 0.0,
        "rto_min_s": round(min(rtos), 2) if rtos else None,
        "rto_max_s": round(max(rtos), 2) if rtos else None,
        "by_mode": {}, "detail": [],
    }
    for mode in ("stop", "netblock"):
        vals = [r["rto_measured_s"] for r in results
                if r["mode"] == mode and r["rto_measured_s"] is not None]
        if vals:
            summary["by_mode"][mode] = {
                "n": len(vals), "mean_s": round(statistics.mean(vals), 2),
                "stddev_s": round(statistics.stdev(vals), 2) if len(vals) > 1 else 0.0,
            }
    for i, r in enumerate(results, 1):
        summary["detail"].append({"run": i, "mode": r["mode"], "kill_delay_s": r["kill_delay_s"],
                                  "rto_measured_s": r["rto_measured_s"], "valid": r["valid"],
                                  "verdict": r["rto_verdict"]})
    (OUT_DIR / "summary.json").write_text(json.dumps(summary, indent=2))
    print("\n=== SUMMARY ===")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
