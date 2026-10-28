AI PROJECT OFFICE
Software Requirements & Architecture Specification — Version 2.0
Project-centric · Single Orchestrator · Multi-Agent · Observable by Design
Status: Proposed baseline  |  Date: 28/10/2026  |  Language: Vietnamese
Tài liệu thiết kế nền tảng; nhiệm vụ cụ thể và lựa chọn nhân sự được cấu hình riêng cho từng project.
# 1. Executive summary
AI Project Office là không gian làm việc ảo dành cho một project tại một thời điểm. Chủ dự án (Owner) chỉ trao đổi với một Project Orchestrator Agent. Orchestrator lập kế hoạch, phân công và điều phối nhiều worker agents (Claude, Codex, Suno, Tripo, custom agents...) tùy năng lực được khai báo. Owner quan sát trực tiếp trạng thái, cuộc họp, trao đổi giữa agent, task, artifact và quyết định; mọi can thiệp thực thi được gửi qua Orchestrator hoặc cơ chế phê duyệt của runtime.
Nguyên tắc kiến trúc: One Project → One Active Orchestrator → Many Workers → One Observable Workspace. Một hệ thống có thể lưu nhiều project, nhưng màn hình làm việc, sự kiện, quyền và dữ liệu luôn được lọc theo project đang mở.
# 2. Phạm vi, giả định và không thuộc phạm vi
## 2.1 In scope
- Tạo/chọn project; gán một Orchestrator hoạt động và nhiều worker agents theo project.
- Chat Owner ↔ Orchestrator; kế hoạch, giao việc, họp nhóm, DM giữa agents và handoff.
- Virtual Office 2D, activity feed, conversation explorer, task board, artifact browser, approvals.
- Agent adapter chuẩn hóa API/CLI/MCP/job-based tools; audit, metrics, chi phí, retry và recovery.
- Quản lý project context, permissions, budget, retention và policy.
## 2.2 Out of scope cho MVP
- Mô phỏng công ty có CEO/phòng ban cố định, hệ thống HR/payroll.
- Tự động quyết định loại công việc cho mọi project bằng workflow cứng.
- Bắt buộc tích hợp Suno/Tripo nếu chưa có API/quyền truy cập hợp lệ.
- Truy cập suy nghĩ nội bộ/hidden reasoning hoặc hội thoại không được provider cung cấp.
- Agent tự triển khai production, thanh toán hoặc công bố nội dung khi chưa được phê duyệt.
# 3. Các vai trò và khái niệm
| Thực thể | Định nghĩa |
| Project | Ranh giới dữ liệu, task, hội thoại, artifacts, policy và ngân sách. |
| Owner | Người dùng duy nhất trao đổi với Orchestrator trong luồng vận hành chính. |
| Orchestrator | Một agent hoạt động tại một thời điểm/project; điều phối, tổng hợp và chịu trách nhiệm báo cáo. |
| Worker Agent | Agent thực hiện task; có thể là conversational LLM hoặc tool-based adapter. |
| Agent Definition | Danh tính logic, capabilities, provider config; tách khỏi model thực thi. |
| Agent Assignment | Vai trò, quyền, ngân sách và trạng thái agent trong một project. |
| Conversation | Group meeting, private DM, hoặc task thread có participant và visibility. |
| Artifact | Kết quả có version và provenance: code, audio, 3D, tài liệu, log. |

# 4. Quy tắc nghiệp vụ bắt buộc
- BR-01: Một project có tối đa một active Orchestrator tại một thời điểm; cho phép thay thế có version và audit.
- BR-02: Owner chat trực tiếp với Orchestrator; worker không tự tạo kênh yêu cầu mới đến Owner.
- BR-03: Worker được giao nhiệm vụ theo capabilities và policy, không hardcode Claude/Codex/Suno/Tripo vào role.
- BR-04: Mọi event, task, conversation và artifact phải có project_id; kiểm soát truy cập tại backend.
- BR-05: Agent-to-agent DM và group meeting được phép theo policy; transcript được ghi lại nếu nguồn cung cấp dữ liệu.
- BR-06: Owner có thể quan sát và gửi lệnh pause/resume/cancel/redirect; runtime xác nhận trạng thái thực tế.
- BR-07: Actions rủi ro cao cần approval; approval có expiry, scope, approver và audit.
- BR-08: Worker có thể chạy song song; Orchestrator không được là điểm lưu trạng thái duy nhất.
- BR-09: Không công bố “live” hoặc “complete” khi chưa có event/trạng thái được xác nhận.
# 5. Kiến trúc logic
Owner UI (Project Chat | Virtual Office | Task Board | Audit)
                   | REST / WebSocket
             Project API Gateway
                   |
     +-------------+------------------+
     |             |                  |
