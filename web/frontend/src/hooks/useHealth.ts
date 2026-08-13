import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { HealthStatus } from "../types";

export function useHealth(intervalMs = 30000) {
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [checking, setChecking] = useState(true);
  const [unreachable, setUnreachable] = useState(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    async function tick() {
      try {
        const h = await api.health();
        if (!mounted.current) return;
        setHealth(h);
        setUnreachable(false);
      } catch {
        if (!mounted.current) return;
        setUnreachable(true);
      } finally {
        if (mounted.current) setChecking(false);
      }
    }
    tick();
    const id = setInterval(tick, intervalMs);
    return () => {
      mounted.current = false;
      clearInterval(id);
    };
  }, [intervalMs]);

  return { health, checking, unreachable };
}
