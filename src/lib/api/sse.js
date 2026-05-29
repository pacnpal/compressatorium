/* exported sseConnectNamed, verifyEventSource */
// SSE helpers — both auto-reconnecting (for the jobs stream) and
// single-shot (for per-file verify streams).
//
// JSON parsing is inlined at each call site (rather than via a helper)
// because Codacy's bundled JSHint repeatedly flagged a const-arrow
// `safeParse` helper as undefined despite esversion:11. The 3-line
// inline pattern is trivial and removes the false positive.

const RECONNECT_INITIAL_MS = 500;
const RECONNECT_MAX_MS = 30_000;

/**
 * Auto-reconnecting EventSource wrapper. Used for /api/jobs/events — the
 * connection MUST survive backend restarts. Native EventSource auto-retries
 * silently with no backoff, which floods. We force-close on error and
 * schedule a reconnect with exponential backoff (500ms → 30s, reset on open).
 *
 * Status callbacks let callers wire connection state into UI feedback
 * (e.g. "Lost connection — retrying…" toast):
 *   - onOpen():         called every time the EventSource opens
 *   - onReconnecting(): called when a reconnect is being scheduled
 *
 * @param {string} url
 * @param {string[]} eventNames - named event types to subscribe to
 * @param {(evt: {type: string, data: any}) => void} onEvent
 * @param {{onOpen?: () => void, onReconnecting?: () => void}} [statusHandlers]
 * @returns {{close: () => void}}
 */
export function sseConnectNamed(url, eventNames, onEvent, statusHandlers = {}) {
  let es = null;
  let closed = false;
  let backoffMs = RECONNECT_INITIAL_MS;
  let reconnectTimer = null;

  const dispatch = (type, ev) => {
    if (ev.data == null || ev.data === '') return;
    let data;
    try {
      data = JSON.parse(ev.data);
    } catch (_e) {
      return;
    }
    onEvent({ type, data });
  };

  const connect = () => {
    if (closed) return;
    es = new EventSource(url);

    for (const name of eventNames) {
      es.addEventListener(name, (ev) => dispatch(name, ev));
    }

    es.onopen = () => {
      backoffMs = RECONNECT_INITIAL_MS;
      try {
        statusHandlers.onOpen?.();
      } catch (_e) {
        // ignore — status handlers must not break the stream
      }
    };

    es.onerror = () => {
      if (closed) return;
      // The backend emits failed jobs as a server-sent `event: error`
      // (e.g. job_manager._notify_subscribers on conversion failure),
      // which fires this onerror handler too because `error` is also
      // the spec'd transport-error event name — they share one
      // dispatch slot. Without disambiguation, every failed job would
      // tear down the connection and trigger the reconnect/backoff
      // cycle, briefly missing live updates for other active jobs
      // and surfacing a false "Lost connection" toast.
      //
      // Distinguish via `readyState`: server-sent named events fire
      // while readyState === OPEN (1); real transport errors put the
      // connection into CONNECTING (0) or CLOSED (2). Bail out for
      // OPEN — the matching addEventListener('error', …) on the line
      // above has already routed the payload to the app handler.
      if (es && es.readyState === EventSource.OPEN) return;
      try {
        es?.close();
      } catch (_e) {
        // ignore
      }
      es = null;
      try {
        statusHandlers.onReconnecting?.();
      } catch (_e) {
        // ignore
      }
      reconnectTimer = setTimeout(() => {
        backoffMs = Math.min(backoffMs * 2, RECONNECT_MAX_MS);
        connect();
      }, backoffMs);
    };
  };

  connect();

  return {
    close() {
      closed = true;
      if (reconnectTimer) {
        clearTimeout(reconnectTimer);
        reconnectTimer = null;
      }
      try {
        es?.close();
      } catch (_e) {
        // ignore
      }
      es = null;
    },
  };
}

/**
 * Single-shot verify SSE — opens an EventSource, reports progress via callback,
 * resolves on verify_complete, rejects on verify_error or connection error.
 * Auto-closes on resolve/reject (does NOT auto-reconnect — this is a one-shot
 * operation; the user retries by re-clicking).
 *
 * @param {string} url - already-built URL with ?path=...
 * @param {(data: any) => void} [onProgress]
 * @param {{ failureFallback?: string }} [opts]
 * @returns {Promise<any>}
 */
export function verifyEventSource(url, onProgress, { failureFallback = 'Verification failed' } = {}) {
  return new Promise((resolve, reject) => {
    const es = new EventSource(url);
    let settled = false;
    const cleanup = () => {
      if (settled) return;
      settled = true;
      try {
        es.close();
      } catch (_e) {
        // ignore
      }
    };

    es.addEventListener('verify_progress', (ev) => {
      if (!onProgress || ev.data == null || ev.data === '') return;
      try {
        onProgress(JSON.parse(ev.data));
      } catch (_e) {
        // ignore malformed progress frames
      }
    });

    es.addEventListener('verify_complete', (ev) => {
      let data = {};
      if (ev.data) {
        try {
          data = JSON.parse(ev.data) ?? {};
        } catch (_e) {
          data = {};
        }
      }
      cleanup();
      resolve(data);
    });

    es.addEventListener('verify_error', (ev) => {
      let message = failureFallback;
      if (ev.data) {
        try {
          const data = JSON.parse(ev.data);
          if (data && typeof data === 'object' && typeof data.message === 'string') {
            message = data.message;
          }
        } catch (_e) {
          // keep fallback
        }
      }
      cleanup();
      reject(new Error(message));
    });

    es.onerror = () => {
      if (settled) return;
      cleanup();
      reject(new Error('Verification connection error'));
    };
  });
}
