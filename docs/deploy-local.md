# Chạy AI Project Office trên máy cá nhân (local-first)

Mô hình (ADR-0006): một máy, một Owner, SQLite, chạy nền bằng systemd `--user`, chỉ nghe trên `127.0.0.1`.

## Yêu cầu

- `uv` (ở `~/.local/bin/uv`), Python ≥ 3.12 do `uv` quản lý.
- `bwrap` (bubblewrap) cho sandbox mặc định: `sudo apt install bubblewrap`. Kiểm tra: `bwrap --ro-bind / / true`.
  - Không dùng được bwrap ⇒ đặt `OFFICE_SANDBOX=none` (agent CLI chạy **không** sandbox, tự chịu rủi ro).
- Adapter thật: CLI `claude` / `codex` đã đăng nhập bằng chính user sẽ chạy service.

## Biến môi trường

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `OFFICE_DATABASE_URL` | `sqlite+aiosqlite:///./office.db` | Database |
| `OFFICE_DATA_DIR` | `./data` | Content store, workspace của agent, backup |
| `OFFICE_WEB_DIR` | `apps/web` | Thư mục UI; rỗng = chỉ API |
| `OFFICE_SANDBOX` | `bwrap` | `bwrap` hoặc `none` |
| `OFFICE_POLICY_FILE` | `./office-policy.toml` | File policy mặc định (xem dưới) |

Không để token hay secret trong các biến này.

## Lần đầu

```bash
uv sync
export OFFICE_DATABASE_URL=sqlite+aiosqlite:///$HOME/.local/share/ai-project-office/office.db
export OFFICE_DATA_DIR=$HOME/.local/share/ai-project-office/data
mkdir -p ~/.local/share/ai-project-office
uv run office-api migrate
uv run office-api create-owner --name "Tên"   # token chỉ in một lần — lưu vào password manager
```

DB tạo bằng phiên bản trước Phase 4 (`create_all`) sẽ bị từ chối. Với dữ liệu dev thì xóa file DB cũ rồi chạy lại.

## Launcher bấm-là-chạy (`scripts/office-launch.py`)

Chỉ cần mang **một file**. Chép vào repo muốn làm việc (`<repo>/.claude/office-launch.py`, `.agents/` hoặc gốc repo) rồi `chmod +x`. Cấu hình nằm ở khối **CẤU HÌNH** đầu file:
- `OFFICE_HOME`: nơi cài AI Project Office. Để trống (mặc định) thì dùng quy tắc đặt chung bên dưới;
- `PORT`;
- `ORCHESTRATOR`: tên, adapter, config (model, effort, system_prompt…).

Mỗi lần double-click hoặc chạy `./office-launch.py`:
- khởi động server nếu chưa chạy (có unit systemd thì dùng `systemctl --user start`, không thì chạy nền, log ở `~/.local/share/ai-project-office/server.log`);
- lần đầu: tạo Owner, lưu token vào `~/.config/ai-project-office/token` (quyền 600). DB đã có Owner thì hỏi token qua hộp thoại;
- tìm hoặc tạo project gắn với repo (`config.repo_path`, tên = tên thư mục repo);
- đảm bảo project có Điều phối đúng `ORCHESTRATOR`. Lần đầu thì tạo; config khác thì thay bằng phiên bản mới (BR-01: có version + event `agent.assigned`, bản cũ còn trong lịch sử);
- mở trình duyệt đã đăng nhập sẵn vào project: chat với Điều phối được ngay.

Điều phối không có avatar trong Office và không thêm/sửa từ UI. Tab Agent dùng để thêm, sửa, xoá worker (chọn capabilities bằng checkbox, có hướng dẫn từng khóa config và nút "Điền mẫu"). Adapter `claude-cli` chạy CLI `claude`, `codex-cli` chạy CLI `codex`, cả hai dùng đăng nhập sẵn có trên máy.

Tùy chọn: `--stop` dừng server; `--no-browser` chỉ in URL.

### Quy tắc đặt thư mục (dùng chung trên nhiều máy)

Clone `AI-Office-Visualization` **cùng cấp** với các repo làm việc, tức là cùng thư mục cha:

```
~/UnityProject/
├── AI-Office-Visualization/     # server (git clone, đúng tên này)
├── lib-factory/.claude/office-launch.py
└── repo-khac/.agents/office-launch.py
```

