# ADR-0007: Chốt runtime tự viết, deploy local-first, policy mặc định qua file cấu hình

- Trạng thái: Accepted
- Ngày: 2026-10-29
- Liên quan: Spec v2.0 §8.2, §8.3, §13, §18; BR-05, BR-06, BR-07; ADR-0003, ADR-0005, ADR-0006

## Quyết định của Owner (2026-10-29)

Đóng ba câu hỏi còn mở ở spec §18:

1. **Runtime: giữ runtime tự viết** (`office_api/runtime.py` + DB). Không dùng LangGraph hay workflow engine chuyên dụng. Trạng thái bền vững vẫn nằm trong DB (BR-08). Dispatcher, retry/backoff, khôi phục sau restart, gắn lại job vẫn như ADR-0003/0006.
2. **Deploy: local-first** (xác nhận ADR-0006). Một máy, systemd `--user`, SQLite. Server 24/7 / Postgres không nằm trong kế hoạch.
3. **Policy mặc định: Owner tự cấu hình bằng file.** Tạo file `office-policy.toml` ở gốc repo. Giá trị có sẵn trong file giữ nguyên hành vi hiện tại, chỉ trừ transcript DM riêng (xem bên dưới).

## Thiết kế

**Nạp file (`office_api/policy.py`).**
- `Settings.policy` đọc `OFFICE_POLICY_FILE`, mặc định `./office-policy.toml` (systemd chạy ở thư mục repo nên đọc được đúng file này).
- File chỉ được đọc một lần khi khởi động; sửa xong phải restart.
- Thiếu file ⇒ dùng mặc định trong code. Khóa lạ hoặc giá trị sai ⇒ `SystemExit`, server không khởi động, thay vì lặng lẽ bỏ qua.
- Test luôn dùng `Policy()`, không đọc file của Owner. `test_policy_file_shipped_with_repo_matches_builtin_defaults` giữ file trong repo khớp với mặc định trong code.

**Các mục trong file.**

| Mục | Khóa | Mặc định | Hiệu lực |
|---|---|---|---|
| `[transcripts]` | `private_readers` | `["owner"]` | Vai trò được đọc nội dung DM riêng (`visibility=participants`) |
| `[retention]` | `content_days`, `workspace_days` | `0` (giữ mãi) | Dọn nội dung và workspace cũ |
| | `sweep_interval_h` | `6` | Chu kỳ server tự dọn |
| | `backups_keep` | `14` | Số bản `office-api backup` được giữ |
| `[budget]` | `default_max_cost_usd` | trống (không giới hạn) | Trần chi phí cho project mới |
| `[interventions]` | `observer_actions` | `[]` | Hành động can thiệp observer được gửi |
| `[approvals]` | `ttl_s` | `3600` | Thay `Settings.approval_ttl_s`; project vẫn ghi đè bằng `config.approval_ttl_s` |

**Transcript riêng (§8.2: "Owner có thể có quyền audit theo cấu hình").**
- `GET …/conversations/{id}/messages` của DM riêng trả 403 `transcript_private` cho vai trò không nằm trong `private_readers`.
- Event `message.sent` mang thêm `payload.private`. Với người không được đọc, REST replay và WebSocket bỏ `preview` và `content_ref`; vẫn giữ ai gửi, gửi cho ai, lúc nào, để Office hiển thị đúng hoạt động mà không lộ nội dung.
- Event được lọc lúc đọc, không sửa trong DB. Vì vậy đổi policy có hiệu lực cho cả lịch sử cũ.
- Event `message.sent` tạo trước ADR này không có cờ `private`, nên không bị lọc.
- **Thay đổi hành vi:** trước đây observer đọc được DM. Mặc định mới chỉ cho Owner đọc, đúng tinh thần spec.
- Meeting nhóm và chat Owner ↔ Orchestrator không bị ảnh hưởng.
- UI hiện thông báo "Transcript riêng tư" thay cho nội dung.

**Retention (`office_api/retention.py`, §13 "retention configurable").**
- Chỉ dọn project đang rảnh: không còn task chưa ở trạng thái cuối và không có run đang chạy. Vì vậy plan đang chạy không bao giờ mất dữ liệu nó cần đọc.
- Xóa content object (tin nhắn, kết quả task, biên bản, output của run) có mtime cũ hơn `content_days`. Ref vẫn giữ nguyên, API trả `content: null`, UI hiện "(nội dung đã hết hạn lưu trữ)".
- Xóa thư mục workspace của task không đổi trong `workspace_days`. Artifact row vẫn còn, `/content` trả 410 `artifact_gone`.
- **Không bao giờ xóa** event, audit log hay dòng DB: audit trail bất biến (§13).
- Mỗi project được dọn ghi một `AuditLog` `retention.prune` (`principal=system`).
- Server chạy sweep khi khởi động và sau đó cứ mỗi `sweep_interval_h`, chỉ khi retention bật.
- Dọn tay: `office-api prune [--dry-run]`.
- Bản backup vẫn chứa dữ liệu đã dọn cho tới khi chính bản backup đó bị xoay vòng (`backups_keep`).

**Ngân sách mặc định.**
- `POST /projects` không gửi `budget` ⇒ nhận `{"max_cost_usd": default_max_cost_usd}`.
- Gửi `budget` tường minh (kể cả `null`) thì giữ đúng giá trị đó.
- Project đã tồn tại không bị đổi. Ngân sách theo project vẫn là nguồn sự thật (ADR-0006).

**Quyền can thiệp.**
- Owner luôn được can thiệp. Observer chỉ được gửi các hành động trong `observer_actions`; hành động khác ⇒ 403.
- Danh sách rỗng ⇒ observer bị chặn ngay ở dependency, trước khi đọc body. Nhờ vậy test quét route Phase 4 vẫn thấy 403.
- Can thiệp vẫn đi qua `requested → applied|rejected` như cũ (BR-06), `requested_by` là principal của observer.

## Đã kiểm chứng

- 7 test mới trong `apps/api/tests/test_policy.py`:
  - nạp file: thiếu file / khóa sai / file trong repo khớp mặc định;
  - DM riêng: REST + WebSocket với owner/observer, và trường hợp `private_readers = []`;
  - observer chỉ được can thiệp theo cấu hình;
  - ngân sách mặc định;
  - retention: dry-run, chỉ dọn project rảnh, 410 artifact, giữ event, ghi audit.
- Toàn bộ test (kể cả e2e) pass.
- Chạy thử `office-api serve` thật với file policy trong repo: `/readyz` OK. File có khóa sai ⇒ lệnh dừng với thông báo của pydantic.

## Giới hạn / việc sau

- Retention dựa trên mtime của file. Copy/restore làm mất mtime gốc thì đồng hồ tính lại từ lúc restore.
- Không có endpoint xem policy đang áp dụng; UI chỉ biết qua lỗi 403. Thêm `GET /policy` nếu cần.
- Chưa có policy "quyền xóa/export theo yêu cầu" (§13). Hiện chỉ dọn theo thời gian.
