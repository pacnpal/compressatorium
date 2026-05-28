/* exported sseConnectNamed, verifyEventSource */
// SSE helpers — both auto-reconnecting (for the jobs stream) and
// single-shot (for per-file verify streams).
//
// Note: an earlier safeParse helper was inlined into each call site
// because Codacy's bundled JSHint kept flagging "safeParse is not
// defined" for the const-arrow declaration despite esversion:11. The
// duplication is trivial (3 lines per use) and removes the false
// positive permanently.

const RECONNECT_INITIAL_MS = 500;
const RECONNECT_MAX_MS = 30_000;

/**
 * Auto-reconnecting EventSource wrapper. Used for /api/jobs/events — the
 * connection MUST survive backend restarts. Native EventSource auto-retries
 * silently with no backoff, which floods. We force-close on error and
 * schedule a reconnect with exponential backoff (500ms → 30s, reset on open).
 *
 * @param {string} url
 * @param {string[]} eventNames - named event types to subscribe to
 * @param {(evt: {type: string, data: any}) => void} onEvent
 * @returns {{close: () => void}}
 */
export function sseConnectNamed(url, eventNames, onEvent) {
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
    };

    es.onerror = () => {
      if (closed) return;
      try {
        es?.close();
      } catch (_e) {
        // ignore
      }
      es = null;
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
