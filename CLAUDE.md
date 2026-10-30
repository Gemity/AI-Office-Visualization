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
**Phase 1 xong** (ADR-0003): agent registry, Owner ↔ Orchestrator chat, task runtime trên DB (DAG, dispatch theo capability, song song, retry, khôi phục), adapter `claude-cli` (trước là `claude-agent-sdk`) / `codex-cli` / `mock` — đã chạy thật với Claude + Codex.
**Phase 2 xong** (ADR-0004): meeting/DM là task (`kind meeting|dm`, hub có moderator, biên bản, decision, action item), interventions (pause/resume/cancel/redirect, `requested → applied|rejected`), lọc timeline. UI mới `apps/web/index.html` + `po-app.js` là projection từ REST + WebSocket trên renderer Pixel Office (`observeOnly`).
**Phase 3 xong** (ADR-0005): adapter async job (`AsyncJobAdapter`, mock `mock-suno`/`mock-tripo`), approval gate cho hành động rủi ro cao (BR-07), artifact viewer/content an toàn, metrics chi phí.
**Phase 4 xong** (ADR-0006): sandbox bubblewrap mặc định cho adapter CLI, gắn lại job async sau restart, ngân sách + circuit breaker, Alembic, log redaction + security headers, backup/healthcheck, deploy local (systemd `--user`, `docs/deploy-local.md`), e2e AC-01…AC-10 + NFR trong `tests/e2e/`.
**Chốt §18** (ADR-0007): giữ runtime tự viết, deploy local-first, policy mặc định do Owner cấu hình trong `office-policy.toml` (transcript DM riêng, retention, ngân sách mặc định, quyền can thiệp của observer, TTL approval).
**Launcher** (ADR-0008): `scripts/office-launch.py` — một file mang sang repo target (`.claude/`/`.agents/`); khối CẤU HÌNH đầu file (OFFICE_HOME, PORT, ORCHESTRATOR); `OFFICE_HOME` trống ⇒ tìm `AI-Office-Visualization` cùng cấp với repo target (quy tắc dùng nhiều máy, dữ liệu vẫn theo từng máy). Bấm là chạy server + tạo project theo repo + đồng bộ Điều phối + mở UI đã đăng nhập. Điều phối không có avatar trong Office, UI chỉ thêm/sửa/xoá worker (`PATCH`/`DELETE /projects/{id}/agents/{agent_id}`: chỉ worker đang rảnh; xoá = `enabled=false`, giữ lịch sử).
Chưa làm: Postgres (chưa kiểm chứng), secret vault, API/quota thật của Suno/Tripo.

## Lệnh

Python workspace quản lý bằng `uv` (root `pyproject.toml`; members `apps/api`, `packages/contracts`, `packages/agent-adapters`).

```bash
uv sync                                       # cài dependencies
uv run pytest                                 # toàn bộ test
uv run pytest apps/api/tests/test_events.py::test_stream_replays_then_pushes_live  # một test
uv run ruff check . && uv run ruff format .   # lint + format
uv run python scripts/gen_contracts.py        # sinh lại JSON Schema + openapi.json (bắt buộc khi đổi model/API)
uv run office-api create-owner --name "Tên"   # tạo principal, in token (chỉ hiện một lần)
uv run office-api serve --port 8000           # chạy API + UI tại http://127.0.0.1:8000/ (tự migrate khi khởi động)
                                              # env: OFFICE_DATABASE_URL (mặc định sqlite ./office.db), OFFICE_WEB_DIR (mặc định apps/web, rỗng = tắt),
                                              #      OFFICE_DATA_DIR (mặc định ./data: content store + workspace của agent),
                                              #      OFFICE_SANDBOX (bwrap mặc định | none),
                                              #      OFFICE_POLICY_FILE (mặc định ./office-policy.toml; đọc khi khởi động)
uv run office-api migrate                     # nâng DB lên schema mới nhất
uv run office-api makemigration -m "..."      # sinh migration Alembic khi đổi models.py (bắt buộc)
uv run office-api backup [--keep N]           # snapshot SQLite + data dir vào <data>/backups (mặc định retention.backups_keep)
uv run office-api prune [--dry-run]           # áp dụng [retention] của office-policy.toml ngay
uv run pytest tests/e2e                       # e2e: chạy server thật trong tiến trình riêng (chậm hơn)
```

Schema do Alembic quản lý (`office_api/migrations/`): đổi model ⇒ `office-api makemigration`; `test_migrations_match_models_exactly` fail nếu thiếu. DB cũ tạo bằng `create_all` bị từ chối ⇒ xóa `office.db` dev cũ.
Sandbox cần `bwrap` (không lồng được bwrap trong bwrap trên máy này: AppArmor chặn userns lồng).
Adapter thật cần CLI đã đăng nhập trên máy: `claude` (Claude Agent SDK) và `codex`. Test chỉ dùng mock/fake, không gọi provider thật.

