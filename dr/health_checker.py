"""BƯỚC 3a — SINH VIÊN VIẾT. Health checker cho 2 region.

Yêu cầu (đọc §4 "Kiến Trúc Health-Check-Based Failover" + §2 "DNS Failover"):
  1. Poll /readyz của CẢ HAI region mỗi `interval` giây (mặc định 5s).
     Dùng /readyz, KHÔNG dùng /healthz. /healthz chỉ nói "process còn sống" —
     region có process sống nhưng vector DB rỗng thì vẫn không serve được.
  2. Chỉ đổi trạng thái sau `threshold` lần fail LIÊN TIẾP (mặc định 3).
     Một lần fail không phải outage. Đây là chống flapping (§4 Anti-Patterns).
  3. Ghi 1 dòng JSONL MỖI LẦN ĐỔI TRẠNG THÁI (không ghi mỗi lần poll — log sẽ ngập).
     Dòng bắt buộc có: ts, region, to (HEALTHY|UNHEALTHY), reason,
     interval_s, threshold. Thiếu interval_s/threshold thì tools/measure_rto.py
     không tính được detect floor -> mất điểm.

Chạy:  python dr/health_checker.py --interval 5 --threshold 3 --duration 300 \
              --out reports/health-events.jsonl

CÂU HỎI PHẢI TRẢ LỜI TRƯỚC KHI VIẾT (ghi câu trả lời vào reports/postmortem.md):
  interval=5s, threshold=3 -> sớm nhất bạn có thể phát hiện outage là bao nhiêu giây?
  Con số đó nằm TRONG RTO của bạn. Muốn RTO 5 phút thì được phép chọn interval bao nhiêu?
"""
import argparse
import json
import pathlib
import time

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """Gọi /readyz của 1 region. Timeout PHẢI có — netblock làm request treo mãi."""
    try:
        r = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
        body = r.json()
        ready = bool(body.get("ready"))
        reason = "ready" if ready else (",".join(body.get("reasons", [])) or f"http_{r.status_code}")
        return ready, reason
    except Exception as e:  # netblock (SIGSTOP) -> treo -> timeout; stop (SIGKILL) -> ConnectError
        return False, type(e).__name__


def _emit(f, *, region, to, reason, interval, threshold, consecutive):
    line = {
        "event": "state_change", "ts": time.time(), "region": region, "to": to,
        "reason": reason, "interval_s": interval, "threshold": threshold,
        "consecutive_fails": consecutive,
    }
    f.write(json.dumps(line) + "\n")
    f.flush()
    print(json.dumps(line))


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """Poll /readyz của cả 2 region mỗi `interval`s, chỉ đổi trạng thái sau
    `threshold` lần fail (hoặc thành công) LIÊN TIẾP, và chỉ ghi log khi trạng thái
    thực sự đổi (không ghi mỗi lần poll)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    regions = ("a", "b")
    state = {r: "UNKNOWN" for r in regions}
    consec_fail = {r: 0 for r in regions}
    consec_ok = {r: 0 for r in regions}
    start = time.time()
    with out.open("a", encoding="utf-8") as f:
        while time.time() - start < duration:
            for region in regions:
                ready, reason = probe(region, timeout)
                if ready:
                    consec_fail[region] = 0
                    consec_ok[region] += 1
                    if state[region] != "HEALTHY" and consec_ok[region] >= threshold:
                        state[region] = "HEALTHY"
                        _emit(f, region=region, to="HEALTHY", reason=reason,
                              interval=interval, threshold=threshold, consecutive=0)
                else:
                    consec_ok[region] = 0
                    consec_fail[region] += 1
                    if state[region] != "UNHEALTHY" and consec_fail[region] >= threshold:
                        state[region] = "UNHEALTHY"
                        _emit(f, region=region, to="UNHEALTHY", reason=reason,
                              interval=interval, threshold=threshold,
                              consecutive=consec_fail[region])
            time.sleep(interval)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
