# CLAUDE.md

Hướng dẫn cho Claude Code khi làm việc trong repo này.

## Dự án

**AI Project Office** — không gian làm việc ảo cho *một project tại một thời điểm*. Owner chỉ chat với **một Project Orchestrator Agent**; Orchestrator lập kế hoạch, giao task và điều phối nhiều worker agents (Claude, Codex, Suno, Tripo, custom). Owner quan sát trạng thái, cuộc họp, DM giữa agent, task, artifact và quyết định qua Virtual Office.

Nguyên tắc: **One Project → One Active Orchestrator → Many Workers → One Observable Workspace.**

- Spec gốc: `docs/AI_Project_Office_Spec_v2.docx` (nguồn chính thức)
- Bản text để đọc nhanh: `docs/AI_Project_Office_Spec_v2.md` — trích từ .docx; nếu .docx được cập nhật thì phải trích lại.

## Trạng thái hiện tại

Dự án dùng cá nhân, phi thương mại.

**Phase 0 xong** (ADR-0002): contracts, DB schema, project-scoped auth, event stream.
**Phase 1 xong** (ADR-0003): agent registry, Owner ↔ Orchestrator chat, task runtime trên DB (DAG, dispatch theo capability, song song, retry, khôi phục), adapter `claude-agent-sdk` / `codex-cli` / `mock` — đã chạy thật với Claude + Codex.
**Phase 2 xong** (ADR-0004): meeting/DM là task (`kind meeting|dm`, hub có moderator, biên bản, decision, action item), interventions (pause/resume/cancel/redirect, `requested → applied|rejected`), lọc timeline. UI mới `apps/web/index.html` + `po-app.js` là projection từ REST + WebSocket trên renderer Pixel Office (`observeOnly`).
Tiếp theo: Phase 3 (adapter async mock Suno/Tripo, artifact, approval BR-07, metrics).

## Lệnh

Python workspace quản lý bằng `uv` (root `pyproject.toml`; members `apps/api`, `packages/contracts`, `packages/agent-adapters`).

```bash
uv sync                                       # cài dependencies
uv run pytest                                 # toàn bộ test
uv run pytest apps/api/tests/test_events.py::test_stream_replays_then_pushes_live  # một test
uv run ruff check . && uv run ruff format .   # lint + format
uv run python scripts/gen_contracts.py        # sinh lại JSON Schema + openapi.json (bắt buộc khi đổi model/API)
uv run office-api create-owner --name "Tên"   # tạo principal, in token (chỉ hiện một lần)
uv run office-api serve --port 8000           # chạy API + UI tại http://127.0.0.1:8000/
                                              # env: OFFICE_DATABASE_URL (mặc định sqlite ./office.db), OFFICE_WEB_DIR (mặc định apps/web, rỗng = tắt),
                                              #      OFFICE_DATA_DIR (mặc định ./data: content store + workspace của agent)
```

Schema vẫn tạo bằng `create_all` (chưa có Alembic): đổi model ⇒ xóa `office.db` dev cũ.
Adapter thật cần CLI đã đăng nhập trên máy: `claude` (Claude Agent SDK) và `codex`. Test chỉ dùng mock/fake, không gọi provider thật.

## Cấu trúc backend hiện có

- `packages/contracts/src/office_contracts/` — Pydantic models là **nguồn gốc** của contract (`events.py`: Event envelope + EventType; `adapter.py`: CapabilityManifest, AgentInvocation, AgentStatus, ...). `schemas/*.json` và `openapi.json` là file **sinh ra**, không sửa tay; `test_generated_contracts_are_up_to_date` fail nếu cũ.
- `packages/agent-adapters/src/agent_adapters/`:
  - `base.py` — `AgentAdapter` Protocol + `SupportsCancel/SupportsEvents/SupportsArtifacts`.
  - `runner.py` — `TaskBackedAdapter`: nền chung (idempotent invoke, cancel requested→confirmed, phát lại event, quét artifact trong workdir). Adapter mới chỉ cần `describe_capabilities()` + `_execute(ctx)`.
  - `claude_sdk.py`, `codex_cli.py`, `mock.py`; `registry.py` (`create_adapter`). Gọi không có `workdir` ⇒ không cho ghi file.
- `apps/api/src/office_api/`:
  - `models.py` — toàn bộ schema §9. Tham chiếu giữa bảng project-scoped dùng composite FK `(project_id, x_id)`; BR-01 là unique partial index. Không có ORM relationship ⇒ phải `flush()` row cha trước khi thêm row con.
  - `auth.py` — `Membership` dependency là cách duy nhất để vào route `/projects/{project_id}/...`; không phải thành viên → 404.
  - `events.py` — `append_event()` trong transaction rồi `commit_and_publish()`; không publish trước commit, không tự tính sequence.
  - `idempotency.py`, `errors.py` — `Idempotency-Key` bắt buộc cho write; error envelope + `X-Request-ID`.
  - `hub.py` — `ConversationMixin`: chạy meeting/DM theo lượt (`conversation_turn`), moderator = Orchestrator active, biên bản → `decision.recorded` + action item thành task mới.
  - `interventions.py` — `InterventionMixin`: can thiệp chỉ `applied` khi runtime thấy run đã dừng thật; pause/resume không kèm task = cấp project.
  - `runtime.py` — `Runtime(ConversationMixin, InterventionMixin)`: dispatcher, chạy run, retry/backoff, block lan truyền, kết thúc plan → lượt Orchestrator tổng hợp, `recover()` khi khởi động. Mọi chuyển trạng thái đi kèm event trong cùng transaction.
  - `orchestrator.py` — prompt + schema `OrchestratorOutput` (structured output) + `validate_plan`.
  - `storage.py` — content store `obj://` (nội dung tin nhắn/kết quả) và đường dẫn workspace.
  - `routes/` — projects, events (REST replay có lọc `type`/`task_id`/`actor_id`/`conversation_id` + WebSocket), agents (`/adapters`, `/projects/{id}/agents`), orchestrator (`POST .../orchestrator/messages` → 202), work (tasks, conversations, messages, meetings, artifacts), interventions (`POST/GET .../interventions` → 202).
  - `auth.py` — write trong project dùng `OwnerMembership` (observer chỉ đọc → 403).
  - `main.py` — mount `apps/web/` làm static tại `/` **sau cùng**; route API mới phải khai báo trước mount.
- `apps/web/` — `index.html` + `po-app.js` + `po.css` là UI của dự án; `layout.js`/`office.js`/`style.css`/asset từ upstream (sửa gì ghi vào `UPSTREAM.md`). Office không được tự sinh hoạt động: chỉ `setStatus`/`speak`/`setMeeting` từ dữ liệu API/event.
- Test dùng `TestClient` + SQLite tạm; gọi code async trong loop của app qua `conftest.run(client, fn)`. Test runtime (`test_phase1.py`, `test_phase2.py`) dùng mock adapter với kịch bản `responses` và `wait_until(...)`.

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