## Cấu trúc backend hiện có

- `packages/contracts/src/office_contracts/` — Pydantic models là **nguồn gốc** của contract (`events.py`: Event envelope + EventType; `adapter.py`: CapabilityManifest, AgentInvocation, AgentStatus, ...; `policy.py`: RiskAction, ApprovalScope). `schemas/*.json` và `openapi.json` là file **sinh ra**, không sửa tay; `test_generated_contracts_are_up_to_date` fail nếu cũ.
- `packages/agent-adapters/src/agent_adapters/`:
  - `base.py` — `AgentAdapter` Protocol + `SupportsCancel/SupportsEvents/SupportsArtifacts`.
  - `runner.py` — `TaskBackedAdapter`: nền chung (idempotent invoke, cancel requested→confirmed, phát lại event, quét artifact trong workdir). Adapter mới chỉ cần `describe_capabilities()` + `_execute(ctx)`.
  - `jobs.py` — `AsyncJobAdapter`: worker dạng job (submit/poll/cancel), `provider_ref` + progress thật, output ghi vào workdir; `resume(request, provider_ref)` gắn lại job sau restart, `detach()` khi tắt (không hủy job ở provider).
  - `sandbox.py` — bọc lệnh CLI bằng bubblewrap theo `SandboxPolicy` (ẩn → chỉ đọc → ghi được); thiếu bwrap ⇒ `sandbox_unavailable`. Codex trong bwrap dùng `-s danger-full-access` (sandbox riêng của nó cần userns lồng).
  - `claude_cli.py` (`claude-cli`: CLI `claude` qua thư viện Claude Agent SDK), `codex_cli.py`, `mock.py`, `mock_jobs.py` (`mock-suno`, `mock-tripo`); `registry.py` (`create_adapter`, `config_fields`). Mỗi adapter khai báo `config_fields` (`ConfigField`) để UI hướng dẫn điền config. Gọi không có `workdir` ⇒ không cho ghi file.
- `apps/api/src/office_api/`:
  - `models.py` — toàn bộ schema §9. Tham chiếu giữa bảng project-scoped dùng composite FK `(project_id, x_id)`; BR-01 là unique partial index. Không có ORM relationship ⇒ phải `flush()` row cha trước khi thêm row con.
  - `auth.py` — `Membership` dependency là cách duy nhất để vào route `/projects/{project_id}/...`; không phải thành viên → 404.
  - `events.py` — `append_event()` trong transaction rồi `commit_and_publish()`; không publish trước commit, không tự tính sequence.
  - `idempotency.py`, `errors.py` — `Idempotency-Key` bắt buộc cho write; error envelope + `X-Request-ID`.
  - `hub.py` — `ConversationMixin`: chạy meeting/DM theo lượt (`conversation_turn`), moderator = Orchestrator active, biên bản → `decision.recorded` + action item thành task mới.
  - `approvals.py` — `ApprovalMixin`: dispatcher gọi `_approval_gate` trước khi tạo run; task có `risk_actions` hoặc worker có quyền rủi ro cao trong `required_permissions` → `awaiting_approval`; approval dùng một lần, scope gồm task/actions/worker/sha256 chỉ dẫn.
  - `interventions.py` — `InterventionMixin`: can thiệp chỉ `applied` khi runtime thấy run đã dừng thật; pause/resume không kèm task = cấp project.
  - `runtime.py` — `Runtime(ConversationMixin, InterventionMixin, ApprovalMixin)`: dispatcher, chạy run, retry/backoff, block lan truyền, kết thúc plan → lượt Orchestrator tổng hợp, `recover()` khi khởi động (nối lại run có `provider_ref`). Dispatcher kiểm ngân sách: project vượt trần ⇒ can thiệp pause `requested_by=system`. Mọi chuyển trạng thái đi kèm event trong cùng transaction.
  - `orchestrator.py` — prompt + schema `OrchestratorOutput` (structured output) + `validate_plan`.
  - `storage.py` — content store `obj://` (nội dung tin nhắn/kết quả), đường dẫn workspace, `sandbox_policy()` (ẩn data dir/DB/secret trong home).
  - `policy.py` — `Policy` nạp từ `office-policy.toml` (`Settings.policy`); khóa lạ ⇒ không khởi động. Test dùng `Policy()`, không đọc file của Owner.
  - `retention.py` — dọn content object/workspace cũ của project rảnh theo `[retention]`; không xóa event/audit/DB row.
  - `budget.py` — `ProjectBudget` (`projects.budget`), chi tiêu từ `agent_runs.cost`; `PUT /projects/{id}/budget`.
  - `migrations/` — Alembic (`upgrade()` gọi khi khởi động và bởi `office-api migrate`).
  - `logs.py` — che token trong log uvicorn; `cli.py` — `create-owner`, `serve`, `migrate`, `makemigration`, `backup`.
  - `routes/` — projects, events (REST replay có lọc `type`/`task_id`/`actor_id`/`conversation_id` + WebSocket), agents (`/adapters`, `/projects/{id}/agents`), orchestrator (`POST .../orchestrator/messages` → 202), work (tasks, conversations, messages, meetings, artifacts + `/artifacts/{id}/content` có kiểm checksum, CSP sandbox), interventions (`POST/GET .../interventions` → 202), approvals (`GET`, `POST .../approvals/{id}/decision`), metrics (`GET .../metrics`).
  - `auth.py` — write trong project dùng `OwnerMembership` (observer chỉ đọc → 403).
  - `main.py` — mount `apps/web/` làm static tại `/` **sau cùng**; route API mới phải khai báo trước mount. Security headers (CSP `script-src 'self'` ⇒ UI không được có inline script), `/healthz`, `/readyz`.
