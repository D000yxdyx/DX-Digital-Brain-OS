import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

type Overview = {
  task_count: number;
  event_count: number;
  audit_count: number;
  state_counts: Record<string, number>;
  outbox_counts: Record<string, number>;
};

type TaskRow = {
  id: string;
  title: string;
  status: string;
  version: number;
  created_at: string;
};

type TaskDetail = TaskRow & {
  description: string | null;
  history: Array<{
    version: number;
    previous_status: string | null;
    next_status: string;
  }>;
};

type Proposal = {
  id: string;
  task_id: string;
  revision: number;
  status: string;
  objective: string;
  rationale: string;
  scope: Record<string, unknown>;
  requested_capabilities: string[];
  plan: Array<{ title: string; capability: string; parameters: Record<string, unknown> }>;
  created_at: string;
};

type Review = {
  id: string;
  decision: string;
  risk_level: string;
  reason: string;
  created_at: string;
  proposal_status: string;
};

type Approval = {
  id: string;
  scope: Record<string, unknown>;
  status: string;
  expires_at: string;
  created_at: string;
};

type Report = {
  id: string;
  status: string;
  summary: string;
  artifacts: Record<string, unknown>;
  created_at: string;
  proposal_status: string;
};

type GovernanceData = {
  proposals: Proposal[];
  reviews: Review[];
  approvals: Approval[];
  reports: Report[];
};

type Dossier = {
  id: string;
  task_id: string;
  status: string;
  objective: string;
  plan_snapshot: Array<Record<string, unknown>>;
  result_summary: string | null;
  artifacts: Record<string, unknown>;
  evidence: Array<Record<string, unknown>>;
  created_at: string;
  updated_at: string;
  closed_at: string | null;
};

type Retrospective = {
  id: string;
  status: string;
  expected_outcome: string;
  actual_outcome: string;
  deviations: Array<Record<string, unknown>>;
  root_causes: Array<Record<string, unknown>>;
  keep_items: string[];
  change_items: string[];
  stop_items: string[];
  follow_up_items: string[];
  evidence_refs: Array<Record<string, unknown>>;
  created_at: string;
};

type Lesson = {
  id: string;
  status: string;
  memory_type: string;
  statement: string;
  applicability: Record<string, unknown>;
  evidence_refs: Array<Record<string, unknown>>;
  confidence: number;
  reviewed_at: string | null;
};

type ArchiveData = {
  dossier: Dossier | null;
  retrospectives: Retrospective[];
  lessons: Lesson[];
};

async function getJson<T>(url: string, apiKey: string): Promise<T> {
  const response = await fetch(url, { headers: { "X-DX-API-Key": apiKey } });
  const body = await response.text();
  let payload: unknown = body;
  try {
    payload = JSON.parse(body);
  } catch {
    // Keep the plain response body for diagnostics.
  }
  if (!response.ok) {
    const detail = typeof payload === "object" && payload !== null && "detail" in payload
      ? String(payload.detail)
      : body || response.statusText;
    throw new Error(`${response.status}: ${detail}`);
  }
  return payload as T;
}

