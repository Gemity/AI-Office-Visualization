# ADR-0003: Runtime Phase 1, Orchestrator và adapter Claude/Codex

- Trạng thái: Accepted
- Ngày: 2026-10-28
- Liên quan: Spec v2.0 §5, §7, §8.1, §15 (AC-02, AC-03, AC-07, AC-10), §18; ADR-0002

## Quyết định (Owner chọn 2026-10-28)

1. **Runtime tự viết trên DB**, không dùng LangGraph. Toàn bộ trạng thái nằm trong `tasks`, `agent_runs`, `events`; adapter instance và asyncio task chỉ là bộ nhớ tạm.
2. **Claude qua Claude Agent SDK** (`claude-agent-sdk`), chạy bằng Claude Code CLI đã đăng nhập trên máy. Không cần API key.
3. **Codex qua `codex exec`** (Codex CLI 0.165, đăng nhập bằng ChatGPT).

## Thiết kế

**Orchestrator.** Mỗi lượt Orchestrator (`agent_runs.purpose = 'orchestrator_turn'`) là một lần gọi adapter có structured output `OrchestratorOutput {reply, plan_title, tasks[]}` (`office_api/orchestrator.py`). Orchestrator không có công cụ, không sửa trạng thái; runtime kiểm tra kế hoạch (key trùng, phụ thuộc lạ, vòng lặp) rồi mới tạo task. Kế hoạch sai → `run.failed` (`invalid_plan`) và thử lại lượt (tối đa 3). Mọi field trong schema đều required (nullable khi tùy chọn) để tương thích structured output kiểu strict của OpenAI.

Lượt được kích hoạt khi: (a) Owner gửi tin (`POST /projects/{id}/orchestrator/messages`, trả 202 kèm `run_id`); (b) một kế hoạch kết thúc (mọi task con ở trạng thái cuối) → Orchestrator tổng hợp và báo Owner (§8.1). Chỉ Orchestrator ghi vào hội thoại `owner_chat` (BR-02); tin nhắn của worker vào `task_thread` riêng của task.

**Task.** Mỗi kế hoạch có một task gốc `kind='plan'` và các task con `kind='work'`. Trạng thái: `waiting` (còn phụ thuộc) → `pending` (sẵn sàng; `retry_at` để trễ lần thử lại) → `active` → `completed` | `failed`; task có phụ thuộc thất bại hoặc không có worker phù hợp → `blocked` (lan truyền xuống các task phụ thuộc).

**Dispatcher.** Một vòng lặp asyncio (đánh thức ngay khi có thay đổi, hoặc mỗi `dispatch_interval_s`). Chọn worker theo `required_capabilities ⊆ manifest.capabilities` (BR-03), ưu tiên `assignee_hint` nếu hợp lệ; mỗi agent một run tại một thời điểm; tối đa `max_parallel_runs` (5) run/project.

**Retry (AC-07).** Lỗi `retryable` và `attempts < max_attempts` (3) → về `pending` với backoff `retry_backoff_s * 2^(n-1)`; ngược lại `failed`. Mỗi lần lỗi có event `run.failed` (`will_retry`, `error`). Timeout run: `run_timeout_s` (1800s) → hủy và tính là lỗi retryable.

**Khôi phục (AC-10).** Khi khởi động, mọi run chưa kết thúc từ tiến trình trước được đánh dấu `failed` (`interrupted`, retryable) và đi qua đúng chính sách retry.

**Workspace và artifact.** Mỗi task có thư mục riêng `data/workspaces/<project>/<task>/` (agent chỉ ghi ở đó, được đọc cả thư mục project). File mới hoặc vừa sửa trong lúc chạy → `artifacts` (URI `file://`, sha256) + event `artifact.created`. Nội dung tin nhắn và kết quả lưu trong content store cục bộ `data/objects/` qua `content_ref = obj://…`.

**Adapter.** `TaskBackedAdapter` (`agent_adapters/runner.py`) cung cấp đầy đủ contract cho mọi adapter chạy cục bộ: invoke idempotent theo key, `cancel` chỉ *yêu cầu* hủy (`cancel_requested`, chuyển `cancelled` khi task thật sự dừng), phát lại sự kiện, quét artifact.
- `claude-agent-sdk`: tool mặc định của worker là `Read, Write, Edit, Glob, Grep` (không có Bash), `permission_mode=acceptEdits`, `setting_sources=[]` (không kế thừa settings/hook của người dùng), structured output qua `output_format`.
- `codex-cli`: `codex exec --json --ephemeral -s workspace-write -C <workdir>`, prompt qua stdin, kết quả qua `-o`; cấm `danger-full-access`.
- Gọi không có `workdir` (lượt Orchestrator) → Claude không có tool nào, Codex chạy `read-only` trong thư mục tạm.
- `mock`: theo kịch bản, dùng cho test và demo.

## Đã kiểm chứng

- 42 test (mock adapter, CLI Codex giả, `query` giả của SDK) bao phủ AC-02, AC-03, AC-07, AC-10, BR-01, BR-03.
- Chạy thật 2026-10-28: Orchestrator Claude giao 2 task song song cho worker Claude và Codex; cả hai tạo đúng file; Orchestrator báo kết quả bằng tiếng Việt. Tổng ~27 giây.

## Giới hạn / việc sau

- Sandbox chưa đầy đủ (§13): Claude worker không có Bash và chỉ ghi trong workdir; Codex dùng sandbox `workspace-write` của chính nó. Sandbox cấp OS để Phase 4.
- Mock adapter giữ vị trí kịch bản trong bộ nhớ; sau restart sẽ chạy lại từ đầu.
- Chưa có pause/resume/cancel/redirect từ Owner (Phase 2), approval (Phase 3), ngân sách theo project.
- Schema DB đổi so với Phase 0 nhưng vẫn dùng `create_all`: **xóa file SQLite dev cũ** (`office.db`) trước khi chạy. Cần Alembic trước khi có dữ liệu thật.
