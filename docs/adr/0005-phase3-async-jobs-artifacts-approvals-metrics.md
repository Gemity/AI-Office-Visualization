# ADR-0005: Phase 3 — Adapter async job, artifact, approval, metrics

- Trạng thái: Accepted
- Ngày: 2026-10-28
- Liên quan: Spec v2.0 §7, §9, §12, §13, §15 (AC-08, AC-09), §18, BR-03, BR-07, BR-09; ADR-0003, ADR-0004

## Quyết định

1. **Worker dạng job (Suno/Tripo) dùng nền chung `AsyncJobAdapter`** (`agent_adapters/jobs.py`) với 3 hàm submit / poll / cancel. Chưa tích hợp provider thật vì API/quota chưa xác minh (§18). Hai mock `mock-suno` (capabilities `audio, async_job`) và `mock-tripo` (`3d, async_job`) giả lập API job từ xa ngay trong tiến trình và trả về file thật: một giai điệu WAV và một mesh OBJ sinh từ prompt.
2. **Approval là cổng chặn trong dispatcher**, không phụ thuộc vào việc LLM có tự khai báo hay không. Task cần duyệt khi:
   - Orchestrator gắn `risk_actions` cho task, hoặc
   - worker được chọn khai báo quyền rủi ro cao trong `required_permissions`.
3. **Metrics chỉ cộng số liệu đã ghi nhận.** Run không báo chi phí được đếm riêng, không coi là $0.

## Thiết kế

**Async job.**
- Job id của provider được ghi vào `agent_runs.provider_ref` ngay khi job được nhận. `agent_runs.progress` là đúng con số provider báo; mock báo theo bước 10%.
- Hủy run (can thiệp, timeout) sẽ gọi `cancel` tới provider trước khi run được ghi là `cancelled`.
- File output ghi vào workdir của task (chỉ lấy basename, không tin tên file do provider đặt) và thành artifact như mọi adapter khác.
- Job lỗi có `retryable` nên đi qua đúng chính sách retry.
- Không có event type mới (spec §10 cố định 16 loại). UI đọc lại tiến độ qua REST mỗi 2 giây khi đang có job chạy.

**Contract.** `office_contracts.policy`: `RiskAction` (`deploy_production`, `payment`, `delete_data`, `export_sensitive_data`) và `ApprovalScope` (sinh ra `schemas/approval-scope.schema.json`).

**Approval (`office_api/approvals.py`).**
- Dispatcher chọn worker → `_approval_gate`:
  - Không có rủi ro → chạy.
  - Có approval `approved`, chưa hết hạn, chưa dùng, và scope khớp hoàn toàn → chạy, rồi ghi `approvals.run_id`. Approval chỉ dùng một lần, nên retry cần duyệt lại. Scope khớp nghĩa là cùng task, cùng actions, cùng worker và cùng `sha256` của chỉ dẫn.
  - Còn lại → tạo approval `pending` với hạn `approval_ttl_s` (mặc định 3600s, project ghi đè được bằng `config.approval_ttl_s`), task chuyển `awaiting_approval`, phát event `approval.requested`. Adapter không hề được gọi.
- Quyết định `POST /projects/{id}/approvals/{aid}/decision` chỉ dành cho owner của project (observer bị 403, project khác bị 404):
  - approve → task về `pending`, dispatcher chờ đúng worker đã được duyệt;
  - reject → task `blocked` (`approval_rejected`) và lan truyền xuống task phụ thuộc.
- Dispatcher quét approval quá hạn → `expired`; nếu task đang chờ thì chuyển `blocked` (`approval_expired`). Quyết định sau khi hết hạn hoặc đã có quyết định → 409.
- Can thiệp pause/cancel/redirect lên task đang chờ duyệt sẽ rút lại approval (`cancelled`). Redirect đổi chỉ dẫn nên phải xin duyệt lại.
- Event `approval.resolved` có `status` là `approved | rejected | expired | cancelled`. Audit gồm `approver_id`, `comment`, `resolved_at`, `run_id`.

**Artifact.**
- Bảng `artifacts` thêm `name`, `mime_type`, `size_bytes`. `version` tăng khi cùng một file của task được tạo lại.
- `GET /artifacts` có `agent_id` để biết artifact sinh ra từ run nào.
- `GET /artifacts/{aid}/content`:
  - chỉ trả file nằm trong workspace của chính project đó (BR-04);
  - kiểm `sha256` khớp với phiên bản đã ghi (409 nếu file bị sửa, 410 nếu file đã mất);
  - header `Content-Security-Policy: sandbox` và `nosniff`, vì output của agent không đáng tin.
- UI chỉ render inline audio và ảnh. OBJ hiển thị bằng wireframe trên canvas, code/văn bản hiển thị dạng text (kể cả HTML), các loại còn lại chỉ cho tải về.

**Metrics.** `GET /projects/{id}/metrics` trả về:
- chi phí theo project và theo agent;
- run theo trạng thái và theo mục đích, số lần retry, số `run.failed`;
- task, approval, can thiệp theo trạng thái; artifact theo loại; số event;
- thời gian bận của từng agent.

UI hiển thị các số này ở tab "Chi phí" (Audit/Cost View §12).

## Đã kiểm chứng

- 63 test, trong đó 11 test Phase 3:
  - AC-08: job nhạc + 3D trả trạng thái, `provider_ref`, tiến độ và artifact đọc được;
  - hủy job tới tận provider; job lỗi được retry;
  - AC-09: task rủi ro bị chặn tới khi duyệt; reject; hết hạn; quyền rủi ro khai báo ở worker; chỉ owner được duyệt, cross-project bị 404; rút approval khi redirect hoặc cancel;
  - metrics; artifact theo project và có checksum.
- UI kiểm tra bằng Chrome headless trên server thật:
  - tiến độ job trên roster và task;
  - thẻ duyệt trong Chat → duyệt → deploy chạy;
  - xem mesh OBJ, file HTML chỉ hiện dạng text;
  - bảng chi phí. Không có lỗi console.

## Giới hạn / việc sau

- Chưa có adapter Suno/Tripo thật. Khi có API chính thức chỉ cần viết `_submit/_poll/_cancel_job`.
- Khởi động lại runtime khi job đang chạy sẽ fail run (`interrupted`) và submit lại, chưa gắn lại vào job cũ qua `provider_ref`. Với provider thật tính tiền theo job, nên làm phần gắn lại này ở Phase 4.
- Chưa có ngân sách per-project/per-agent và circuit breaker (§13). Metrics mới dừng ở mức quan sát.
- Approval do runtime tạo (`requester_id = system`); Orchestrator chưa tự xin duyệt cho hành động ngoài task.
- Schema đổi (task, artifact, agent_run, approval): xóa `office.db` dev cũ trước khi chạy.
