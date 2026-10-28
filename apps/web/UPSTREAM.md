# Upstream

Thư mục này được fork từ `web/` của Pixel Office.

- Repo: https://github.com/basriayaz/pixel-office
- Commit gốc: `4ebfdc7e4538eb68ea987f57ccf2810bc488a39c` (2026-10-06)
- License: MIT — xem `LICENSE` (Copyright (c) 2026 Basri Ayaz). Không xóa hoặc sửa file này.
- Logo trong `logos/` là nhãn hiệu của chủ sở hữu tương ứng, không thuộc MIT; giữ lại vì dự án chỉ dùng cá nhân, phi thương mại.

Chỉ fork frontend; backend upstream (`src/server/`, Node/Express) **không** được dùng — backend của dự án là FastAPI trong `apps/api/` (ADR-0001).

## Trạng thái

Từ Phase 2 (ADR-0004), trang chính `index.html` là UI mới của AI Project Office (`po-app.js`, `po.css`). UI này dùng API spec §11 (`/projects/{id}/...`, `WS /projects/{id}/stream`) và chỉ tái sử dụng renderer (`layout.js`, `office.js`), `style.css` và asset của upstream. Shell upstream (`app.js`, `i18n.js`, ...) vẫn gọi `/api/*` của server Node; các endpoint đó không tồn tại nên shell này chỉ giữ để tham chiếu.

## Thay đổi so với upstream

Ghi lại mọi thay đổi đáng kể ở đây (ngày, mô tả, lý do).

- 2026-10-28 — `index.html` upstream đổi tên (`git mv`) thành `pixel-office-upstream.html`. `index.html` mới, `po-app.js` và `po.css` là code của dự án: UI project-scoped nối REST + event stream.
- 2026-10-28 — `office.js`: thêm cờ `observeOnly` để tắt mọi hoạt động giả lập (chat ngẫu nhiên, sự kiện vui, an ủi, `say()` tự sinh) vì Office phải là projection từ event thật (BR-09). Thêm `speak(id, text, ms)` để hiện bong bóng thoại từ `message.sent` thật.