Launcher tìm server theo thứ tự: `OFFICE_HOME` ở đầu file → biến môi trường `OFFICE_HOME` → `<thư mục cha của repo>/AI-Office-Visualization`. Nhờ vậy cùng một file launcher (commit vào repo target) chạy được trên mọi máy theo quy tắc này, không phải sửa. Máy nào đặt khác thì đặt `OFFICE_HOME` trong môi trường của máy đó.

Mỗi máy có server, DB, Owner và token **riêng** (`~/.local/share/ai-project-office/`, `~/.config/ai-project-office/token`). Lịch sử chat và worker không đồng bộ giữa các máy.

GNOME/Nautilus mặc định mở file `.py` bằng trình soạn thảo. Muốn double-click là chạy: Files → Preferences → "Executable Text Files" → **Run** (hoặc **Ask**).

## Chạy nền bằng systemd --user

Mẫu nằm trong `infra/systemd/` và giả định repo ở `~/UnityProject/AI-Office-Visualization`. Nếu máy này đặt khác (theo quy tắc cùng cấp ở trên), sửa `WorkingDirectory` sau khi chép unit.

```bash
cp infra/systemd/ai-project-office.service \
   infra/systemd/ai-project-office-backup.service \
   infra/systemd/ai-project-office-backup.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ai-project-office.service ai-project-office-backup.timer
loginctl enable-linger "$USER"     # tùy chọn: chạy cả khi chưa đăng nhập
```

- Ghi đè biến môi trường trong `~/.config/ai-project-office/env` (vd. `OFFICE_SANDBOX=none`).
- Log: `journalctl --user -u ai-project-office -f`. Token trong log đã được che.
- Dừng / khởi động lại: `systemctl --user restart ai-project-office`. `SIGTERM` là tắt êm: job async (Suno/Tripo) đang chạy được thả ra và gắn lại khi server lên lại, không submit lại.

## Healthcheck

```bash
curl -fsS http://127.0.0.1:8000/healthz   # tiến trình còn sống
curl -fsS http://127.0.0.1:8000/readyz    # DB trả lời + dispatcher đang chạy (503 nếu không)
```

## Backup / khôi phục

```bash
uv run office-api backup   # timer mẫu chạy hằng ngày; số bản giữ lại = retention.backups_keep trong office-policy.toml
```

Tạo `$OFFICE_DATA_DIR/backups/office-YYYYmmdd-HHMMSS.tar.gz` (quyền 600), gồm `office.db` (snapshot nhất quán) và `data/` (content store, workspace). Nên chép bản backup ra ổ khác.

Khôi phục:

```bash
systemctl --user stop ai-project-office
cd ~/.local/share/ai-project-office
mv office.db office.db.old && mv data data.old
tar -xzf data.old/backups/office-….tar.gz            # tạo office.db và data/
systemctl --user start ai-project-office              # tự migrate lên schema mới nhất
```

## Mạng

Giữ `--host 127.0.0.1`. Trình duyệt gửi token qua query `?token=` của WebSocket, và server chưa có TLS. Muốn truy cập từ máy khác thì đi qua SSH tunnel (`ssh -L 8000:127.0.0.1:8000 máy-chủ`), đừng mở port.

## Policy mặc định (`office-policy.toml`)

File ở gốc repo, có chú thích từng khóa (ADR-0007):
- transcript DM riêng ai được đọc;
- retention: số ngày giữ nội dung/workspace, số bản backup;
- ngân sách mặc định cho project mới;
- hành động can thiệp observer được gửi;
- TTL approval.

Server chỉ đọc file khi khởi động: sửa xong chạy `systemctl --user restart ai-project-office`. File sai khóa/giá trị ⇒ service không lên, xem lỗi bằng `journalctl --user -u ai-project-office`.
Xem trước retention sẽ xóa gì: `uv run office-api prune --dry-run`.

## Ngân sách

Mặc định không giới hạn (đổi cho project mới bằng `[budget] default_max_cost_usd`). Đặt trần trong tab **Chi phí** của UI hoặc qua API:

```bash
curl -X PUT http://127.0.0.1:8000/projects/<id>/budget \
  -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: $(uuidgen)" -H "Content-Type: application/json" \
  -d '{"max_cost_usd": 5, "agents": {"agt_…": 1}}'
```

Khi chạm trần, hệ thống tự tạm dừng project. Nâng trần rồi bấm "Tiếp tục project".