Orchestration   Conversation      Observation
Runtime         & Meeting Hub     & Projection
     |             |                  |
     +---------- Event Bus ------------+
                   |
          Agent Adapter Registry
        /       |       |        \
     Claude   Codex   Suno      Tripo / Custom
                   |
    PostgreSQL | Redis Streams | Object Storage
                OpenTelemetry / Langfuse
Runtime là bộ máy thực thi bền vững: lập lịch, checkpoint, retry, timeout, idempotency và policy enforcement. Orchestrator là agent quyết định chiến lược, không trực tiếp sở hữu toàn bộ trạng thái hệ thống.
# 6. Các module
| Module | Trách nhiệm | MVP |
| Project Workspace | Project config, membership, active orchestrator, budget | P0 |
| Command Center | Owner ↔ Orchestrator chat; status; approvals | P0 |
| Orchestration Runtime | Task DAG, dispatch, checkpoints, recovery | P0 |
| Agent Registry & Adapters | Capabilities, invocation, result normalization | P0 |
| Communication Hub | Group/DM/task thread, transcript, turn control | P0 |
| Observation & Virtual Office | Event timeline, status, click-through trace | P0 |
| Task & Artifact Monitor | Dependency, blocker, output provenance | P0 |
| Governance | RBAC, sandbox, approvals, quotas, audit | P0 |
| Cost & Quality Analytics | Token/job cost, quality score, tracing | P1 |
| 3D Office renderer | Thay thế renderer 2D, dùng chung event API | P2 |

# 7. Hợp đồng Agent Adapter
Mọi adapter khai báo capabilities (chat, code, image, audio, 3d, tool_call, async_job), invocation_mode (sync, stream, async_job), required_permissions, supported_artifact_types, cost_model và observability_level.
interface AgentAdapter {
  describeCapabilities(): CapabilityManifest
  invoke(request: AgentInvocation): Promise<InvocationHandle>
  getStatus(handleId: string): Promise<AgentStatus>
  cancel?(handleId: string): Promise<CancelResult>
  subscribeEvents?(handleId: string): AsyncIterable<AgentEvent>
  collectArtifacts?(handleId: string): Promise<ArtifactRef[]>
}
Claude/Codex có thể sử dụng SDK/API hoặc CLI wrapper phù hợp. Suno/Tripo được coi là tool-based async workers trừ khi nhà cung cấp hỗ trợ hội thoại; trạng thái “đang tạo” dựa trên job status thực tế. Không dùng scraping trái điều khoản dịch vụ.
# 8. Workflow chính
## 8.1 Từ yêu cầu đến kết quả
Owner message → Orchestrator interprets → Plan proposed
→ Tasks created (DAG) → Policy check → Worker dispatch
→ Progress/events/DM/meetings → Artifact validation
→ Orchestrator synthesis → Owner report / approval
## 8.2 Cuộc họp
Meeting lifecycle: proposed → scheduled → active → decision_recorded → action_items_created → closed. Mỗi meeting có moderator, participants, agenda, turn_limit, time_limit, token_budget, minutes và decisions. Private DM được đánh dấu private đối với worker không tham gia, nhưng Owner có thể có quyền audit theo cấu hình.
## 8.3 Can thiệp
Owner nhắn Orchestrator “tạm dừng task X”; API tạo intervention_request; runtime kiểm tra quyền, gửi cancel/pause tới worker, cập nhật trạng thái thực tế rồi thông báo lại. Nếu provider không hỗ trợ cancel, UI hiển thị “cancel requested” cho đến khi xác minh.
# 9. Data model tối thiểu
| Bảng | Trường quan trọng |
| projects | id, name, status, config, budget, created_by |
| agent_definitions | id, provider, adapter_type, capability_manifest, config_ref |
| project_agents | project_id, agent_id, role, permissions, enabled |
| orchestrator_assignments | project_id, agent_id, active_from, active_to, version |
| conversations | id, project_id, type, visibility, status, created_by |
| conversation_participants | conversation_id, agent_id, role |
| messages | id, project_id, conversation_id, sender_id, content_ref, parent_id |
| tasks | id, project_id, parent_id, assignee_id, status, priority, deadline |
| task_dependencies | task_id, depends_on_id |
| meetings | id, project_id, agenda, moderator_id, status, decisions_ref |
| artifacts | id, project_id, task_id, type, uri, checksum, version |
| events | id, project_id, seq, type, actor_id, payload, occurred_at |
| approvals | id, project_id, action, requester_id, status, expires_at |
| agent_runs | id, project_id, task_id, adapter, status, trace_id, cost |
| audit_logs | id, project_id, principal, action, resource, outcome |

