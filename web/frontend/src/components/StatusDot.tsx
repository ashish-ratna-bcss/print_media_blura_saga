import type { ReactNode } from "react";
import styles from "./StatusDot.module.css";

export function StatusDot({
  state,
  children,
}: {
  state: "ok" | "bad" | "checking";
  children: ReactNode;
}) {
  return (
    <span className={styles.wrap}>
      <span className={`${styles.dot} ${styles[state]}`} />
      <span>{children}</span>
    </span>
  );
}
