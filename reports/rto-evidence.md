# RTO/RPO Evidence — Lab 23 (số thật, drill chạy ngày 2026-08-25)

Quy tắc duy nhất: mỗi con số ở đây phải trỏ được về **một dòng log thật**
(`đường/dẫn.jsonl:số_dòng`). `pytest tests/test_rto_evidence.py` sẽ mở từng file ra kiểm tra.
Con số không có evidence = trượt, bất kể các phần khác.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | 2026-08-25T07:35:59 | chaos kill (`--mode netblock --mock`) | `chaos/chaos-events.jsonl:3` |
| Request fail đầu tiên | +2.0s | dòng `ok:false` đầu tiên với `ts >= t_outage` (`tools/measure_rto.py` chỉ tính request có `ts` từ lúc kill trở đi) | `reports/drill-1-nodr.jsonl:18` |
| Request thành công sau đó | không có | không có dòng `ok:true` nào sau t_outage trong 32 request | `reports/measure-drill-1.json` |
| RTO | `NO_RECOVERY` | `python3 tools/measure_rto.py --loadgen reports/drill-1-nodr.jsonl --target-rto 300` | `reports/measure-drill-1.json` |

15/32 request thất bại (46.9%). Không có `dr/health_checker.py` hay `dr/failover.py` chạy trong drill này — đúng ý đồ của Bước 2: chứng minh hệ thống **không tự phục hồi** khi chưa có DR.

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0.0 | `action:kill` | `chaos/chaos-events.jsonl:1` |
| User thấy lỗi đầu tiên | 0.0 | dòng `ok:false` đầu, `ts >= t_outage` | `reports/drill-2-withdr.jsonl:25` |
| Health check phát hiện | 19.2 | `to:UNHEALTHY, region:a` | `reports/health-events.jsonl:3` |
| Snapshot restore xong | 19.2 (log lúc +20.2, xem giải thích §3) | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region phụ ready | 26.7 | `step:4_wait_ready, ok:true, waited_s:6.5` | `reports/failover-events.jsonl:4` |
| DNS cutover | 26.7 | `step:5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **29.2** | dòng `ok:true` đầu sau lỗi, `served_by:"b"` | `reports/drill-2-withdr.jsonl:39` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | 29.2s | 300s (5 phút) | **PASS** |
| RPO — Vector DB | 6.0s / 3 doc | 300s (5 phút) | **PASS** |

Output máy đo đầy đủ: `reports/measure-drill-2.json` — `"valid":true`, `"warnings":[]`,
`"recovered_by_region":"b"` (Region B thật sự phục vụ request, không phải A "sống lại giả").
RPO đo được (`rpo_seconds:6.0, docs_lost:3`) đến từ `2_restore_snapshot`
(`reports/failover-events.jsonl:2`) — bằng chênh lệch `latest_doc_ts` giữa vector DB thật
của Region A (272 doc tại thời điểm restore) và bản snapshot gần nhất mà `state/replicate.py`
đã `put` (mỗi 30s). **RPO phụ thuộc đúng vào lúc replicate cycle cuối cùng rơi vào đâu so với
thời điểm restore — số này được kỳ vọng khác nhau giữa các lần chạy**, không phải hằng số.

## 3. RTO của tôi gồm những gì

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---|---|---|
| Health-check detect floor | 19.2s | Floor lý thuyết = `interval_s(5.0) × threshold(3) = 15.0s` (`reports/health-events.jsonl:3`). Đo thực tế 19.2s vì mỗi vòng poll còn cộng thêm ~2s `ReadTimeout` (netblock làm request treo tới hết `timeout`) trước khi 1 lần fail được tính | Hạ `interval`/`timeout` — đổi lại rủi ro flapping (xem postmortem §5, câu 2) |
| Snapshot restore | 1.0s | Từ lúc health check phát hiện (`reports/health-events.jsonl:3`, +19.2s) tới khi `2_restore_snapshot` ghi log xong (`reports/failover-events.jsonl:2`, +20.2s) — gồm cả bước `dr/runbook.py` đọc lại log health-checker + `1_verify_target`. Bản thân copy file (backend `fs`) gần như tức thời | Đã tối ưu cho lab; backend `minio` thật sẽ chậm hơn (stretch goal 1) |
| GPU pool warm-up | 6.5s | `waited_s` ở `step:4_wait_ready` (`reports/failover-events.jsonl:4`) — đúng bằng `WARMUP_SECONDS` mặc định của `serving/app.py` cộng chút overhead polling | Giảm `WARMUP_SECONDS` — đổi lại rủi ro model/pool chưa sẵn sàng thật đã nhận traffic |
| DNS/LB TTL cache | 2.5s | `t_recovered(29.2s) − t_cutover(26.7s)` = request đầu tiên sau cutover phải đợi hết phần còn lại của `EDGE_TTL_SECONDS=5s` mà `edge/proxy.py` đang cache | Hạ `EDGE_TTL_SECONDS` — đổi lại tải đọc file/API "DNS" tăng lên |
| **Tổng** | **29.2s** | 19.2 + 1.0 + 6.5 + 2.5 = 29.2s, khớp `rto_measured_s` trong `reports/measure-drill-2.json` | |
