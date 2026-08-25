"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append 1 dòng JSONL có ts + iso vào LOG, và print ra stdout."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()), **kw}
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")
    print(json.dumps(line))
    return line


def failover(target: str, backend: str, wait: float) -> dict:
    """5 bước ở trên, đúng thứ tự. Region còn lại (không phải target) được coi là
    primary khi tính RPO — lab này chỉ có 2 region."""
    primary = "b" if target == "a" else "a"

    # 1_verify_target — chỉ để log, KHÔNG chặn failover nếu probe lỗi
    # (region phụ đang warm/chưa sẵn sàng chính là lý do ta phải failover).
    try:
        before = httpx.get(f"{URL[target]}/v1/state", timeout=2.0).json()
    except Exception as e:
        before = {"error": type(e).__name__}
    emit(step="1_verify_target", target=target, state_before=before)

    # 2_restore_snapshot
    meta = snapshot.get(target, backend)
    primary_db = pathlib.Path(f"state/region-{primary}/vectors.sqlite")
    restored_db = pathlib.Path(f"state/region-{target}/vectors.sqlite")
    r = snapshot.rpo(primary_db, restored_db)
    emit(step="2_restore_snapshot", target=target, backend=backend,
         rpo_seconds=r["rpo_seconds"], docs_lost=r["docs_lost"],
         embed_model_version=meta.get("embed_model_version"))

    # 3_scale_pool — warm -> full
    pool_file = pathlib.Path(f"state/region-{target}/pool_state")
    pool_file.parent.mkdir(parents=True, exist_ok=True)
    pool_file.write_text("full")
    emit(step="3_scale_pool", target=target, pool_state="full")

    # 4_wait_ready — poll /readyz tới khi 200, hoặc hết `wait` giây thì ABORT
    poll = min(0.5, max(0.05, wait / 10)) if wait > 0 else 0.5
    start = time.time()
    ready, reason = False, None
    while time.time() - start <= wait:
        try:
            rr = httpx.get(f"{URL[target]}/readyz", timeout=2.0)
            if rr.status_code == 200:
                ready = True
                break
            reason = rr.json().get("reasons")
        except Exception as e:
            reason = type(e).__name__
        time.sleep(poll)
    waited_s = round(time.time() - start, 2)
    emit(step="4_wait_ready", target=target, ok=ready, waited_s=waited_s, reason=reason)
    if not ready:
        emit(step="abort", target=target, reason="target khong ready trong wait window -> khong cutover")
        return {"ok": False, "step_failed": "4_wait_ready", "reason": reason, "waited_s": waited_s}

    # 5_dns_cutover — chỉ đổi DNS SAU KHI target đã ready
    active_file = pathlib.Path("edge/active_region")
    active_file.write_text(target)
    emit(step="5_dns_cutover", target=target)

    return {"ok": True, "target": target, "primary": primary,
            "rpo_seconds": r["rpo_seconds"], "docs_lost": r["docs_lost"],
            "embed_model_version": meta.get("embed_model_version"), "waited_s": waited_s}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
