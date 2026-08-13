/** Pull a "3/8" style fraction out of a stage string like "reading text 3/8". */
export function parseProgress(stage: string | undefined | null): number | null {
  if (!stage) return null;
  const m = stage.match(/(\d+)\s*\/\s*(\d+)/);
  if (!m) return null;
  const done = Number(m[1]);
  const total = Number(m[2]);
  if (!total) return null;
  return Math.min(1, Math.max(0, done / total));
}

export function formatWhen(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const time = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  if (sameDay) return time;
  return (
    d.toLocaleDateString(undefined, { month: "short", day: "numeric" }) + ", " + time
  );
}

export function formatSeconds(s: number | null | undefined): string {
  const n = s || 0;
  if (n < 60) return `${n.toFixed(0)}s`;
  const m = Math.floor(n / 60);
  const rem = Math.round(n % 60);
  return `${m}m ${rem}s`;
}

export function pct(n: number | null | undefined): string {
  return `${Math.round((n || 0) * 100)}%`;
}

export function bytesToSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const kb = bytes / 1024;
  if (kb < 1024) return `${kb.toFixed(1)} KB`;
  return `${(kb / 1024).toFixed(1)} MB`;
}
