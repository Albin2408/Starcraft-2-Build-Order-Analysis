/*
 * Follow a batch of queued replay-parsing jobs.
 *
 * The server tails a Redis Stream and pushes Server-Sent Events: a `file`
 * event as each replay finishes and a `state` event with the running counters.
 * If EventSource isn't available (or the connection drops) it falls back to
 * polling the same state over plain JSON.
 */
(function (global) {
  'use strict';

  function follow(batchId, handlers) {
    handlers = handlers || {};
    let finished = false;
    let source = null;
    let pollTimer = null;

    const done = (state) => {
      if (finished) return;
      finished = true;
      if (source) source.close();
      if (pollTimer) clearInterval(pollTimer);
      if (handlers.onDone) handlers.onDone(state);
    };

    const poll = () => {
      pollTimer = setInterval(async () => {
        try {
          const state = await (await fetch(`/api/batches/${batchId}`)).json();
          if (handlers.onState) handlers.onState(state);
          if (state.finished) done(state);
        } catch (e) { /* keep trying */ }
      }, 1000);
    };

    if (!('EventSource' in window)) { poll(); return { close: () => done(null) }; }

    source = new EventSource(`/api/batches/${batchId}/stream`);
    source.addEventListener('file', (e) => handlers.onFile && handlers.onFile(JSON.parse(e.data)));
    source.addEventListener('state', (e) => handlers.onState && handlers.onState(JSON.parse(e.data)));
    source.addEventListener('done', (e) => done(JSON.parse(e.data)));
    source.onerror = () => {
      // The server closes the stream when the batch ends; only fall back if it didn't.
      if (!finished) { source.close(); source = null; poll(); }
    };
    return { close: () => done(null) };
  }

  // Collect .SC2Replay files from a drop, descending into dropped folders.
  async function filesFromDrop(dataTransfer) {
    const out = [];
    const walk = async (entry) => {
      if (entry.isFile) {
        const file = await new Promise((res, rej) => entry.file(res, rej));
        out.push(file);
      } else if (entry.isDirectory) {
        const reader = entry.createReader();
        let batch;
        do {
          batch = await new Promise((res, rej) => reader.readEntries(res, rej));
          for (const child of batch) await walk(child);
        } while (batch.length);
      }
    };
    const items = Array.from(dataTransfer.items || []);
    if (items.length && items[0].webkitGetAsEntry) {
      const entries = items.map((i) => i.webkitGetAsEntry()).filter(Boolean);
      for (const entry of entries) await walk(entry);
    } else {
      out.push(...Array.from(dataTransfer.files));
    }
    return out.filter((f) => f.name.toLowerCase().endsWith('.sc2replay'));
  }

  // POST files with upload-progress callbacks (fetch can't report upload progress).
  function uploadFiles(url, files, onProgress) {
    return new Promise((resolve, reject) => {
      const form = new FormData();
      files.forEach((f) => form.append('replays', f, f.name));
      const xhr = new XMLHttpRequest();
      xhr.open('POST', url);
      xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
      xhr.onload = () => {
        let body = {};
        try { body = JSON.parse(xhr.responseText); } catch (e) { /* not JSON */ }
        if (xhr.status >= 200 && xhr.status < 300) resolve(body);
        else reject(new Error(body.error || `Upload failed (${xhr.status})`));
      };
      xhr.onerror = () => reject(new Error('Network error during upload'));
      xhr.send(form);
    });
  }

  global.Batch = { follow, filesFromDrop, uploadFiles };
})(window);
