# ADR-0006: Phase 4 — Hardening: sandbox, gắn lại job, ngân sách, Alembic, bảo mật, deploy local

- Trạng thái: Accepted
- Ngày: 2026-10-28
- Liên quan: Spec v2.0 §7, §9, §11, §13, §14, §15 (AC-01…AC-10), §18; BR-04, BR-06, BR-08, BR-09; ADR-0003, ADR-0005

## Quyết định của Owner (2026-10-28)

1. **Deploy local-first.** Một máy, một người dùng: systemd `--user` + SQLite, có backup và healthcheck. Server 24/7 / Postgres để sau.
2. **Ngân sách mặc định không giới hạn.** Chỉ áp dụng khi được cấu hình. Vượt trần project thì circuit breaker dừng dispatch và báo trên UI.
3. **Sandbox OS bật mặc định** (bubblewrap). Máy không có `bwrap` thì từ chối chạy adapter CLI, trừ khi đặt `OFFICE_SANDBOX=none`. Mạng vẫn mở (agent cần gọi API của provider).

## Thiết kế

**Sandbox (`agent_adapters/sandbox.py`, contract `SandboxPolicy`).**
- `AgentInvocation.sandbox` mô tả `mode` (`none|bwrap`), `writable`, `readable`, `hidden`, `network`. Runtime tạo policy bằng `storage.sandbox_policy()` cho lượt task, lượt Orchestrator và lượt hội thoại.
- Thứ tự mount của bwrap: `--ro-bind / /` → `/dev`, `/proc`, tmpfs `/tmp` → **ẩn** (thư mục dùng tmpfs, file dùng `/dev/null`) → mở lại **chỉ đọc** → **ghi được**. Mount sau đè mount trước. Thêm `--unshare-pid/ipc`, `--new-session`, `--die-with-parent`.
- Bị ẩn: data dir của Office (content store + workspace của project khác), file SQLite (`-wal/-shm/-journal`), `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.azure`, `~/.kube`, `~/.docker`, `~/.config/gh`, `~/.config/gcloud`, `~/.netrc`, `~/.git-credentials`, `~/.pgpass`, `~/.password-store`, `.env*`, `secrets/` ở thư mục chạy server.
- Đọc được: workspace của chính project đó. Ghi được: workdir của task, cùng state của CLI (`~/.claude*`, `~/.cache/claude`, `~/.codex`) vì CLI cần token đăng nhập và cache.
- Claude Agent SDK: `cli_path` trỏ tới một script wrapper `exec bwrap … -- <claude thật> "$@"`.
- Codex: bọc cả lệnh `codex exec`; bên trong bwrap dùng `-s danger-full-access`. Sandbox riêng của Codex cần user namespace lồng nhau, mà host bật `apparmor_restrict_unprivileged_userns=1` nên chặn. **Đánh đổi:** lệnh shell do Codex chạy vẫn có mạng. Filesystem vẫn bị bwrap giới hạn như trên.
- Thiếu bwrap ⇒ `AdapterRunError("sandbox_unavailable", retryable=False)`: task fail rõ ràng, không lặng lẽ chạy ngoài sandbox.

**Gắn lại job sau restart (AC-10, NFR Recovery).**
- `SupportsResume`. `AsyncJobAdapter.resume(request, provider_ref)` nối lại job đang chạy ở provider thay vì submit lại.
- Khi tắt server, `detach()` thả job ra mà không hủy ở provider.
- `Runtime.recover()`: run `RUNNING` có `provider_ref` và adapter hỗ trợ resume ⇒ chạy lại cùng run đó (giữ nguyên `started_at`). Provider báo `job_not_found` ⇒ run `failed` (retryable), sau đó đi theo chính sách retry. Run không resume được ⇒ `interrupted` + retry như Phase 1.
- Mock job lưu `.mockjob-<id>.json` trong workdir và tính tiến độ theo wall clock, nên job tiếp tục chạy trong lúc server tắt (kể cả khi bị `SIGKILL`).

**Ngân sách + circuit breaker (`office_api/budget.py`).**
- `projects.budget = {"max_cost_usd": float|null, "agents": {agent_id: float}}`, cập nhật qua `PUT /projects/{id}/budget` (chỉ owner, có idempotency key, ghi AuditLog).
- Chi tiêu = tổng `agent_runs.cost` mà provider báo. Run không báo chi phí tính là 0 cho ngân sách và được đếm riêng trong metrics.
- Project chạm trần ⇒ dispatcher tạo can thiệp `pause` cấp project với `requested_by = "system"`, `outcome = "budget_exceeded: spent $X of $Y"`. Đây là đường can thiệp thông thường (event + audit + UI requested/applied). Resume khi vẫn vượt trần ⇒ 409 `budget_exceeded`.
- Agent chạm trần ⇒ không được giao việc mới. Task không còn worker nào trong ngân sách ⇒ `blocked (budget_exceeded)`. Hội thoại có participant vượt trần cũng bị block.
- `InvocationLimits.max_cost` = ngân sách còn lại. Adapter nào hỗ trợ (Claude SDK `max_budget_usd`) thì tự dừng.
- Metrics trả thêm `budget` và `over_budget`. UI có form ngân sách trong tab "Chi phí", và hiện lý do tạm dừng trên nút project.

