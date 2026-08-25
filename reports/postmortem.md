# Postmortem — DR Drill Lab 23 (2026-08-25, drill thật)

Theo đúng template §4 "Sau Failover: Blameless Postmortem". Blameless: câu hỏi là
"hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline (mọi dòng phải có evidence path:line)

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 2026-08-25T07:33:21.774 | outage bắt đầu (`kill --region a --mode netblock --mock`) | `chaos/chaos-events.jsonl:1` |
| 2026-08-25T07:33:21.780 | user đầu tiên bị ảnh hưởng (`ok:false`, `ReadTimeout`) | `reports/drill-2-withdr.jsonl:25` |
| 2026-08-25T07:33:40.983 | health check alert — `region:a` → `UNHEALTHY` sau 3 lần fail liên tiếp | `reports/health-events.jsonl:3` |
| 2026-08-25T07:33:41.879 | operator/automation xác nhận + mở incident (`--auto`, không hỏi tay) | `reports/runbook-run.jsonl:2` |
| 2026-08-25T07:33:48.436 | DNS/LB cutover sang Region B | `reports/failover-events.jsonl:5` |
| 2026-08-25T07:33:50.947 | resolved — request đầu tiên OK từ Region B (`served_by:"b"`) | `reports/drill-2-withdr.jsonl:39` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: `29.2s` · gap: `270.8s` (dư rất nhiều, PASS thoải mái)
- RPO mục tiêu: 300s · đo được: `6.0s` (`3` doc bị mất) · gap: `294.0s` (PASS thoải mái)
- **Bước tốn nhiều giây nhất:** `health-check detect floor` (19.2s / 29.2s tổng, ~66%) —
  vì ngưỡng chống-flap `interval=5s × threshold=3` là bước bảo thủ nhất trong toàn bộ
  chuỗi (§4 Anti-Patterns: thà chậm phát hiện còn hơn flip-flop qua lại giữa 2 region).
  Xem chi tiết từng thành phần ở `reports/rto-evidence.md` §3.

## 3. Root cause (5 whys)

Không phải "vì tôi chạy chaos script". Câu hỏi: *nếu đây là outage thật, bước nào
trong runbook của tôi sẽ thất bại?*

1. **Vì sao user thấy lỗi trong 29.2s?** — Vì toàn bộ chuỗi phát hiện → khôi phục →
   cutover mất từng đó thời gian cộng dồn (detect 19.2s + restore 1.0s + warm-up 6.5s +
   DNS TTL 2.5s).
2. **Vì sao riêng bước phát hiện đã chiếm 19.2s (66% RTO)?** — Vì ngưỡng chống-flap
   (`interval=5s, threshold=3`) được chọn bảo thủ có chủ đích để không flip trạng thái
   vì 1 lần fail thoáng qua.
3. **Vì sao ngưỡng phải bảo thủ như vậy?** — Vì chỉ có **một** `dr/health_checker.py`
   chạy độc lập, không có cơ chế đồng thuận (quorum) nào khác xác nhận outage — nếu hạ
   ngưỡng để nhanh hơn, một lần mất gói tin/GC pause ngẫu nhiên cũng đủ trigger failover
   giả, gây flapping 2 chiều.
4. **Vì sao không có health-checker dự phòng/đa nguồn?** — Vì đây là lab quy mô 1 máy,
   1 process giám sát là đủ để minh hoạ khái niệm; một hệ thống thật cần nhiều prober
   độc lập (nhiều AZ) bỏ phiếu đa số để tăng tốc phát hiện mà không tăng false-positive.
5. **Vì sao chưa khắc phục điều đó?** — Ngoài phạm vi lab 2 giờ này; đây là action item
   thật cho một đợt hardening sản xuất (xem §4 bên dưới). Thêm nữa: nếu chính process
   `dr/health_checker.py` chết (không phải region nó theo dõi), **không ai báo động cả**
   — nó chạy tách biệt khỏi `serving/`, không tự giám sát chính nó (Reflection Q2 của
   GUIDE.md nêu đúng lỗ hổng này).

## 4. Action items (có owner + deadline)

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Chạy ≥2 `health_checker.py` độc lập (giả lập multi-AZ), quyết định UNHEALTHY theo đa số để có thể hạ `threshold` xuống 2 mà không tăng rủi ro flap | Platform/Infra | +2 tuần | ước tính giảm detect floor ~5–7s |
| 2 | Giảm chu kỳ `state/replicate.py --every` từ 30s xuống 10s | Data/Infra | +1 tuần | giảm RPO trung bình ~20s (kỳ vọng, phụ thuộc traffic) |
| 3 | Thêm circuit breaker tự động (giới hạn N lần failover/giờ) cho `dr/runbook.py` trước khi cân nhắc bật `--auto` ngoài CI | SRE | +1 quý | không đổi RTO/RPO trực tiếp, giảm rủi ro flapping khi mở rộng tự động hoá |
| 4 | Thêm alert riêng khi chính process `dr/health_checker.py` chết (dead-man's switch) | SRE | +2 tuần | không đổi RTO đo được, nhưng chặn kịch bản "không ai biết outage" hoàn toàn |

## 5. Ba câu hỏi bắt buộc trả lời

1. **`interval × threshold` = 5s × 3 = 15s.** Đây là **51.4%** của RTO đo được (15s / 29.2s).
   (Thời gian phát hiện *thực tế* là 19.2s vì mỗi lần probe thất bại còn cộng thêm
   `ReadTimeout` ~2s trước khi tính là 1 lần fail — xem `reports/rto-evidence.md` §3.)
2. **Nếu hạ `interval` xuống 1s:** floor lý thuyết còn 1×3=3s; ước tính detect thực tế
   giảm còn ~9s (thay vì 19.2s) → RTO ước tính còn ~19s, **giảm khoảng 10s**. Cái giá
   phải trả: tần suất poll `/readyz` của cả 2 region tăng gấp 5 lần (tải hệ thống), và
   quan trọng hơn — cửa sổ "1 lần fail thoáng qua" hẹp lại nhiều, nên một đợt nghẽn mạng
   ngắn hay GC pause ngẫu nhiên cũng đủ 3 lần liên tiếp để trigger failover giả
   (flapping 2 chiều, đúng Anti-Pattern §4 cảnh báo).
3. **Nếu outage kéo dài 6 giờ và Region A mất dữ liệu vĩnh viễn:** `docs_lost=3` trong
   drill này CHỈ nhỏ vì tôi cố tình cho ingest chạy chậm (0.5 doc/giây) và replicate mỗi
   30s trong một drill ngắn. Với traffic thật, cùng chu kỳ replicate 30s đó nghĩa là
   **mọi document được ghi trong ≤30 giây cuối trước khi A chết vĩnh viễn sẽ biến mất
   không dấu vết** — với khách hàng, đó không phải "3 con số" mà là những tương tác thật
   (câu hỏi hoá đơn, cập nhật ticket, đơn hàng) lặng lẽ mất đi và có thể phải yêu cầu lại
   từ đầu — với dữ liệu nhạy cảm về thời gian (hoá đơn, đơn hàng), đây là chi phí thật về
   niềm tin, không chỉ là một con số kỹ thuật.
