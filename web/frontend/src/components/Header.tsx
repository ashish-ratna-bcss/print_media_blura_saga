import { Newspaper } from "lucide-react";
import { StatusDot } from "./StatusDot";
import type { HealthStatus } from "../types";
import styles from "./Header.module.css";

export function Header({
  health,
  checking,
  unreachable,
}: {
  health: HealthStatus | null;
  checking: boolean;
  unreachable: boolean;
}) {
  const state: "ok" | "bad" | "checking" = checking
    ? "checking"
    : unreachable || !health?.ready
      ? "bad"
      : "ok";

  const label = checking
    ? "checking…"
    : unreachable
      ? "backend unreachable"
      : health?.ready
        ? `${health.vlm ?? "model"} on ${health.gpu || "GPU"}`
        : health?.detail || "backend not ready";

  return (
    <header className={styles.header}>
      <div className={styles.brand}>
        <div className={styles.mark}>
          <Newspaper size={18} strokeWidth={2.25} />
        </div>
        <div className={styles.titles}>
          <h1>E-Paper</h1>
          <div className={styles.sub}>Layout · OCR · article extraction</div>
        </div>
      </div>
      <StatusDot state={state}>{label}</StatusDot>
    </header>
  );
}
