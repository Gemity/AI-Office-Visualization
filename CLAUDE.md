# CLAUDE.md

Hướng dẫn cho Claude Code khi làm việc trong repo này.

## Dự án

**AI Project Office** — không gian làm việc ảo cho *một project tại một thời điểm*. Owner chỉ chat với **một Project Orchestrator Agent**; Orchestrator lập kế hoạch, giao task và điều phối nhiều worker agents (Claude, Codex, Suno, Tripo, custom). Owner quan sát trạng thái, cuộc họp, DM giữa agent, task, artifact và quyết định qua Virtual Office.

Nguyên tắc: **One Project → One Active Orchestrator → Many Workers → One Observable Workspace.**

- Spec gốc: `docs/AI_Project_Office_Spec_v2.docx` (nguồn chính thức)
- Bản text để đọc nhanh: `docs/AI_Project_Office_Spec_v2.md` — trích từ .docx; nếu .docx được cập nhật thì phải trích lại.

## Trạng thái hiện tại

Repo mới khởi tạo; mới có frontend fork nguyên trạng trong `apps/web/`, chưa có backend. Spec ở trạng thái *Proposed baseline* (Phase 0: chốt schema, adapter contract, project-scoped auth, prototype event stream). Chưa có lệnh build/test/lint — cập nhật mục này khi tooling được thêm vào.

