import { animate, motion, useMotionValue, useTransform } from "framer-motion";
import { useEffect } from "react";
import styles from "./StatRow.module.css";

export interface Stat {
  label: string;
  value?: number;
  suffix?: string;
  decimals?: number;
  /** Non-numeric display (e.g. "2400×3694") — skips the count-up animation. */
  raw?: string;
}

function CountUp({ value, suffix = "", decimals = 0 }: { value: number; suffix?: string; decimals?: number }) {
  const mv = useMotionValue(0);
  const rounded = useTransform(mv, (v) => v.toFixed(decimals) + suffix);

  useEffect(() => {
    const controls = animate(mv, value, { duration: 0.7, ease: [0.22, 0.9, 0.32, 1] as const });
    return controls.stop;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);

  return <motion.b>{rounded}</motion.b>;
}

export function StatRow({ stats }: { stats: Stat[] }) {
  return (
    <div className={styles.row}>
      {stats.map((s) => (
        <div className={styles.tile} key={s.label}>
          {s.raw != null ? (
            <b>{s.raw}</b>
          ) : (
            <CountUp value={s.value ?? 0} suffix={s.suffix} decimals={s.decimals} />
          )}
          <span>{s.label}</span>
        </div>
      ))}
    </div>
  );
}
