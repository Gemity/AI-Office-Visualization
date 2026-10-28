"""Mock tool-based job providers (AC-08): music generation and 3D model generation.

They stand in for Suno / Tripo until official APIs and quotas are verified
(spec §18). Each adapter talks to an in-process ``MockJobService`` that behaves
like a remote job API: jobs are queued, run with coarse progress, then succeed
with generated files (a real WAV melody / a real OBJ mesh derived from the
prompt), fail, or are cancelled. No network calls. Like a remote provider, the
service outlives the caller: each job is persisted (``.mockjob-<id>.json`` in
the task workdir, a dot-file so it is never an artifact) and a new process can
re-attach to it.

Config (both):
  duration_s: float     job run time, default 3
  poll_s: float         adapter poll interval, default 0.5
  fail_first: int       first N jobs fail with a retryable provider error
  price_usd: float      notional price per job (reported as cost), default none
"""

import hashlib
import io
import json
import math
import struct
import time
import uuid
import wave
from dataclasses import asdict, dataclass, field
from pathlib import Path

from office_contracts import AgentError, AgentInvocation, CapabilityManifest

from agent_adapters.jobs import AsyncJobAdapter, JobStatus
from agent_adapters.runner import AdapterRunError


@dataclass
class MockJob:
    id: str
    prompt: str
    submitted_at: float
    fails: bool
    store: str = ""  # directory holding the job record
    state: str = "queued"
    cancelled_at: float | None = None
    outputs: dict[str, bytes] = field(default_factory=dict)


class MockJobService:
    """Provider-side job store; job state is a function of elapsed wall-clock time."""

    def __init__(self, duration_s: float, make_outputs):
        self.duration_s = duration_s
        self.make_outputs = make_outputs  # prompt -> (outputs, message)
        self.jobs: dict[str, MockJob] = {}

    def submit(self, prompt: str, fails: bool, store: str) -> str:
        job = MockJob(
            id=f"job_{uuid.uuid4().hex[:16]}",
            prompt=prompt,
            submitted_at=time.time(),
            fails=fails,
            store=store,
        )
        self.jobs[job.id] = job
        self._save(job)
        return job.id

    def attach(self, job_id: str, store: str) -> MockJob:
        if job_id not in self.jobs:
            path = Path(store) / f".mockjob-{job_id}.json"
            if not path.is_file():
                raise AdapterRunError("job_not_found", f"unknown job {job_id}", retryable=True)
            self.jobs[job_id] = MockJob(**json.loads(path.read_text()))
        return self.jobs[job_id]

    def _save(self, job: MockJob) -> None:
        record = {k: v for k, v in asdict(job).items() if k != "outputs"}
        (Path(job.store) / f".mockjob-{job.id}.json").write_text(json.dumps(record))

    def poll(self, job_id: str) -> tuple[MockJob, float | None, str]:
        job = self.jobs[job_id]
        if job.state in {"succeeded", "failed", "cancelled"}:
            return job, 1.0 if job.state == "succeeded" else None, ""
        elapsed = time.time() - job.submitted_at
        queue_s = self.duration_s * 0.2
        if elapsed < queue_s:
            return job, None, ""
        if elapsed < self.duration_s:
            job.state = "running"
            # Providers report coarse progress; so does the mock (10% steps).
            return job, math.floor((elapsed - queue_s) / (self.duration_s - queue_s) * 10) / 10, ""
        if job.fails:
            job.state = "failed"
            self._save(job)
            return job, None, ""
        job.outputs, message = self.make_outputs(job.prompt)
        job.state = "succeeded"
        self._save(job)
        return job, 1.0, message

    def cancel(self, job_id: str) -> None:
        job = self.jobs[job_id]
        if job.state in {"queued", "running"}:
            job.state, job.cancelled_at = "cancelled", time.time()
            self._save(job)