Tất cả bảng liên quan project phải dùng composite indexes theo (project_id, created_at/status) và kiểm tra tenant/project boundary. Nội dung lớn hoặc nhạy cảm có thể lưu object storage qua content_ref thay vì trong events.
# 10. Event protocol & realtime
{
  "event_id": "evt_01",
  "project_id": "prj_alpha",
  "sequence": 42,
  "type": "message.sent",
  "actor_id": "agent_claude",
  "conversation_id": "conv_17",
  "task_id": "task_09",
  "trace_id": "trace_abc",
  "occurred_at": "2026-10-28T09:42:00+07:00",
  "payload": {"recipient_ids": ["agent_codex"], "content_ref": "msg_42"}
}
Event types: project.created, agent.assigned, task.created, task.started, task.blocked, task.completed, message.sent, meeting.started, meeting.ended, decision.recorded, artifact.created, approval.requested, approval.resolved, run.failed, intervention.requested, intervention.applied. Event stream hỗ trợ replay theo sequence và de-dup bằng event_id.
# 11. API contract sơ bộ
| Endpoint | Mục đích |
| POST /projects | Tạo project |
| GET /projects/{id} | Thông tin và cấu hình project |
| POST /projects/{id}/orchestrator/messages | Owner chat với Orchestrator |
| GET /projects/{id}/agents | Agent và trạng thái |
| GET /projects/{id}/tasks | Task board |
| GET /projects/{id}/conversations | Meeting/DM/task threads |
| GET /projects/{id}/conversations/{cid}/messages | Transcript theo quyền |
| GET /projects/{id}/artifacts | Danh sách artifacts |
| POST /projects/{id}/interventions | Pause/resume/cancel/redirect |
| POST /projects/{id}/approvals/{aid}/decision | Approve/reject |
| GET /projects/{id}/events?after_seq=N | Replay event |
| WS /projects/{id}/stream | Realtime event subscription |

Tất cả write endpoints nhận idempotency key; response trả request_id, operation status và lỗi có mã. WebSocket phải xác thực và giới hạn theo project.
# 12. UX / Visualization specification
- Project Selector: chỉ chuyển project, không trộn dữ liệu giữa các project.
- Command Center: chat chính, plan, progress, yêu cầu phê duyệt, tóm tắt của Orchestrator.
- Office View: avatar theo agent, room theo activity; click agent mở task/run/trace, không giả lập trạng thái khi thiếu event.
- Meeting Room: participants, transcript theo thời gian, decisions, action items, live/past indicator.
- Conversation Explorer: group/DM, bộ lọc agent, task, thời gian, quyền đọc.
- Task View: kanban hoặc dependency graph, trạng thái blocked/waiting/active/completed.
- Artifact Viewer: preview/download theo loại file và provenance từ task/run.
- Audit/Cost View: timeline, retries, failures, approvals, chi phí theo agent và project.
# 13. Security, governance & reliability
- Project-scoped RBAC, server-side authorization, secret vault, mã hóa transit/at-rest, log redaction.
- Worker coding agents chạy trong sandbox; network/filesystem/tool permissions theo task.
- Approval bắt buộc cho deploy production, thanh toán, xóa dữ liệu, xuất dữ liệu nhạy cảm.
- Per-project và per-agent budget; circuit breaker, concurrency limit, token/time limit.
- Checkpoint sau bước quan trọng; retry với backoff; idempotent dispatch; dead-letter queue.
- Không hiển thị chain-of-thought nội bộ; chỉ transcript, tool calls, summaries và events được phép cung cấp.
- Audit trail bất biến logic; retention configurable và quyền xóa/export theo policy.
# 14. Non-functional requirements (mục tiêu ban đầu)
| Tiêu chí | Mục tiêu MVP |
| Event UI latency | p95 < 2 giây sau khi backend nhận event (môi trường chuẩn) |
| Event delivery | At-least-once, idempotent consumer, replay theo sequence |
| Recovery | Restart runtime không mất task/event đã commit |
| Concurrent workers | Tối thiểu 5 worker/project (tùy provider quota) |
| Isolation | Không đọc/ghi cross-project qua API thông thường |
| Observability | Mỗi task/run có trace_id, actor_id, status và timestamps |
| Availability | Mục tiêu thử nghiệm; SLA production xác định sau load test |

