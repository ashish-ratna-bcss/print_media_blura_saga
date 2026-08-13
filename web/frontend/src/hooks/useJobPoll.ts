import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { JobDetail } from "../types";

/**
 * Polls /api/jobs/{id} while the job is queued or running, and stops as soon
 * as it lands on done/error. A ref-based generation guard makes it safe to
 * call watch() again with a new id while an old poll loop is still in flight.
 */
export function useJobPoll(intervalMs = 1500) {
  const [detail, setDetail] = useState<JobDetail | null>(null);
  const [active, setActive] = useState(false);
  const generation = useRef(0);

  useEffect(
    () => () => {
      generation.current += 1;
    },
    [],
  );

  function watch(jobId: string) {
    const gen = ++generation.current;
    setDetail(null);
    setActive(true);

    async function tick() {
      if (generation.current !== gen) return;
      try {
        const d = await api.job(jobId);
        if (generation.current !== gen) return;
        setDetail(d);
        if (d.job.status === "done" || d.job.status === "error") {
          setActive(false);
          return;
        }
      } catch {
        // transient network hiccup: keep polling
      }
      if (generation.current === gen) setTimeout(tick, intervalMs);
    }
    tick();
  }

  function reset() {
    generation.current += 1;
    setActive(false);
    setDetail(null);
  }

  return { detail, active, watch, reset };
}
