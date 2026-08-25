# Runbook 1 trang — Region chính down

Runbook phải chạy được lúc 3h sáng bởi người KHÔNG viết nó. Mỗi bước: lệnh copy-paste
được + cách biết bước đó xong.

**Lệnh chính (1 lệnh, chạy hết bước 1→7):**

```bash
python3 dr/runbook.py --primary a --target b --backend fs
```

Mặc định lệnh này DỪNG LẠI sau bước 2 và hỏi `Xac nhan failover tu a sang b? [y/N]` —
đây là "alert + 1-click confirm" bán tự động của §4, không phải full-auto (full-auto
không circuit breaker → 2 region flap qua lại). Chỉ dùng `--auto` cho CI/chấm điểm,
KHÔNG dùng khi thao tác thật lúc 3h sáng — người trực phải tự mắt xác nhận trước khi
cutover.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | Tự động: `dr/runbook.py` probe `/readyz` của region chính 3 lần, cách nhau 1s. Kiểm tra chéo thủ công: `python3 chaos/kill_region.py status` | Dòng `{"step":1,"outage_confirmed":true}` trong `reports/runbook-run.jsonl` (cần ≥2/3 probe fail — 1 probe fail không tính); thủ công thấy `a.alive:false` hoặc `a.ready:false` | on-call |
| 2 | Mở incident + bấm giờ RTO | Tự động, không cần lệnh riêng | Dòng `{"step":2,"name":"thong_bao_incident","t_operator_biet":<ts>}` trong `reports/runbook-run.jsonl`, với `<ts>` LUÔN LỚN HƠN `t_outage` (dòng `action:kill` trong `chaos/chaos-events.jsonl`) | on-call |
| 3 | Restore state ở region phụ | Tự động qua `failover.failover()` (bước con `2_restore_snapshot`). Thủ công nếu automation hỏng: `python3 state/snapshot.py get --region b --backend fs` | Dòng `{"step":"2_restore_snapshot", ...}` trong `reports/failover-events.jsonl` có `rpo_seconds`, `docs_lost`, `embed_model_version` đều khác `null` | infra on-call |
| 4 | Scale pool warm→full | Tự động (bước con `3_scale_pool` + `4_wait_ready`). Thủ công: `echo full > state/region-b/pool_state` rồi tự poll `/readyz` | `curl localhost:8002/readyz` trả **200** (`"ready":true`); dòng `{"step":"4_wait_ready","ok":true}` trong `reports/failover-events.jsonl` | infra on-call |
| 5 | DNS/LB cutover | Tự động (bước con `5_dns_cutover`), CHỈ chạy sau khi bước 4 `ok:true`. Thủ công: `echo b > edge/active_region` | `curl localhost:8080/edge/state` → `"active_region":"b"`; dòng `{"step":"5_dns_cutover"}` trong `reports/failover-events.jsonl` | on-call |
| 6 | Verify golden signals | Tự động: 10 request thật tới `/v1/infer` của region b | Dòng `{"step":6,"name":"verify_golden_signals"}` trong `reports/runbook-run.jsonl` — mục tiêu `error_rate == 0` và `p95_latency_ms < 500`ms (ngưỡng nội bộ cho mock API này; điền lại số thật của bạn sau khi đo) | on-call |
| 7 | Đo RTO + postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | Output có `"rto_verdict":"PASS"` và `"valid":true` | on-call → chuyển cho người viết `reports/postmortem.md` |

## Rollback (failover ngược) — trả traffic về Region A

**Điều kiện bắt buộc CẢ BA, không thiếu cái nào:**

1. Region A xác nhận `ready:true` **liên tục nhiều lần** qua `curl localhost:8001/readyz` — không dựa vào 1 lần đọc (cùng logic chống flap đã dùng ở bước 1).
2. Đã đồng bộ ngược dữ liệu: chạy `python3 state/snapshot.py put --region b --backend fs` rồi `python3 state/snapshot.py get --region a --backend fs` để A có toàn bộ dữ liệu B ghi được trong lúc B làm primary — nếu bỏ qua bước này, rollback sẽ tạo RPO mới theo chiều ngược lại.
3. Đã quan sát B ổn định tối thiểu 15 phút không flap kể từ lúc cutover (không có state_change UNHEALTHY→HEALTHY→UNHEALTHY lặp lại trong `reports/health-events.jsonl`) — đây là circuit breaker thủ công mà §4 Anti-Patterns yêu cầu để tránh 2 region flap qua lại liên tục.

**Ai có quyền quyết định rollback:** Incident Commander (không phải on-call kỹ thuật
đơn lẻ) — vì rollback nghĩa là chấp nhận một cửa sổ downtime/RPO thứ hai, đó là quyết
định rủi ro-kinh doanh chứ không chỉ là thao tác kỹ thuật. On-call thực thi lệnh,
nhưng người ký duyệt phải là Incident Commander, ghi lại trong incident channel trước
khi chạy `python3 dr/runbook.py --primary b --target a --backend fs`.
