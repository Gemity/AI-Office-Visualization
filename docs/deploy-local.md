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

## Chạy nền bằng systemd --user

Mẫu nằm trong `infra/systemd/` và giả định repo ở `~/UnityProject/AI-Office-Visualization`. Nếu khác, sửa `WorkingDirectory`.

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
uv run office-api backup --keep 14   # timer mẫu chạy hằng ngày
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

## Ngân sách

Mặc định không giới hạn. Đặt trần trong tab **Chi phí** của UI hoặc qua API:

```bash
curl -X PUT http://127.0.0.1:8000/projects/<id>/budget \
  -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: $(uuidgen)" -H "Content-Type: application/json" \
  -d '{"max_cost_usd": 5, "agents": {"agt_…": 1}}'
```

Khi chạm trần, hệ thống tự tạm dừng project. Nâng trần rồi bấm "Tiếp tục project".