async function getOptionalJson<T>(url: string, apiKey: string): Promise<T | null> {
  try {
    return await getJson<T>(url, apiKey);
  } catch (reason) {
    if (reason instanceof Error && reason.message.startsWith("404:")) return null;
    throw reason;
  }
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function statusClass(status: string): string {
  return `status status-${status.replace(/[^a-z0-9_-]/gi, "-")}`;
}

function App() {
  const [apiBase, setApiBase] = useState("http://127.0.0.1:8000");
  const [apiKey, setApiKey] = useState("");
  const [connected, setConnected] = useState(false);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [tasks, setTasks] = useState<TaskRow[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [governance, setGovernance] = useState<GovernanceData | null>(null);
  const [archive, setArchive] = useState<ArchiveData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const refresh = useCallback(async () => {
    if (!apiKey) return;
    setLoading(true);
    setError(null);
    try {
      const [nextOverview, nextTasks] = await Promise.all([
        getJson<Overview>(`${apiBase}/v1/control/overview`, apiKey),
        getJson<TaskRow[]>(`${apiBase}/v1/control/tasks?limit=50`, apiKey),
      ]);
      setOverview(nextOverview);
      setTasks(nextTasks);
      setSelectedId((current) => current ?? nextTasks[0]?.id ?? null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法连接 DX API");
    } finally {
      setLoading(false);
    }
  }, [apiBase, apiKey]);

  useEffect(() => {
    if (!connected) return;
    void refresh();
    const timer = window.setInterval(() => void refresh(), 15_000);
    return () => window.clearInterval(timer);
  }, [connected, refresh]);

  useEffect(() => {
    if (!selectedId || !apiKey) {
      setDetail(null);
      setGovernance(null);
      setArchive(null);
      return;
    }
    let active = true;
    setGovernance(null);
    setArchive(null);
    const loadSelectedTask = async () => {
      const [nextDetail, proposals] = await Promise.all([
        getJson<TaskDetail>(`${apiBase}/v1/tasks/${selectedId}`, apiKey),
        getJson<Proposal[]>(`${apiBase}/v1/tasks/${selectedId}/proposals`, apiKey),
      ]);
      let nextGovernance: GovernanceData = { proposals, reviews: [], approvals: [], reports: [] };
      const latest = proposals[0];
      if (latest) {
        const [reviews, approvals, reports] = await Promise.all([
          getJson<Review[]>(`${apiBase}/v1/proposals/${latest.id}/reviews`, apiKey),
          getJson<Approval[]>(`${apiBase}/v1/proposals/${latest.id}/approvals`, apiKey),
          getJson<Report[]>(`${apiBase}/v1/proposals/${latest.id}/execution-reports`, apiKey),
        ]);
        nextGovernance = { proposals, reviews, approvals, reports };
      }
      const dossier = await getOptionalJson<Dossier>(`${apiBase}/v1/tasks/${selectedId}/dossier`, apiKey);
      let retrospectives: Retrospective[] = [];
      let lessons: Lesson[] = [];
      if (dossier) {
        retrospectives = await getJson<Retrospective[]>(`${apiBase}/v1/dossiers/${dossier.id}/retrospectives`, apiKey);
        const latestRetrospective = retrospectives[0];
        if (latestRetrospective) {
          lessons = await getJson<Lesson[]>(`${apiBase}/v1/retrospectives/${latestRetrospective.id}/lessons`, apiKey);
        }
      }
      if (active) {
        setDetail(nextDetail);
        setGovernance(nextGovernance);
        setArchive({ dossier, retrospectives, lessons });
      }
    };
    void loadSelectedTask().catch((reason: unknown) => {
      if (active) setError(reason instanceof Error ? reason.message : "无法读取任务治理链路");
    });
    return () => {
      active = false;
    };
  }, [apiBase, apiKey, selectedId]);

  function connect(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setConnected(true);
  }

  const pending = overview?.outbox_counts.pending ?? 0;
  const published = overview?.outbox_counts.published ?? 0;

  return (
    <main className="shell">
      <header className="masthead">
        <div>
          <p className="eyebrow">DX DIGITAL BRAIN OS · CONTROL PLANE</p>
          <h1>运行总览</h1>
          <p className="subhead">只读控制台 · 状态、事件、Outbox 与审计可见性</p>
        </div>
        <div className="read-only-badge"><span className="dot" /> READ ONLY</div>
      </header>

      <section className="connection-panel panel">
        <form onSubmit={connect}>
          <label>
            API 地址
            <input value={apiBase} onChange={(event) => setApiBase(event.target.value.replace(/\/$/, ""))} />
          </label>
          <label>
            本地 API Key
            <input type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder="不会持久化" required />
          </label>
          <button type="submit">连接</button>
          {connected && <button type="button" className="secondary" onClick={() => void refresh()} disabled={loading}>{loading ? "刷新中…" : "刷新"}</button>}
        </form>
        <p className="security-note">密钥仅保留在当前浏览器标签页内；所有请求仍由 FastAPI 鉴权。</p>
      </section>

      {error && <div className="alert" role="alert">{error}</div>}

      <section className="metric-grid" aria-label="内核指标">
        <Metric label="任务" value={overview?.task_count ?? "—"} hint="当前任务聚合" />
        <Metric label="领域事件" value={overview?.event_count ?? "—"} hint="不可变事件记录" />
        <Metric label="审计记录" value={overview?.audit_count ?? "—"} hint="追加式审计" />
        <Metric label="Outbox" value={`${published} / ${pending}`} hint="已发布 / 待处理" />
      </section>

      <section className="workspace-grid">
        <div className="panel task-panel">
          <div className="panel-heading">
            <div><p className="eyebrow">TASK STREAM</p><h2>任务流</h2></div>
            <span className="muted">{tasks.length} 条</span>
          </div>
          {tasks.length === 0 ? <Empty text={connected ? "暂无任务" : "连接 API 后加载"} /> : (
            <div className="task-list">
              {tasks.map((task) => (
                <button key={task.id} className={`task-row ${selectedId === task.id ? "selected" : ""}`} onClick={() => setSelectedId(task.id)}>
                  <span className="task-main"><strong>{task.title}</strong><small>{task.id}</small></span>
                  <span className={statusClass(task.status)}>{task.status}</span>
                  <span className="task-date">{formatDate(task.created_at)}</span>
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="panel detail-panel">
          <div className="panel-heading"><div><p className="eyebrow">AUDITABLE TIMELINE</p><h2>任务详情</h2></div></div>
          {!detail ? <Empty text="选择一条任务查看状态轨迹" /> : (
            <>
              <div className="detail-title"><h3>{detail.title}</h3><span className={statusClass(detail.status)}>{detail.status}</span></div>
              {detail.description && <p className="description">{detail.description}</p>}
              <div className="timeline">
                {detail.history.map((entry) => <div className="timeline-item" key={entry.version}><span className="timeline-marker" /><div><strong>v{entry.version} · {entry.next_status}</strong><small>{entry.previous_status ? `${entry.previous_status} → ` : "初始状态 · "}{entry.next_status}</small></div></div>)}
              </div>
            </>
          )}
        </div>
      </section>

      <section className="governance-grid">
        <div className="panel governance-panel">
          <div className="panel-heading">
            <div><p className="eyebrow">GOVERNANCE CONTRACT</p><h2>计划 · 审核 · 审批 · 执行</h2></div>
            <span className="muted">只读</span>
          </div>
          {!governance ? <Empty text={selectedId ? "正在读取治理链路…" : "选择任务查看治理链路"} /> : governance.proposals.length === 0 ? <Empty text="该任务还没有计划提案" /> : (
            <div className="governance-body">
              <div className="proposal-picker">
                {governance.proposals.map((proposal) => (
                  <div className="proposal-chip" key={proposal.id}><span className={statusClass(proposal.status)}>{proposal.status}</span><strong>v{proposal.revision} · {proposal.objective}</strong><small>{formatDate(proposal.created_at)}</small></div>
                ))}
              </div>
              <div className="proposal-summary">
                <div className="detail-title"><h3>{governance.proposals[0].objective}</h3><span className={statusClass(governance.proposals[0].status)}>{governance.proposals[0].status}</span></div>
                <p className="description">{governance.proposals[0].rationale}</p>
                <div className="plan-list">{governance.proposals[0].plan.map((step, index) => <div className="plan-step" key={`${step.title}-${index}`}><span>{String(index + 1).padStart(2, "0")}</span><div><strong>{step.title}</strong><small>{step.capability}</small></div></div>)}</div>
              </div>
              <div className="governance-columns">
                <GovernanceColumn title="审核" eyebrow="REVIEW" empty="暂无审核" items={governance.reviews.map((item) => <div className="governance-item" key={item.id}><div><span className={statusClass(item.decision)}>{item.decision}</span><span className="risk">{item.risk_level}</span></div><strong>{item.reason}</strong><small>{formatDate(item.created_at)}</small></div>)} />
                <GovernanceColumn title="审批" eyebrow="APPROVAL" empty="暂无审批" items={governance.approvals.map((item) => <div className="governance-item" key={item.id}><div><span className={statusClass(item.status)}>{item.status}</span></div><strong>范围：{JSON.stringify(item.scope)}</strong><small>截止 {formatDate(item.expires_at)}</small></div>)} />
                <GovernanceColumn title="执行报告" eyebrow="EXECUTION" empty="暂无报告" items={governance.reports.map((item) => <div className="governance-item" key={item.id}><div><span className={statusClass(item.status)}>{item.status}</span></div><strong>{item.summary}</strong><small>{formatDate(item.created_at)} · {item.proposal_status}</small></div>)} />
              </div>
            </div>
          )}
        </div>
      </section>

      <section className="archive-grid">
        <div className="panel archive-panel">
          <div className="panel-heading">
            <div><p className="eyebrow">RECORDS &amp; LEARNING</p><h2>档案 · 复盘 · 经验</h2></div>
            <span className="muted">只读</span>
          </div>
          {!archive ? <Empty text={selectedId ? "正在读取项目档案…" : "选择任务查看项目档案"} /> : !archive.dossier ? <Empty text="该任务还没有封存档案" /> : (
            <div className="archive-body">
              <div className="archive-summary">
                <div className="detail-title"><h3>项目档案</h3><span className={statusClass(archive.dossier.status)}>{archive.dossier.status}</span></div>
                <p className="description">{archive.dossier.objective}</p>
                {archive.dossier.result_summary && <p className="archive-result">{archive.dossier.result_summary}</p>}
                <small className="archive-meta">{archive.dossier.closed_at ? `封存于 ${formatDate(archive.dossier.closed_at)}` : `创建于 ${formatDate(archive.dossier.created_at)}`} · 证据 {archive.dossier.evidence.length} 项</small>
              </div>
              <div className="archive-columns">
                <div className="archive-column"><div className="column-heading"><div><p className="eyebrow">RETROSPECTIVE</p><h3>复盘</h3></div><span className="muted">{archive.retrospectives.length}</span></div>{archive.retrospectives.length === 0 ? <div className="column-empty">暂无复盘</div> : archive.retrospectives.map((item) => <div className="archive-item" key={item.id}><div><span className={statusClass(item.status)}>{item.status}</span></div><strong>{item.actual_outcome}</strong><small>预期：{item.expected_outcome}</small><small>后续行动：{item.follow_up_items.join("；") || "无"}</small></div>)}</div>
                <div className="archive-column"><div className="column-heading"><div><p className="eyebrow">VALIDATED LESSONS</p><h3>经验</h3></div><span className="muted">{archive.lessons.length}</span></div>{archive.lessons.length === 0 ? <div className="column-empty">暂无经验</div> : archive.lessons.map((item) => <div className="archive-item" key={item.id}><div><span className={statusClass(item.status)}>{item.status}</span><span className="risk">{item.memory_type} · {(item.confidence * 100).toFixed(0)}%</span></div><strong>{item.statement}</strong><small>{item.reviewed_at ? `复核于 ${formatDate(item.reviewed_at)}` : "待人工复核"}</small></div>)}</div>
              </div>
            </div>
          )}
        </div>
      </section>

      <footer>Kernel Tracer v0.1 · GET-only Control UI · 自动刷新 15 秒</footer>
    </main>
  );
}

function Metric({ label, value, hint }: { label: string; value: number | string; hint: string }) {
  return <div className="metric panel"><span className="muted">{label}</span><strong>{value}</strong><small>{hint}</small></div>;
}

function Empty({ text }: { text: string }) {
  return <div className="empty">{text}</div>;
}

function GovernanceColumn({ title, eyebrow, empty, items }: { title: string; eyebrow: string; empty: string; items: ReactNode[] }) {
  return <div className="governance-column"><div className="column-heading"><div><p className="eyebrow">{eyebrow}</p><h3>{title}</h3></div><span className="muted">{items.length}</span></div>{items.length === 0 ? <div className="column-empty">{empty}</div> : items}</div>;
}

createRoot(document.getElementById("root")!).render(<App />);