Đã chốt (xem `docs/adr/0001-web-stack-fork-pixel-office.md`):
- **Toàn bộ là web**, không dùng Unity (dù repo nằm trong thư mục `UnityProject/`). Không tạo asset/scene/script Unity.
- **UI base: fork Pixel Office** (https://github.com/basriayaz/pixel-office, MIT) vào `apps/web/` + `packages/office-renderer/`. Giữ `LICENSE`/copyright của upstream và ghi upstream + commit gốc trong `apps/web/UPSTREAM.md`. Upstream theo mô hình "thuê nhân viên", chưa có project scoping hay một Orchestrator duy nhất — khi fork phải sửa theo BR-01/02/04, không mang mô hình đó sang.
- **Chỉ fork frontend** (`web/` upstream → `apps/web/`, đã copy nguyên trạng, commit `4ebfdc7`). **Backend là FastAPI** trong `apps/api/`, viết mới — không dùng server Node/Express của upstream. Frontend hiện còn gọi `/api/*` của upstream, cần chuyển sang API spec §11. Ghi mọi thay đổi so với upstream vào `apps/web/UPSTREAM.md`.

Các quyết định còn mở (spec §18) — **không tự chốt**, hỏi Owner hoặc ghi ADR trong `docs/adr/`:
- Runtime: LangGraph + durable execution hay workflow engine chuyên dụng.
- Tích hợp Claude/Codex qua SDK/API hay CLI.
- API/quota chính thức của Suno/Tripo (dùng mock cho tới khi xác minh).
- Policy mặc định: transcript riêng, retention, ngân sách, quyền can thiệp.
- Deploy local-first, server 24/7 hay hybrid.

## Cấu trúc repo đề xuất (spec §17)

```
apps/web/                   # Project UI + Pixel Office
apps/api/                   # FastAPI + WebSocket
services/orchestrator/      # Workflow runtime
services/agent-workers/     # Worker execution/sandbox
packages/contracts/         # OpenAPI + event JSON schemas
packages/agent-adapters/    # Claude/Codex/Suno/Tripo/custom
packages/office-renderer/   # UI projection from events
infra/docker/               # Local Compose
docs/adr/                   # Architecture decisions
tests/e2e/                  # Cross-agent workflows
```

Stack dự kiến: FastAPI + WebSocket, PostgreSQL, Redis Streams, Object Storage, OpenTelemetry/Langfuse.

## Quy tắc nghiệp vụ bắt buộc (không được vi phạm khi viết code)

- **BR-01** Tối đa một active Orchestrator/project; thay thế phải có version + audit (`orchestrator_assignments`).
- **BR-02** Chỉ Orchestrator nói chuyện với Owner; worker không tạo kênh mới tới Owner.
- **BR-03** Giao việc theo *capabilities* + policy. Không hardcode tên provider (Claude/Codex/Suno/Tripo) vào role hay logic điều phối.
- **BR-04** Mọi event/task/conversation/artifact có `project_id`; kiểm tra quyền **ở backend**. Mọi query phải lọc theo project — không có đường đọc/ghi cross-project.
- **BR-05** DM và meeting giữa agent theo policy; ghi transcript nếu provider cung cấp.
- **BR-06** Owner gửi pause/resume/cancel/redirect; runtime xác nhận trạng thái **thực tế**.
- **BR-07** Hành động rủi ro cao (deploy production, thanh toán, xóa dữ liệu, xuất dữ liệu nhạy cảm) cần approval có expiry, scope, approver, audit.
- **BR-08** Worker chạy song song; Orchestrator **không** phải nơi lưu trạng thái duy nhất — trạng thái bền vững thuộc Runtime.
- **BR-09** Không hiển thị "live"/"complete" khi chưa có event xác nhận. UI phân biệt `requested` vs `confirmed` (vd. "cancel requested").

## Nguyên tắc kiến trúc

- **Orchestrator ≠ Runtime.** Orchestrator là agent ra quyết định chiến lược; Runtime lo lập lịch, checkpoint, retry/backoff, timeout, idempotency, dead-letter, policy enforcement.
- **Visualization tách khỏi execution.** Office/UI chỉ là *projection từ event stream*; renderer 2D và 3D (P2) dùng chung event API. Không giả lập trạng thái khi thiếu event, không bịa dữ liệu.
- **Event protocol** (§10): mỗi event có `event_id`, `project_id`, `sequence`, `type`, `actor_id`, `trace_id`, `occurred_at`, `payload`. Delivery at-least-once; consumer idempotent (de-dup bằng `event_id`); replay bằng `sequence` (`GET /projects/{id}/events?after_seq=N`). Nội dung lớn/nhạy cảm để ở object storage qua `content_ref`, không nhét vào payload.
- **Agent Adapter contract** (§7): `describeCapabilities`, `invoke`, `getStatus`, tùy chọn `cancel`, `subscribeEvents`, `collectArtifacts`. Manifest khai báo capabilities, invocation_mode (sync/stream/async_job), permissions, artifact types, cost_model, observability_level. Suno/Tripo là async job workers — trạng thái dựa trên job status thật. Không scraping trái ToS.
- **API** (§11): mọi write endpoint nhận idempotency key; response có `request_id`, operation status, lỗi có mã. WebSocket phải xác thực và giới hạn theo project.
- **Data model** (§9): bảng gắn project dùng composite index `(project_id, created_at|status)`.
- **Bảo mật:** không hiển thị chain-of-thought/hidden reasoning — chỉ transcript, tool calls, summaries, events. Log redaction, secret vault, coding workers chạy trong sandbox.

## Ngoài phạm vi MVP

Mô phỏng công ty/CEO/phòng ban, HR/payroll; workflow cứng cho mọi loại project; bắt buộc tích hợp Suno/Tripo thật; agent tự deploy/thanh toán/công bố khi chưa duyệt.

## Kiểm thử nghiệm thu

Acceptance criteria AC-01 … AC-10 ở spec §15 — dùng làm nguồn cho test e2e trong `tests/e2e/`. NFR ở §14 (p95 event→UI < 2s, ≥5 worker song song/project, restart không mất task/event đã commit).

## Quy ước làm việc

- Tài liệu và trao đổi bằng tiếng Việt; tên định danh trong code, schema, event type bằng tiếng Anh theo spec.
- Quyết định kiến trúc mới → viết ADR trong `docs/adr/`.
- Thay đổi schema/event/API phải cập nhật `packages/contracts/` trước, code sau.
