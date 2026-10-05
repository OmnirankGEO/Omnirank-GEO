import { useRef, useEffect, useCallback } from 'react';

interface TrackEvent {
  type: string;
  keyword_id?: number;
  keyword_text?: string;
  phase?: string;
  data?: Record<string, unknown>;
  ts?: number;
}

export function useInteractionTracker(token: string, phase: 'selection' | 'pricing' | 'confirmed') {
  const buffer = useRef<TrackEvent[]>([]);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const flush = useCallback(() => {
    if (buffer.current.length === 0) return;
    const events = [...buffer.current];
    buffer.current = [];

    const blob = new Blob(
      [JSON.stringify({ events: events.map(e => ({ ...e, phase })) })],
      { type: 'application/json' }
    );

    // Try fetch first, fall back to sendBeacon
    fetch(`/api/s/${token}/events`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ events: events.map(e => ({ ...e, phase })) }),
    }).catch(() => {
      navigator.sendBeacon?.(`/api/s/${token}/events`, blob);
    });
  }, [token, phase]);

  const flushBeacon = useCallback(() => {
    if (buffer.current.length === 0) return;
    const events = [...buffer.current];
    buffer.current = [];
    const blob = new Blob(
      [JSON.stringify({ events: events.map(e => ({ ...e, phase })) })],
      { type: 'application/json' }
    );
    navigator.sendBeacon?.(`/api/s/${token}/events`, blob);
  }, [token, phase]);

  useEffect(() => {
    timerRef.current = setInterval(flush, 10000);

    const handleUnload = () => flushBeacon();
    const handleVisChange = () => {
      if (document.visibilityState === 'hidden') flushBeacon();
    };

    window.addEventListener('beforeunload', handleUnload);
    document.addEventListener('visibilitychange', handleVisChange);

    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
      flushBeacon();
      window.removeEventListener('beforeunload', handleUnload);
      document.removeEventListener('visibilitychange', handleVisChange);
    };
  }, [flush, flushBeacon]);

  const track = useCallback((event: TrackEvent) => {
    buffer.current.push({ ...event, ts: Date.now() });
  }, []);

  return { track, flush };
}
