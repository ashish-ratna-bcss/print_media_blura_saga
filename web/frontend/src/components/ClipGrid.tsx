import { motion } from "framer-motion";
import type { ReactNode } from "react";
import styles from "./ClipGrid.module.css";

const container = {
  hidden: {},
  show: { transition: { staggerChildren: 0.045 } },
};

export function ClipGrid({ children }: { children: ReactNode }) {
  return (
    <motion.div
      className={styles.grid}
      variants={container}
      initial="hidden"
      animate="show"
    >
      {children}
    </motion.div>
  );
}
