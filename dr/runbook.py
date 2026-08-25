"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
HEALTH_LOG = pathlib.Path("reports/health-events.jsonl")
CHAOS_LOG = pathlib.Path("chaos/chaos-events.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Ghi 1 dòng {ts, iso, step, name, ...} vào LOG."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    line = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "step": n, "name": name, **kw}
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")
    print(json.dumps(line))
    return line


def confirm(auto: bool, msg: str) -> bool:
    """auto=True -> True (CI/chấm điểm); ngược lại hỏi operator y/N."""
    if auto:
        return True
    ans = input(f"{msg} [y/N] ").strip().lower()
    return ans == "y"


def _probe(region: str, timeout: float = 1.0):
    try:
        r = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
        body = r.json()
        return bool(body.get("ready")), body.get("reasons", [])
    except Exception as e:
        return False, [type(e).__name__]


def _last_kill_ts(region: str):
    """t_outage thật, đọc từ chaos-events.jsonl (cùng nguồn measure_rto.py dùng)."""
    if not CHAOS_LOG.exists():
        return None
    ts = None
    for line in CHAOS_LOG.read_text().splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("action") == "kill" and e.get("region") == region:
            ts = e["ts"]
    return ts


def _wait_for_health_detect(primary: str, since_ts: float, wait: float, poll: float = 1.0):
    """Đợi dòng state_change UNHEALTHY cho `primary` xuất hiện trong
    reports/health-events.jsonl — do dr/health_checker.py (đang chạy song song) ghi ra.

    KHÔNG được tự probe nhanh hơn rồi cutover trước khi hệ thống giám sát chính thức
    đã phát hiện outage: t_cutover < t_detect nghĩa là bạn đo bằng tay, không phải
    bằng automation (measure_rto.py sẽ gắn cờ INVALID/warning cho việc này)."""
    start = time.time()
    while time.time() - start <= wait:
        if HEALTH_LOG.exists():
            for line in HEALTH_LOG.read_text().splitlines():
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (e.get("event") == "state_change" and e.get("to") == "UNHEALTHY"
                        and e.get("region") == primary and e.get("ts", 0) >= since_ts):
                    return e
        time.sleep(poll)
    return None


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """7 bước runbook §4 "Region Chính Down"."""
    t_start = time.time()

    # 1 xac_nhan_outage — UU TIEN cho health_checker.py (dang chay song song, co
    # anti-flap threshold) tu phat hien va ghi vao reports/health-events.jsonl; runbook
    # CHI xac nhan outage sau khi thay dong UNHEALTHY do -> khong bao gio cutover som
    # hon he thong giam sat chinh thuc (tranh t_cutover < t_detect).
    # Fallback: neu health_checker khong chay song song (vd goi runbook.py doc lap),
    # tu probe nhieu lan de van con dung duoc mot minh.
    since = _last_kill_ts(primary)
    if since is None:
        since = t_start
    detect_event = _wait_for_health_detect(primary, since, wait=45.0)
    if detect_event is not None:
        checks = [detect_event]
        outage_confirmed = True
        source = "health_checker_log"
    else:
        checks, fails = [], 0
        for i in range(3):
            ready, reasons = _probe(primary)
            checks.append({"attempt": i + 1, "ready": ready, "reasons": reasons})
            if not ready:
                fails += 1
            if i < 2:
                time.sleep(1.0)
        outage_confirmed = fails >= 2
        source = "self_probe_fallback"
    step(1, "xac_nhan_outage", primary=primary, source=source, checks=checks,
         outage_confirmed=outage_confirmed)
    if not outage_confirmed:
        step("abort", "khong_xac_nhan_duoc_outage", primary=primary)
        return {"ok": False, "reason": "outage_khong_duoc_xac_nhan"}

    # 2 thong_bao_incident — moc "operator biet tin", luon SAU t_outage that
    t_incident = time.time()
    step(2, "thong_bao_incident", primary=primary, t_operator_biet=t_incident,
         ghi_chu="ts nay phai sau t_outage that trong chaos/chaos-events.jsonl")

    # Ban tu dong: mac dinh phai hoi confirm truoc khi cutover
    if not confirm(auto, f"Xac nhan failover tu {primary} sang {target}?"):
        step("abort", "operator_tu_choi_cutover", primary=primary, target=target)
        return {"ok": False, "reason": "operator_tu_choi"}

    # 3 scale_gpu_pool — goi failover.failover() DUY NHAT 1 LAN. Ham nay tu lam
    # 5 buoc con va tu ghi log rieng vao reports/failover-events.jsonl.
    fo_result = fo.failover(target, backend, wait=60.0)
    step(3, "scale_gpu_pool", target=target, backend=backend, failover_result=fo_result)

    # 4 verify_state_replica — CHI DOC lai ket qua buoc 3, khong goi lai failover
    step(4, "verify_state_replica", target=target,
         docs_lost=fo_result.get("docs_lost"), rpo_seconds=fo_result.get("rpo_seconds"),
         embed_model_version=fo_result.get("embed_model_version"))

    # 5 dns_cutover — cung chi doc lai
    step(5, "dns_cutover", target=target, ok=fo_result.get("ok"))

    if not fo_result.get("ok"):
        step("abort", "failover_that_bai_khong_cutover", target=target,
             reason=fo_result.get("reason"))
        return {"ok": False, "reason": "failover_that_bai", "failover_result": fo_result}

    # 6 verify_golden_signals — 10 request that vao region phu
    lat_ms, errs = [], 0
    for _ in range(10):
        t0 = time.time()
        try:
            r = httpx.get(f"{URL[target]}/v1/infer", timeout=5.0)
            lat_ms.append((time.time() - t0) * 1000)
            if r.status_code != 200 or not r.json().get("answer"):
                errs += 1
        except Exception:
            lat_ms.append((time.time() - t0) * 1000)
            errs += 1
    lat_sorted = sorted(lat_ms)
    p95 = lat_sorted[max(0, int(len(lat_sorted) * 0.95) - 1)] if lat_sorted else None
    step(6, "verify_golden_signals", target=target, requests=len(lat_ms),
         p95_latency_ms=round(p95, 1) if p95 is not None else None,
         error_rate=round(errs / len(lat_ms), 2) if lat_ms else None)

    # 7 post_incident
    elapsed_s = round(time.time() - t_start, 2)
    step(7, "post_incident", elapsed_s=elapsed_s,
         measure_cmd="python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300")

    return {"ok": True, "elapsed_s": elapsed_s, "failover_result": fo_result}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
