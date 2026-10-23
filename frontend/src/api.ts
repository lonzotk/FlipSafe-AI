export type Run = {
  run_id: string;
  status: string;
  kind?: string;
  optimized?: boolean;
  events?: Array<Record<string, any>>;
  usage?: Array<Record<string, any>>;
  result?: any;
  security?: { finding_codes?: string[]; allowed?: boolean };
  reason?: string;
  missing_evidence?: string[];
};

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  const value = await response.json();
  if (!response.ok) throw value;
  return value as T;
}

export async function jsonPost<T = any>(path: string, body: unknown): Promise<T> {
  return api<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
}

export async function waitForRun(runId: string, onUpdate: (run: Run) => void): Promise<Run> {
  let last = await api<Run>(`/api/runs/${runId}`);
  onUpdate(last);
  const terminal = (status: string) => ['complete', 'stopped', 'error', 'blocked', 'cancelled'].includes(status);
  if (terminal(last.status)) return last;
  return new Promise((resolve) => {
    let closed = false;
    let source: EventSource | undefined;
    const poll = window.setInterval(() => { api<Run>(`/api/runs/${runId}`).then(finish).catch(() => undefined); }, 900);
    const finish = (run: Run) => {
      if (closed) return;
      last = run; onUpdate(run);
      if (terminal(run.status)) { closed = true; source?.close(); window.clearInterval(poll); resolve(run); }
    };
    source = new EventSource(`/api/runs/${runId}/events`);
    source.onmessage = () => { api<Run>(`/api/runs/${runId}`).then(finish).catch(() => undefined); };
    source.addEventListener('done', () => { api<Run>(`/api/runs/${runId}`).then(finish).catch(() => undefined); });
  });
}
