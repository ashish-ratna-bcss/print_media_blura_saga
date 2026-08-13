import { motion } from "framer-motion";
import type { ReactNode } from "react";
import styles from "./TabBar.module.css";

export interface TabDef {
  id: string;
  label: string;
  icon?: ReactNode;
  badge?: number;
}

export function TabBar({
  tabs,
  active,
  onChange,
}: {
  tabs: TabDef[];
  active: string;
  onChange: (id: string) => void;
}) {
  return (
    <div className={styles.tabs} role="tablist" aria-label="Sections">
      {tabs.map((t) => {
        const selected = t.id === active;
        return (
          <button
            key={t.id}
            role="tab"
            aria-selected={selected}
            className={styles.tab}
            onClick={() => onChange(t.id)}
          >
            {selected && (
              <motion.span
                layoutId="tab-pill"
                className={styles.pill}
                transition={{ type: "spring", stiffness: 500, damping: 36 }}
              />
            )}
            {t.icon}
            {t.label}
            {!!t.badge && <span className={styles.badge}>{t.badge}</span>}
          </button>
        );
      })}
    </div>
  );
}