**Migration (Alembic) thay `create_all`.**
- `office_api/migrations/` (`env.py` dùng `render_as_batch` cho SQLite). Revision `0001` là toàn bộ schema hiện tại.
- Server tự `upgrade` khi khởi động; có lệnh `office-api migrate`. DB tạo bằng `create_all` trước đây bị từ chối với thông báo rõ ràng (không đoán phiên bản).
- Đổi `models.py` ⇒ `office-api makemigration -m "..."`. Lệnh này diff với một DB tạm ở head. `test_migrations_match_models_exactly` fail nếu thiếu migration.

**Vận hành.**
- `office-api backup --keep N`: snapshot SQLite bằng online backup API + data dir → `<data>/backups/office-*.tar.gz` (chmod 600). Chỉ hỗ trợ SQLite; Postgres dùng `pg_dump`.
- `/healthz` (liveness) và `/readyz` (DB trả lời + dispatcher đang chạy).
- Mẫu systemd `infra/systemd/` (service + backup timer), xem `docs/deploy-local.md`. Repo không tự cài.

**Bảo mật.**
- Log redaction (`office_api/logs.py`): `token=…`, `Bearer …`, `po_…` bị che trong log uvicorn. Filter giữ nguyên số lượng và kiểu `args` vì access formatter đọc trực tiếp.
- Header cho mọi response: `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, CSP `default-src 'self'; script-src 'self'` (không còn inline script; cấu hình UI chuyển sang `po-config.js`). Route artifact giữ CSP `sandbox` riêng của nó.
- Test quét toàn bộ route `/projects/{project_id}/…` trong OpenAPI: người ngoài ⇒ 404, không có token ⇒ 401, observer: GET được và mọi write ⇒ 403. WebSocket: token sai ⇒ 4401, không phải thành viên ⇒ 4404.

**Kiểm thử e2e (`tests/e2e/`).**
- Chạy `office-api serve` thật trong tiến trình riêng (port ngẫu nhiên, DB tạm), gọi qua HTTP + WebSocket như trình duyệt. Chỉ dùng adapter mock.
- `test_acceptance.py`:
  - AC-01 (cô lập project);
  - một kịch bản gộp AC-02/03/04/05/07/08/09;
  - AC-06;
  - AC-10 với cả `SIGTERM` lẫn `SIGKILL`: event replay y hệt qua REST/WS, job được gắn lại chứ không submit lại, sequence liền mạch.
- `test_nfr.py`: ≥5 run song song (giới hạn `max_parallel_runs=5`) và p95 độ trễ event → client WS < 2s. Đo trên máy dev: p95 ≈ 0.07s.
- Fixture `server` nằm trong `harness.py`, không đặt `conftest.py` ở `tests/e2e/` vì sẽ trùng tên module với `apps/api/tests/conftest.py`.

## Đã kiểm chứng

- 89 test pass (Phase 4 thêm: 11 trong `test_phase4.py`, 6 sandbox, 2 restart trong `test_phase3.py`, 7 e2e).
- Claude và Codex thật chạy trong bwrap: ghi được workdir; ghi ra ngoài bị chặn; `~/.ssh` và file secret bị ẩn.
- UI trên server thật (Chrome headless): vượt trần → project tạm dừng, nút hiện "đã dừng: vượt ngân sách" → nâng trần trong tab Chi phí → tiếp tục → task còn lại hoàn thành. Không có lỗi CSP/console.

## Giới hạn / việc sau

- Codex trong bwrap: lệnh shell có mạng (xem trên). `network=False` vẫn dùng được cho adapter khác (`--unshare-net`).
- Chi phí chỉ biết khi run kết thúc, nên run đang chạy có thể vượt trần (trừ adapter tự tôn trọng `max_cost`).
- Lượt Orchestrator không bị chặn bởi ngân sách, để Owner luôn chat được và nhận báo cáo; chi phí của nó vẫn được cộng.
- Chưa chạy với PostgreSQL (máy dev không có). Migration và query viết trung tính, nhưng chưa kiểm chứng.
- TTL approval (`approval_ttl_s`) là cài đặt riêng. "Policy mặc định" ở §18 (retention, quyền can thiệp, transcript riêng) vẫn mở.
- Chưa có secret vault; token provider do CLI tự quản lý trong home của user.
