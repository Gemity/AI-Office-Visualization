# ADR-0001: Toàn bộ là web, UI fork từ Pixel Office

- Trạng thái: Accepted
- Ngày: 2026-10-28
- Liên quan: Spec v2.0 §12, §17, §18

## Bối cảnh

Spec §18 để mở lựa chọn UI base ("fork Pixel Office sau khi kiểm tra license, activity event và khả năng mở rộng"). Repo nằm trong thư mục `UnityProject/`, nên cần làm rõ renderer Office có dùng Unity hay không.

## Quyết định

- Toàn bộ sản phẩm là web theo cấu trúc spec §17 (`apps/web`, `apps/api` FastAPI + WebSocket, ...). Không dùng Unity.
- Virtual Office 2D được fork từ Pixel Office, đặt trong `apps/web/` và `packages/office-renderer/`.
- Renderer chỉ là projection từ event stream (§10); 3D renderer (P2) sau này dùng chung event API.

## Upstream

- Repo: https://github.com/basriayaz/pixel-office (Basri Ayaz), v0.1.0, nhánh `main`, push gần nhất 2026-10-06.
- License: **MIT** (kiểm tra 2026-10-28). Khi fork phải giữ nguyên `LICENSE` và dòng copyright "Copyright (c) 2026 Basri Ayaz"; ghi upstream + commit gốc trong `apps/web/UPSTREAM.md`.
- Sprite/nhân vật được vẽ bằng code (`web/office.js`, `web/sprites.html`); README không ghi asset bên thứ ba nào khác. Logo trong `web/logos/` (Claude, Codex, Gemini, OpenRouter) là nhãn hiệu của chủ sở hữu tương ứng, không thuộc phạm vi MIT.
- Stack upstream: Node ≥20, TypeScript server Express 5 + `ws` + Claude Agent SDK (`src/server/`), frontend JS thuần (`web/`). Đã có sẵn các khái niệm gần với spec: `meeting.ts`, `board.ts`, `events.ts`, `runs.ts`, `retry.ts`, `ledger.ts`/`prices.ts`, `codex.ts`/`gemini.ts`.
- Khác biệt với spec: upstream mô phỏng "thuê nhân viên" (employee/hire/market), không có Orchestrator duy nhất, không phân tách theo project — trái với BR-01/BR-02/BR-04 và §19. Phải sửa mô hình này khi fork.

## Việc cần làm trước khi fork

- Đánh giá cơ chế activity event của Pixel Office và cách map sang event protocol của dự án (`task.started`, `message.sent`, `meeting.started`, ...).
- Đảm bảo UI không hiển thị trạng thái khi thiếu event (BR-09, AC-05).

## Backend (chốt 2026-10-28)

- Chỉ fork frontend `web/` của upstream → `apps/web/` (commit `4ebfdc7`, xem `apps/web/UPSTREAM.md`).
- Backend là **FastAPI + WebSocket** trong `apps/api/` theo spec §17, viết mới. Không dùng server Node/TypeScript (`src/server/`) của upstream; chỉ tham khảo ý tưởng (meeting, board, events, retry, ledger).
- Lý do: mô hình dữ liệu của upstream (employee/hire/market, không project scoping, không Orchestrator duy nhất) lệch các quy tắc cốt lõi BR-01/02/04, giữ server thì vẫn phải viết lại phần lớn; FastAPI giữ đúng spec.
- Hệ quả: frontend phải chuyển từ các endpoint `/api/*` của upstream sang API spec §11 (`/projects/{id}/...`, `WS /projects/{id}/stream`).

## Hệ quả

- Không có asset/scene/script Unity trong repo.
- Phải theo dõi thay đổi upstream của Pixel Office và ghi lại các điểm đã sửa so với bản gốc.
