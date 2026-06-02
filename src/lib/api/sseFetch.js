// POST-body SSE: backend batch-verify endpoints accept a JSON body of paths
// and return an SSE stream in the response body. EventSource cannot do POST,
// so we use fetch + ReadableStream + TextDecoderStream and parse SSE frames
// manually (split on \n\n, with the tail preserved between reads).

/**
 * @param {string} url
 * @param {object} body
 * @param {{
 *   signal?: AbortSignal,
 *   onEvent: (evt: { type: string, data: any }) => void,
 *   fallbackMessage?: string,
 * }} opts
 */
export async function sseFetchPost(url, body, { signal, onEvent, fallbackMessage = 'SSE stream failed' }) {
  const res = await fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
    },
    body: JSON.stringify(body),
    signal,
  });

  if (!res.ok || !res.body) {
    let detail = fallbackMessage;
    try {
      const errBody = await res.json();
      if (errBody && typeof errBody.detail === 'string') detail = errBody.detail;
    } catch (_e) {
      // not JSON
    }
    const err = new Error(detail);
    err.status = res.status;
    throw err;
  }

  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = '';

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      // SSE servers (and Python/FastAPI in particular) commonly emit CRLF
      // line endings. Boundary detection only works on \n\n, so collapse
      // CRLF to LF before slicing, otherwise the stream buffers forever.
      buffer = (buffer + value).replace(/\r\n/g, '\n');
      // Slice off each complete `\n\n`-terminated event from the head of
      // the buffer. Lifted the assignment out of the loop test so
      // Biome / jshint don't flag an assignment-in-expression.
      for (;;) {
        const boundary = buffer.indexOf('\n\n');
        if (boundary === -1) break;
        const chunk = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const parsed = parseChunk(chunk);
        if (parsed) onEvent(parsed);
      }
    }
    if (buffer.trim()) {
      const parsed = parseChunk(buffer);
      if (parsed) onEvent(parsed);
    }
  } finally {
    try {
      reader.releaseLock();
    } catch (_e) {
      // ignore
    }
  }
}

function parseChunk(chunk) {
  let event = 'message';
  const dataLines = [];
  for (const line of chunk.split('\n')) {
    if (!line) continue;
    if (line.startsWith(':')) continue; // SSE comment / keepalive
    if (line.startsWith('event:')) {
      event = line.slice(6).trim();
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).trimStart());
    }
  }
  if (dataLines.length === 0) return null;
  const raw = dataLines.join('\n');
  try {
    const data = JSON.parse(raw);
    if (data == null || typeof data !== 'object') return null;
    return { type: event, data };
  } catch (_e) {
    return null;
  }
}
