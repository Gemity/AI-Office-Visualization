# ADR-0008: Launcher một file, Điều phối cấu hình trong launcher, không có avatar

- Trạng thái: Accepted
- Ngày: 2026-10-29
- Liên quan: BR-01, BR-02, BR-03; ADR-0007; `docs/deploy-local.md`

## Quyết định của Owner (2026-10-29)

1. Chỉ mang **một file thực thi** (`scripts/office-launch.py`) sang repo target, đặt trong `.claude/` hoặc `.agents/`. Bấm là chạy.
2. **Config của Điều phối nằm ngay đầu file đó**, trong khối CẤU HÌNH gồm `OFFICE_HOME`, `PORT`, `ORCHESTRATOR`. Owner tự mở file để chỉnh.
3. Điều phối **không cần khởi tạo tay**: mở project bằng launcher thì luôn chat được ngay.
4. Điều phối **không có avatar** trong Office.

## Thiết kế

**Launcher** (chỉ dùng thư viện chuẩn của Python).
- Khởi động server nếu chưa chạy: dùng systemd `--user` nếu đã cài unit, không thì chạy nền.
- Lần đầu: tạo Owner và lưu token (file 600).
- Tìm hoặc tạo project theo `config.repo_path`.
- So `ORCHESTRATOR` với Điều phối đang active (`name`, `adapter_type`, `config`). Khác thì gọi `POST /projects/{id}/agents` với `role=orchestrator` để thay bằng phiên bản mới. Đây là đường BR-01 sẵn có: có version, `active_to` của bản cũ và event `agent.assigned`.
- Không thêm API mới. `AgentOut` có thêm trường `config` (config adapter, không chứa secret) để launcher so sánh được.
- Mở UI qua `/#token=…&project=…`. Phần fragment không bao giờ được gửi lên server hay ghi vào log. UI lưu lại vào localStorage rồi xóa khỏi thanh địa chỉ.

**UI.**
- Office và roster chỉ dựng worker.
- Điều phối chỉ hiện ở khung chat và danh sách agent.
- Lời nói (`speak`) và phòng họp chỉ áp cho worker.
- Form ở tab Agent chỉ thêm worker. Lỗi `no_orchestrator` hướng dẫn mở project bằng launcher.

**BR-03.** Adapter mặc định `claude-agent-sdk` là giá trị cấu hình trong file của Owner, không phải logic điều phối của server.

## Bổ sung (2026-10-29): quản lý worker, hướng dẫn config, đổi tên adapter

- **Đổi tên `claude-agent-sdk` → `claude-cli`.** Adapter này luôn chạy CLI `claude` đã đăng nhập trên máy. Thư viện Claude Agent SDK chỉ là cầu nối: nó khởi động CLI làm tiến trình con.
  - Thứ tự tìm CLI: `cli_path` → `claude` trong PATH → bản CLI kèm theo SDK.
  - Migration `0002` đổi `adapter_type` trong `agent_definitions` (kể cả trong `capability_manifest`) và `agent_runs.adapter` của dữ liệu cũ.
  - Gói Python `claude-agent-sdk` vẫn là dependency.
- **Sửa/xoá worker.**
  - `PATCH /projects/{id}/agents/{agent_id}` sửa `name` và/hoặc thay toàn bộ `config`. Manifest được tính lại và runtime bỏ adapter cũ trong cache, nên run sau dùng config mới. Phát event `agent.updated`.
  - `DELETE` đặt `project_agents.enabled = false` và phát event `agent.removed`. Agent vẫn được liệt kê (`enabled=false`) để lịch sử còn tên. Worker đã xoá không nhận việc mới; meeting/DM cần worker đó bị block (`participant_unavailable`).
  - Cả hai chỉ áp cho worker đang rảnh: worker có run đang chạy ⇒ 409 `agent_busy`, vì run đó còn giữ adapter cũ và lệnh cancel phải tới đúng adapter ấy. Với Điều phối ⇒ 409 `orchestrator_managed`, vì Điều phối do launcher quản lý.
  - Không đổi được adapter của worker đã có: muốn đổi thì xoá rồi thêm worker mới.
- **Hướng dẫn điền.**
  - Contract `ConfigField` (`key`, `type`, `description`, `default`, `choices`, `example`). Mỗi adapter khai báo `config_fields`, trả về trong `GET /adapters`.
  - `GET /capabilities` trả tập capability cố định của contract.
  - UI: capabilities là checkbox (adapter job có capabilities cố định thì checkbox bị khoá); dưới ô config có giải thích từng khóa; nút "Điền mẫu" điền sẵn config ví dụ.

## Giới hạn

- Project tạo bằng nút "+ Project" trên UI chưa có Điều phối cho tới khi được mở bằng launcher.
- Agent vẫn làm việc trong workspace riêng của Office. Repo target mới chỉ được ghi lại (`repo_path`), agent chưa đọc hay ghi trực tiếp vào đó.