- `apps/web/` — `index.html` + `po-app.js` + `po.css` là UI của dự án; `layout.js`/`office.js`/`style.css`/asset từ upstream (sửa gì ghi vào `UPSTREAM.md`). Office không được tự sinh hoạt động: chỉ `setStatus`/`speak`/`setMeeting` từ dữ liệu API/event.
- Test dùng `TestClient` + SQLite tạm; gọi code async trong loop của app qua `conftest.run(client, fn)`. Test runtime (`test_phase1.py`, `test_phase2.py`, `test_phase3.py`) dùng mock adapter với kịch bản `responses` và `wait_until(...)`. `test_phase4.py` quét mọi route `/projects/{project_id}/…` trong OpenAPI (404/401/403) — route mới tự được kiểm.
- `tests/e2e/` — chạy `office-api serve` thật (port ngẫu nhiên) qua HTTP/WebSocket; fixture `server` ở `harness.py` (không dùng `conftest.py` ở đây vì trùng tên với `apps/api/tests/conftest.py`).

Đã chốt (xem `docs/adr/0001-web-stack-fork-pixel-office.md`):
- **Toàn bộ là web**, không dùng Unity (dù repo nằm trong thư mục `UnityProject/`). Không tạo asset/scene/script Unity.
- **UI base: fork Pixel Office** (https://github.com/basriayaz/pixel-office, MIT) vào `apps/web/` + `packages/office-renderer/`. Giữ `LICENSE`/copyright của upstream và ghi upstream + commit gốc trong `apps/web/UPSTREAM.md`. Upstream theo mô hình "thuê nhân viên", chưa có project scoping hay một Orchestrator duy nhất — khi fork phải sửa theo BR-01/02/04, không mang mô hình đó sang.
- **Chỉ fork frontend** (`web/` upstream → `apps/web/`, đã copy nguyên trạng, commit `4ebfdc7`). **Backend là FastAPI** trong `apps/api/`, viết mới — không dùng server Node/Express của upstream. Frontend hiện còn gọi `/api/*` của upstream, cần chuyển sang API spec §11. Ghi mọi thay đổi so với upstream vào `apps/web/UPSTREAM.md`.

Đã chốt §18 (ADR-0007): runtime tự viết (không LangGraph/workflow engine), deploy local-first, policy mặc định qua `office-policy.toml`.

Còn mở — **không tự chốt**, hỏi Owner hoặc ghi ADR trong `docs/adr/`:
- Tích hợp Claude/Codex qua SDK/API hay CLI (hiện: CLI `claude` + CLI `codex`, đều dùng đăng nhập của máy).
- API/quota chính thức của Suno/Tripo (dùng mock cho tới khi xác minh).

## Cấu trúc repo đề xuất (spec §17)

```
apps/web/                   # Project UI + Pixel Office
apps/api/                   # FastAPI + WebSocket
services/orchestrator/      # Workflow runtime
services/agent-workers/     # Worker execution/sandbox
packages/contracts/         # OpenAPI + event JSON schemas
packages/agent-adapters/    # Claude/Codex/Suno/Tripo/custom
packages/office-renderer/   # UI projection from events
infra/docker/               # Local Compose (chưa dùng: deploy local-first, ADR-0006)
infra/systemd/              # Mẫu systemd --user (service + backup timer)
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
