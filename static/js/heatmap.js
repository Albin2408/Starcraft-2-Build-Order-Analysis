/*
 * Spatio-temporal heatmaps on a plain <canvas> (no dependencies).
 *
 * The server hands over coarse count grids. This turns them into a smooth
 * density picture: each non-empty cell is splatted onto a finer field as a
 * Gaussian, the field is normalised and colour-mapped, and the result is
 * scaled up to the canvas with bilinear filtering. Several layers (your army,
 * theirs, where units died) are blended additively on a dark map.
 *
 * Coordinates: every grid here has y pointing UP (as SC2 does), so the canvas
 * drawing flips y to match what you see on the in-game minimap.
 */
(function (global) {
  'use strict';

  const FIELD = 128;                       // internal resolution of the density field

  // colour ramps: [stop, r, g, b]
  const RAMPS = {
    mine:   [[0, 8, 30, 70], [0.35, 30, 110, 220], [0.7, 90, 200, 255], [1, 230, 250, 255]],
    opp:    [[0, 70, 15, 10], [0.35, 200, 60, 30], [0.7, 255, 140, 60], [1, 255, 235, 190]],
    heat:   [[0, 20, 10, 60], [0.3, 110, 30, 130], [0.6, 235, 90, 60], [0.85, 252, 190, 50], [1, 255, 250, 200]],
    deaths: [[0, 90, 10, 20], [0.5, 255, 80, 80], [1, 255, 245, 235]],
  };

  function ramp(name, v) {
    const stops = RAMPS[name];
    if (v <= stops[0][0]) return stops[0].slice(1);
    for (let i = 1; i < stops.length; i++) {
      if (v <= stops[i][0]) {
        const a = stops[i - 1], b = stops[i];
        const t = (v - a[0]) / (b[0] - a[0]);
        return [a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t, a[3] + (b[3] - a[3]) * t];
      }
    }
    return stops[stops.length - 1].slice(1);
  }

  // Splat sparse cells onto a FIELD x FIELD float field with a Gaussian kernel.
  // `cells` is [{x, y, v}] with x, y in 0..1 (y up).
  function splat(cells, sigma) {
    const field = new Float32Array(FIELD * FIELD);
    const r = Math.ceil(sigma * 3);
    const kernel = [];
    for (let dy = -r; dy <= r; dy++) {
      for (let dx = -r; dx <= r; dx++) {
        kernel.push([dx, dy, Math.exp(-(dx * dx + dy * dy) / (2 * sigma * sigma))]);
      }
    }
    for (const c of cells) {
      const cx = Math.min(FIELD - 1, Math.max(0, Math.round(c.x * FIELD - 0.5)));
      const cy = Math.min(FIELD - 1, Math.max(0, Math.round(c.y * FIELD - 0.5)));
      for (const [dx, dy, w] of kernel) {
        const x = cx + dx, y = cy + dy;
        if (x >= 0 && x < FIELD && y >= 0 && y < FIELD) field[y * FIELD + x] += c.v * w;
      }
    }
    return field;
  }

  // field -> RGBA offscreen canvas. `scale` fixes the value that maps to 1.0 so
  // frames of an animation stay comparable instead of each re-normalising.
  function colorize(field, rampName, scale, gamma) {
    const off = document.createElement('canvas');
    off.width = off.height = FIELD;
    const ctx = off.getContext('2d');
    const img = ctx.createImageData(FIELD, FIELD);
    for (let y = 0; y < FIELD; y++) {
      for (let x = 0; x < FIELD; x++) {
        const v = Math.min(1, field[y * FIELD + x] / scale);
        const o = ((FIELD - 1 - y) * FIELD + x) * 4;         // flip y
        if (v <= 0.004) { img.data[o + 3] = 0; continue; }
        const t = Math.pow(v, gamma);
        const [r, g, b] = ramp(rampName, t);
        img.data[o] = r; img.data[o + 1] = g; img.data[o + 2] = b;
        img.data[o + 3] = Math.min(255, 40 + 215 * t);
      }
    }
    ctx.putImageData(img, 0, 0);
    return off;
  }

  function maxOf(field) {
    let m = 0;
    for (let i = 0; i < field.length; i++) if (field[i] > m) m = field[i];
    return m;
  }

  function drawBackdrop(ctx, w, h) {
    ctx.fillStyle = '#0a0e18';
    ctx.fillRect(0, 0, w, h);
    ctx.strokeStyle = 'rgba(147,160,194,0.10)';
    ctx.lineWidth = 1;
    for (let i = 1; i < 8; i++) {
      ctx.beginPath(); ctx.moveTo((w * i) / 8, 0); ctx.lineTo((w * i) / 8, h); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(0, (h * i) / 8); ctx.lineTo(w, (h * i) / 8); ctx.stroke();
    }
  }

  function drawMarker(ctx, w, h, pos, label, color) {
    if (!pos) return;
    const x = pos[0] * w, y = (1 - pos[1]) * h;
    ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(x, y, 7, 0, Math.PI * 2); ctx.stroke();
    ctx.font = '11px Segoe UI, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(label, x, y - 12);
  }

  /*
   * layers: [{ cells: [{x,y,v}], ramp: 'mine'|'opp'|'heat'|'deaths',
   *            sigma, scale (optional fixed max), gamma (default .6), alpha }]
   * options: { markers: [{pos, label, color}], blend: 'lighter' | 'source-over' }
   */
  function render(canvas, layers, options) {
    options = options || {};
    const w = canvas.width, h = canvas.height;
    const ctx = canvas.getContext('2d');
    drawBackdrop(ctx, w, h);
    ctx.imageSmoothingEnabled = true;
    ctx.imageSmoothingQuality = 'high';
    ctx.globalCompositeOperation = options.blend || 'lighter';
    for (const layer of layers) {
      if (!layer.cells.length) continue;
      const field = splat(layer.cells, layer.sigma || 3);
      const scale = layer.scale || Math.max(maxOf(field), 1e-6);
      const img = colorize(field, layer.ramp, scale, layer.gamma || 0.6);
      ctx.globalAlpha = layer.alpha === undefined ? 1 : layer.alpha;
      ctx.drawImage(img, 0, 0, w, h);
    }
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = 'source-over';
    for (const m of options.markers || []) drawMarker(ctx, w, h, m.pos, m.label, m.color);
    ctx.strokeStyle = 'rgba(42,51,80,1)';
    ctx.strokeRect(0.5, 0.5, w - 1, h - 1);
  }

  // ---- helpers for turning server data into cells ------------------------------------

  // aggregated API layer (per-minute flat [cell, value, ...]) over minutes [m0, m1)
  function composeMinutes(perMinute, grid, m0, m1) {
    const acc = new Map();
    const span = Math.max(1, m1 - m0);
    for (let m = m0; m < Math.min(m1, perMinute.length); m++) {
      const flat = perMinute[m];
      for (let i = 0; i < flat.length; i += 2) acc.set(flat[i], (acc.get(flat[i]) || 0) + flat[i + 1] / span);
    }
    const cells = [];
    acc.forEach((v, cell) => cells.push({ x: ((cell % grid) + 0.5) / grid, y: (Math.floor(cell / grid) + 0.5) / grid, v }));
    return cells;
  }

  // raw telemetry cell (64-grid in SC2 coordinates) -> 0..1 within the game's bounds
  function rawCellToUnit(cell, tel) {
    const g = tel.grid, size = tel.cell;
    const cx = (cell % g) * size + size / 2, cy = Math.floor(cell / g) * size + size / 2;
    const [x0, y0, x1, y1] = tel.bounds;
    return {
      x: Math.min(1, Math.max(0, (cx - x0) / (x1 - x0))),
      y: Math.min(1, Math.max(0, (cy - y0) / (y1 - y0))),
    };
  }

  function posToUnit(pos, tel) {
    if (!pos) return null;
    const [x0, y0, x1, y1] = tel.bounds;
    return [Math.min(1, Math.max(0, (pos[0] - x0) / (x1 - x0))), Math.min(1, Math.max(0, (pos[1] - y0) / (y1 - y0)))];
  }

  // Peak of the smoothed field for these cells - lets an animation pass a fixed
  // `scale` so frames stay comparable (a small army should look small).
  function fieldMax(cells, sigma) {
    return cells.length ? maxOf(splat(cells, sigma || 3)) : 0;
  }

  global.Heat = { render, composeMinutes, rawCellToUnit, posToUnit, fieldMax, FIELD };
})(window);
