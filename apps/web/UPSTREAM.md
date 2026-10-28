# Upstream

Thư mục này được fork từ `web/` của Pixel Office.

- Repo: https://github.com/basriayaz/pixel-office
- Commit gốc: `4ebfdc7e4538eb68ea987f57ccf2810bc488a39c` (2026-10-06)
- License: MIT — xem `LICENSE` (Copyright (c) 2026 Basri Ayaz). Không xóa hoặc sửa file này.
- Logo trong `logos/` là nhãn hiệu của chủ sở hữu tương ứng, không thuộc MIT.

Chỉ fork frontend; backend upstream (`src/server/`, Node/Express) **không** được dùng — backend của dự án là FastAPI trong `apps/api/` (ADR-0001).

## Trạng thái

Bản copy nguyên trạng, chưa sửa. Frontend hiện gọi các endpoint `/api/*` của server upstream (`/api/employees`, `/api/offices`, `/api/integrations`, `/api/skills`, `/api/providers`, `/api/settings`, ...) và một WebSocket; các endpoint này chưa tồn tại. Cần chuyển sang API theo spec §11 (`/projects/{id}/...`, `WS /projects/{id}/stream`).

## Thay đổi so với upstream

Ghi lại mọi thay đổi đáng kể ở đây (ngày, mô tả, lý do).
