import { motion } from "framer-motion";
import type { ReactNode } from "react";
import styles from "./EmptyState.module.css";

export function EmptyState({
  icon,
  title,
  sub,
}: {
  icon: ReactNode;
  title: string;
  sub?: string;
}) {
  return (
    <motion.div
      className={styles.wrap}
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.25 }}
    >
      <div className={styles.icon}>{icon}</div>
      <div className={styles.title}>{title}</div>
      {sub && <div className={styles.sub}>{sub}</div>}
    </motion.div>
  );
}
