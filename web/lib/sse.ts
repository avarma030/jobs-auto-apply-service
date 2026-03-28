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
  message?: string;  // individual pipeline step message
}

export function useRunStream(runId: string | null) {
  const [latest, setLatest] = useState<RunEvent | null>(null);
  const [messages, setMessages] = useState<string[]>([]);
  const [done, setDone] = useState(false);
  const esRef = useRef<EventSource | null>(null);

  useEffect(() => {
    if (!runId) return;
    // Reset state for new run
    setLatest(null);
    setMessages([]);
    setDone(false);

    const token = getToken();
    // EventSource doesn't support custom headers; pass token as query param
    const url = `${BASE}/runs/${runId}/stream${token ? `?token=${token}` : ""}`;
    const es = new EventSource(url);
    esRef.current = es;

    es.onmessage = (e) => {
      try {
        const data: RunEvent = JSON.parse(e.data);
        setLatest((prev) => ({
          status: data.status ?? prev?.status ?? "",
          jobs_found: data.jobs_found ?? prev?.jobs_found ?? 0,
          jobs_applied: data.jobs_applied ?? prev?.jobs_applied ?? 0,
          event: data.event,
          error_message: data.error_message,
        }));
        // Accumulate pipeline progress messages
        if (data.message) {
          setMessages((prev) => [...prev, data.message as string]);
        }
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

  return { latest, messages, done };
}
