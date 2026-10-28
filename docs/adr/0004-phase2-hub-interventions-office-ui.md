# ADR-0004: Phase 2 — Meeting/DM hub, can thiệp của Owner, Office UI nối event stream

- Trạng thái: Accepted
- Ngày: 2026-10-28
- Liên quan: Spec v2.0 §8.2, §8.3, §10, §12, §15 (AC-04, AC-05, AC-06), BR-05, BR-06, BR-09; ADR-0001, ADR-0003

## Quyết định

1. **Meeting và DM là task** (`tasks.kind = 'meeting' | 'dm'`, có `participants`, `turn_limit`) do Orchestrator lên kế hoạch như task thường, nằm trong cùng DAG. Nhờ vậy chúng dùng chung dispatcher, phụ thuộc, khôi phục và can thiệp; kết quả hội thoại là `result` của task, task phụ thuộc đọc được.
2. **Can thiệp là bản ghi bền vững** (`interventions`: `requested → applied | rejected`), không phải lệnh tức thời. Trạng thái `applied` chỉ được ghi khi runtime đã quan sát được hiệu ứng thật (BR-06, BR-09).
3. **UI mới viết trên nền renderer Pixel Office** (`apps/web/index.html`, `po-app.js`, `po.css`). UI là projection từ REST + WebSocket; renderer chạy ở chế độ `observeOnly`, không còn hoạt động giả lập. Shell upstream giữ ở `pixel-office-upstream.html` để tham chiếu.

## Thiết kế

**Hub (`office_api/hub.py`, `ConversationMixin`).** Mỗi lượt nói là một lần gọi adapter (không tool, không workdir) và là một `agent_runs` (`purpose='conversation_turn'`) cộng một message. Người nói lần lượt xoay vòng theo `participants`, tối đa `turn_limit` lượt (mặc định meeting 6, DM 4, trần 12), giới hạn thời gian `conversation_time_limit_s` (900s). Một lượt lỗi được thử lại một lần, sau đó task `failed`.
- **Meeting:** `visibility='project'`. Orchestrator đang active làm moderator. Sau các lượt, moderator tạo biên bản theo schema `MeetingOutcome {minutes, decisions[], action_items[]}`. Mỗi quyết định phát `decision.recorded`, mỗi action item thành task `work` mới trong cùng plan. Vòng đời event: `meeting.started` → `message.sent`… → `decision.recorded` → `meeting.ended`.
- **DM:** `visibility='participants'` (BR-05). Owner vẫn đọc được với quyền audit. Không có moderator và không có biên bản.
- Participant không còn hợp lệ → task `blocked` (`participant_unavailable`). Plan có meeting/DM thiếu participants (DM cần đúng 2, meeting cần ít nhất 2) → `invalid_plan`.

**Can thiệp (`office_api/interventions.py`, `POST/GET /projects/{id}/interventions`, trả 202).**
- `pause` / `resume` không kèm task là can thiệp cấp project: `projects.status = paused` dừng dispatch; run đang chạy được chạy nốt.
- Task chưa chạy (`waiting`, `pending`, `paused`) → áp dụng ngay.
- Task đang chạy → yêu cầu adapter `cancel` (hoặc hủy vòng hội thoại). Chỉ khi run thật sự dừng mới chuyển `applied`. Nếu run kết thúc trước → `rejected`.
- `redirect` nối chỉ dẫn mới vào mô tả task rồi chạy lại. `resume` đưa task về `waiting`/`pending`.
- Task đã kết thúc → `rejected`. Mỗi task chỉ có một can thiệp `requested` tại một thời điểm (409).
- Orchestrator cũng có thể đề xuất can thiệp qua `OrchestratorOutput.interventions` khi Owner yêu cầu trong chat; `requested_by` ghi id của Orchestrator.
- Event: `intervention.requested` → `intervention.applied` (payload `status = applied | rejected`, `outcome`).

**Timeline/replay.** `GET /projects/{id}/events` lọc theo `type` (lặp được), `task_id`, `actor_id`, `conversation_id`; replay một cuộc họp = lọc theo `conversation_id` (AC-04). `GET /projects/{id}/meetings` trả biên bản, quyết định và cờ `live`.

**Office UI (`apps/web/po-app.js`).**
- Đăng nhập bằng token (localStorage). Mọi write gửi kèm `Idempotency-Key`. WebSocket `/projects/{id}/stream?after_seq=` tự kết nối lại; mã đóng 4401 → đăng xuất. De-dup theo `sequence`.
- Mỗi event chỉ làm "bẩn" các tài nguyên liên quan, rồi refresh lại từ REST (debounce 250ms). UI không tự suy diễn trạng thái từ payload.
- Agent chỉ ở trạng thái "working" khi API có `current_runs`. Bong bóng thoại chỉ hiện nội dung `message.sent` thật. Phòng họp chỉ có người khi meeting `live`. Lỗi chỉ hiện khi có `run.failed` không retry.
- Tab: Chat với Orchestrator (chỉ báo "đang soạn" theo run thật), Task (theo plan, nút tạm dừng/tiếp tục/chuyển hướng/hủy, badge "đã yêu cầu — chờ xác nhận" khác "đã áp dụng"), Họp & DM (transcript, quyết định), Timeline (lọc theo loại), Agent (thêm agent theo `/adapters`), Chi tiết agent.

## Đã kiểm chứng

- 52 test, trong đó 10 test Phase 2: AC-04 (DM + meeting ghi lại và replay được), AC-06 (pause đang chạy: requested → applied, resume chạy lại), cancel/redirect, project pause, Orchestrator yêu cầu can thiệp, hủy meeting đang diễn ra, lọc timeline.
- UI kiểm tra bằng Chrome headless trên server thật với agent mock: meeting live (agent vào phòng họp, bong bóng thoại là tin nhắn thật), task board, can thiệp tạm dừng từ UI (requested → applied, run `cancelled`), timeline, chi tiết agent; không có lỗi console.

## Giới hạn / việc sau

- Provider không có pause thật: pause một task đang chạy là dừng run; resume chạy lại từ đầu (với Claude/Codex tốn thêm chi phí).
- `token_budget` của meeting chưa được áp dụng; chỉ có giới hạn lượt và thời gian.
- Chưa có approval (BR-07) và metrics — Phase 3.
- UI chưa có replay kiểu "tua lại" Office theo thời gian; replay hiện là xem transcript và timeline đã lọc.
- Schema đổi (task, meeting, bảng `interventions` mới): xóa `office.db` dev cũ trước khi chạy.
