"use client";

import { useEffect, useRef, useState } from "react";
import { getToken } from "./auth";

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export interface RunEvent {
  status: string;
  jobs_found: number;
  jobs_applied: number;
  event?: string;
  error_message?: string;
}

export function useRunStream(runId: string | null) {
  const [latest, setLatest] = useState<RunEvent | null>(null);
  const [done, setDone] = useState(false);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!runId) return;
    const token = getToken();
    // EventSource doesn't support custom headers; pass token as query param
    const url = `${BASE}/runs/${runId}/stream${token ? `?token=${token}` : ""}`;
    const es = new EventSource(url);
    esRef.current = es;

    es.onmessage = (e) => {
      try {
        const data: RunEvent = JSON.parse(e.data);
        setLatest(data);
        if (data.event && ["done", "failed", "stopped"].includes(data.event)) {
          setDone(true);
          es.close();
        }
      } catch {}
    };

    es.onerror = () => {
      setDone(true);
      es.close();
    };

    return () => {
      es.close();
    };
  }, [runId]);

  return { latest, done };
}