# 15. Acceptance criteria / kiểm thử nghiệm thu
- AC-01: Tạo Project A và B; mở A không thấy transcript, task, artifact của B.
- AC-02: Owner chỉ chat Orchestrator của project; worker không gửi chat trực tiếp đến Owner.
- AC-03: Orchestrator tạo ít nhất 3 task và giao 2 worker chạy song song.
- AC-04: Hai worker trao đổi DM; một group meeting được lưu và replay trong UI.
- AC-05: Click agent trong Office xem task/run/transcript tương ứng, không hiển thị dữ liệu bịa.
- AC-06: Owner gửi pause; UI phân biệt requested và confirmed, hỗ trợ resume.
- AC-07: Task thất bại được retry theo policy, audit lưu nguyên nhân và lịch sử.
- AC-08: Tool-based agent (mock Suno/Tripo) nhận async job, trả trạng thái và artifact.
- AC-09: Hành động rủi ro cao bị chặn đến khi approval hợp lệ.
- AC-10: Khởi động lại backend, event replay và trạng thái task được khôi phục.
# 16. Roadmap triển khai
| Giai đoạn | Đầu ra |
| Phase 0 (1 tuần) | Chốt schema, adapter contract, project-scoped auth, prototype event stream |
| Phase 1 (2 tuần) | Project workspace, Orchestrator chat, task runtime, Claude/Codex adapters |
| Phase 2 (2 tuần) | Meeting/DM hub, 2D Office, timeline, replay, interventions |
| Phase 3 (2 tuần) | Async tool adapters (mock trước), artifacts, approvals, metrics |
| Phase 4 (1–2 tuần) | Hardening: sandbox, recovery, load/security tests, deploy beta |

Thời gian chỉ là ước lượng triển khai MVP cho nhóm có kinh nghiệm; phụ thuộc SDK/provider access và phạm vi UI.
# 17. Repository structure đề xuất
ai-project-office/
  apps/web/                   # Project UI + Pixel Office
  apps/api/                   # FastAPI + WebSocket
  services/orchestrator/      # Workflow runtime
  services/agent-workers/     # Worker execution/sandbox
  packages/contracts/         # OpenAPI + event JSON schemas
  packages/agent-adapters/    # Claude/Codex/Suno/Tripo/custom
  packages/office-renderer/   # UI projection from events
  infra/docker/               # Local Compose
  docs/adr/                   # Architecture decisions
  tests/e2e/                  # Cross-agent workflows
# 18. Quyết định và câu hỏi còn mở
- Chọn runtime: LangGraph + durable execution hay một workflow engine chuyên dụng; cần spike recovery.
- Chọn UI base: fork Pixel Office sau khi kiểm tra license, activity event và khả năng mở rộng.
- Chọn cơ chế tích hợp Claude/Codex (SDK/API/CLI) theo môi trường và quyền sử dụng.
- Xác minh API chính thức và quota của Suno/Tripo trước khi bật production adapter.
- Xác định policy mặc định về transcript riêng, retention, ngân sách và quyền can thiệp.
- Xác định deployment local-first, server 24/7 hay hybrid ở giai đoạn implementation.
# 19. Thay đổi so với v1.0
| v1.0 | v2.0 |
| Company là root; nhiều project | Project là workspace trung tâm; nhiều project chỉ là container quản lý |
| Công ty/phòng ban/CEO cố định | Một Orchestrator được gán động cho mỗi project |
| Owner có thể giao việc đa kênh | Owner chat duy nhất với Orchestrator |
| Agent role thiên về tổ chức | Agent capabilities và assignment theo project |
| Office là mô phỏng công ty | Office là observation surface cho một project |
| Orchestration luôn hiện diện | Runtime hỗ trợ managed execution, nhưng visualization tách khỏi execution |

END OF SPECIFICATION — v2.0