class _MockJobAdapter(AsyncJobAdapter):
    provider = ""
    capabilities: list[str] = []
    artifact_types: list[str] = []

    def __init__(self, config=None):
        super().__init__(config)
        self.service = MockJobService(float(self.config.get("duration_s", 3)), self._outputs)
        self._submitted = 0
        self._messages: dict[str, str] = {}

    def describe_capabilities(self) -> CapabilityManifest:
        price = self.config.get("price_usd")
        return CapabilityManifest(
            adapter_type=self.adapter_type,
            provider=self.provider,
            capabilities=self.capabilities,
            invocation_modes=["async_job"],
            supported_artifact_types=self.artifact_types,
            cost_model={"unit": "job", "unit_price": price, "notes": "mock, notional price"},
            observability_level="status_only",
            supports_cancel=True,
        )

    def _outputs(self, prompt: str) -> tuple[dict[str, bytes], str]:
        raise NotImplementedError

    async def _submit(self, invocation: AgentInvocation) -> str:
        self._submitted += 1
        fails = self._submitted <= int(self.config.get("fail_first", 0))
        return self.service.submit(invocation.instructions, fails, invocation.workdir)

    async def _attach(self, invocation: AgentInvocation, job_id: str) -> None:
        self.service.attach(job_id, invocation.workdir)

    async def _poll(self, job_id: str) -> JobStatus:
        job, progress, message = self.service.poll(job_id)
        if message:
            self._messages[job_id] = message
        price = self.config.get("price_usd")
        return JobStatus(
            state=job.state,
            progress=progress,
            message=self._messages.get(job_id, ""),
            error=AgentError(code="provider_error", message="mock job failed", retryable=True)
            if job.state == "failed"
            else None,
            outputs=job.outputs,
            cost_usd=float(price) if price is not None and job.state == "succeeded" else None,
        )

    async def _cancel_job(self, job_id: str) -> None:
        self.service.cancel(job_id)


def _seed(prompt: str) -> bytes:
    return hashlib.sha256(prompt.encode()).digest()


class MockMusicJobAdapter(_MockJobAdapter):
    """Suno-style music generation: prompt in, WAV track out."""

    adapter_type = "mock-suno"
    provider = "suno (mock)"
    capabilities = ["audio", "async_job"]
    artifact_types = ["audio"]

    def _outputs(self, prompt: str) -> tuple[dict[str, bytes], str]:
        seed = _seed(prompt)
        rate, note_s = 22050, 0.3
        scale = [261.63, 293.66, 329.63, 392.0, 440.0, 523.25, 587.33, 659.25]  # C pentatonic-ish
        notes = [scale[b % len(scale)] for b in seed[:16]]
        frames = bytearray()
        for i, freq in enumerate(notes):
            for n in range(int(rate * note_s)):
                t = n / rate
                env = min(1.0, t * 40) * max(0.0, 1 - t / note_s) ** 1.5
                sample = math.sin(2 * math.pi * freq * t) + 0.3 * math.sin(4 * math.pi * freq * t)
                if i % 4 == 0:  # accent the beat
                    sample *= 1.2
                frames += struct.pack("<h", int(max(-1, min(1, sample * 0.4 * env)) * 32767))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(bytes(frames))
        seconds = len(notes) * note_s
        return {
            "track.wav": buf.getvalue()
        }, f"Generated track.wav ({seconds:.1f}s, mock music job)"


class MockModelJobAdapter(_MockJobAdapter):
    """Tripo-style 3D generation: prompt in, OBJ mesh out."""

    adapter_type = "mock-tripo"
    provider = "tripo (mock)"
    capabilities = ["3d", "async_job"]
    artifact_types = ["3d"]

    def _outputs(self, prompt: str) -> tuple[dict[str, bytes], str]:
        seed = _seed(prompt)
        sides = 3 + seed[0] % 6  # a prism/pyramid with 3..8 sides
        height = 1 + seed[1] / 255
        pointed = seed[2] % 2 == 0
        lines = [f"# mock 3D model for: {prompt[:80]!r}", "o model"]
        for i in range(sides):
            a = 2 * math.pi * i / sides
            lines.append(f"v {math.cos(a):.4f} 0 {math.sin(a):.4f}")
        if pointed:
            lines.append(f"v 0 {height:.4f} 0")
        else:
            for i in range(sides):
                a = 2 * math.pi * i / sides
                lines.append(f"v {0.6 * math.cos(a):.4f} {height:.4f} {0.6 * math.sin(a):.4f}")
        lines.append("f " + " ".join(str(i + 1) for i in reversed(range(sides))))
        for i in range(sides):
            j = (i + 1) % sides
            if pointed:
                lines.append(f"f {i + 1} {j + 1} {sides + 1}")
            else:
                lines.append(f"f {i + 1} {j + 1} {sides + j + 1} {sides + i + 1}")
        if not pointed:
            lines.append("f " + " ".join(str(sides + i + 1) for i in range(sides)))
        shape = "pyramid" if pointed else "prism"
        text = "\n".join(lines) + "\n"
        return {
            "model.obj": text.encode()
        }, f"Generated model.obj ({sides}-sided {shape}, mock 3D job)"
