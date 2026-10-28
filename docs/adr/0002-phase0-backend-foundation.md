# ADR-0002: Nền tảng backend Phase 0

- Trạng thái: Accepted
- Ngày: 2026-10-28
- Liên quan: Spec v2.0 §7, §9, §10, §11, §13, §16 (Phase 0); ADR-0001

## Bối cảnh

Phase 0 cần chốt schema, adapter contract, project-scoped auth và prototype event stream. Máy dev hiện chỉ có Python + uv (chưa có Docker/Postgres/Redis). Dự án dùng cá nhân, phi thương mại.

## Quyết định

**Python workspace (uv).** Ba package: `apps/api` (`office_api`), `packages/contracts` (`office_contracts`), `packages/agent-adapters` (`agent_adapters`). Python ≥ 3.12.

**Contracts: Pydantic là nguồn gốc.** Event envelope và adapter contract định nghĩa bằng Pydantic trong `office_contracts`. JSON Schema (`packages/contracts/schemas/*.schema.json`) và `openapi.json` được **sinh ra** bằng `scripts/gen_contracts.py`; test sẽ fail nếu file sinh ra bị cũ. Frontend/công cụ khác dùng các file JSON này.

**Adapter contract.** `AgentAdapter` (Protocol) gồm `describe_capabilities`, `invoke`, `get_status`. Các phương thức tùy chọn trong spec tách thành `SupportsCancel`, `SupportsEvents`, `SupportsArtifacts` để runtime kiểm tra bằng `isinstance`. `CancelResult` phân biệt `accepted` với `state` thực tế quan sát được (BR-06, AC-06).

**Database.** SQLAlchemy 2 async; Postgres là đích, SQLite (aiosqlite) cho dev/test. Phase 0 tạo bảng bằng `create_all`; **Alembic migrations bắt buộc trước khi có dữ liệu thật/Postgres**.
- Đủ 15 bảng §9, thêm `principals`, `api_tokens`, `project_members`, `idempotency_keys`.
- Tham chiếu giữa các bảng project-scoped dùng composite FK `(project_id, x_id) → (project_id, id)` — DB chặn tham chiếu chéo project (BR-04).
- BR-01 được DB đảm bảo: unique partial index trên `orchestrator_assignments(project_id) WHERE active_to IS NULL`.

**Auth.** Bearer token (`po_…`, chỉ lưu sha256) → principal → `project_members` (role `owner` | `observer`). Không phải thành viên → 404 `project_not_found` (không lộ sự tồn tại của project). WebSocket nhận token qua query `?token=` vì trình duyệt không gửi header Authorization cho WebSocket. Tạo owner bằng CLI `office-api create-owner`. Chưa có đăng nhập/OAuth — đủ cho dùng cá nhân, xem lại nếu mở rộng.

**Event stream.**
- `sequence` tăng liên tục, không khuyết, theo từng project: `UPDATE projects SET event_seq = event_seq + 1 … RETURNING` trong cùng transaction với thay đổi nghiệp vụ (khóa dòng project ⇒ commit theo đúng thứ tự).
- Chỉ publish sau khi commit (`commit_and_publish`) ⇒ không bao giờ đẩy event có thể bị rollback (BR-09).
- Broker **in-process** (một tiến trình API). WS: subscribe → replay từ DB → push live; bỏ trùng theo sequence; nếu thấy khoảng trống thì đọc bù từ DB. Client chậm bị đóng với mã 4409 và kết nối lại bằng `after_seq`.
- Thay bằng Redis Streams khi chạy nhiều tiến trình/worker (Phase 1–2); giao diện `EventBroker.publish/subscribe` giữ nguyên.

**API conventions.** Mọi response có header `X-Request-ID`. Lỗi: `{"error": {"code", "message"}, "request_id"}`. Write endpoint bắt buộc header `Idempotency-Key` (theo principal); gửi lại cùng request → trả lại response cũ (`Idempotent-Replayed: true`); cùng key khác nội dung → 409 `idempotency_key_reused`. Write trả `{"request_id", "status", "data"}`.

## Hệ quả / việc còn lại

- Chưa có: Alembic, Postgres/Redis thật, `infra/docker` (chưa có Docker trên máy), RBAC chi tiết theo action, rate limit, secret vault.
- Kết nối frontend fork (`apps/web`) với API mới làm ở Phase 1–2.
