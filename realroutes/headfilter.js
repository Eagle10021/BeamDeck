// Head tracking maths: from the face model's pose to the numbers sent to the game. No page, camera or network in here, so
// tools/test_headfilter.js can check it. Used by head.js; also loadable by node.
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory(); else root.HeadFilter = factory();
})(typeof self !== "undefined" ? self : this, function () {
  const R2D = 180 / Math.PI;

  // ---------------------------------------------------------------- the One Euro filter
  // Steady when the head is still (jitter gets a low cut-off) and quick when it moves (the cut-off rises with the speed), which is
  // what makes head tracking feel both calm and responsive. 1 Euro Filter, Casiez, Roussel and Vogel, 2012.
  class OneEuro {
    constructor(minCutoff, beta, dCutoff = 1.0) { this.minCutoff = minCutoff; this.beta = beta; this.dCutoff = dCutoff; this.reset(); }
    reset() { this.x = null; this.dx = 0; this.t = null; }
    static alpha(cutoff, dt) { const tau = 1 / (2 * Math.PI * cutoff); return 1 / (1 + tau / dt); }
    filter(v, t) {
      if (this.x === null || t <= this.t) { this.x = v; this.dx = 0; this.t = t; return v; }
      const dt = Math.min(t - this.t, 0.25);
      const dv = (v - this.x) / dt;
      this.dx += OneEuro.alpha(this.dCutoff, dt) * (dv - this.dx);
      const cutoff = this.minCutoff + this.beta * Math.abs(this.dx);
      this.x += OneEuro.alpha(cutoff, dt) * (v - this.x);
      this.t = t;
      return this.x;
    }
  }

  // ---------------------------------------------------------------- the head pose from the face model
  // MediaPipe's facial transformation matrix (4x4, column-major) maps the canonical face to the camera: x to the right of the picture,
  // y up, z toward the viewer, translation in centimetres. The camera sees the user mirrored, so the user's right is the picture's
  // left. Returns what the user did: yaw (+ is the head turned to their right), pitch (+ is looking up), roll (+ is tilting to their
  // right), and x (+ right), y (+ up) and z (+ toward the screen) in centimetres. Angles in degrees, from R = Ry(a) Rx(b) Rz(c).
  function poseFromMatrix(m) {
    const r02 = m[8], r12 = m[9], r22 = m[10], r10 = m[1], r11 = m[5];
    const s = Math.hypot(r02, r12, r22) || 1;                                       // the matrix may carry the face's size
    return {
      yaw: -Math.atan2(r02, r22) * R2D,
      pitch: Math.asin(Math.max(-1, Math.min(1, r12 / s))) * R2D,
      roll: Math.atan2(r10, r11) * R2D,
      x: -m[12], y: m[13], z: m[14],
    };
  }

  // ---------------------------------------------------------------- the settings (sensitivity)
  const AXES = ["yaw", "pitch", "roll", "x", "y", "z"];
  const ROT = new Set(["yaw", "pitch", "roll"]);
  const DEFAULTS = {
    yaw:   {gain: 2.0, invert: false},         // the view turns this many degrees for each degree the head turns
    pitch: {gain: 1.5, invert: false},
    roll:  {gain: 0.5, invert: false},
    x:     {gain: 1.0, invert: false},         // sideways lean: 1 means a centimetre of head is a centimetre of view
    y:     {gain: 1.0, invert: false},
    z:     {gain: 1.0, invert: false},
    curve: 0.6,                                // 0 is a straight line; higher is gentler near the centre and quicker toward the edges
    deadzone: 1.0,                             // degrees (and a third of that in centimetres) around the centre that do nothing
    smoothness: 0.5,                           // 0 is the lowest delay, 1 the steadiest
    quick: 0.5,                                // how much fast movements are let through unsmoothed
    maxYaw: 120, maxPitch: 60, maxRoll: 45, maxLean: 30,
  };
  const REF = {yaw: 30, pitch: 30, roll: 30, x: 10, y: 10, z: 10};                       // where "far from the centre" starts, head degrees or cm
  const LIMIT = s => ({yaw: s.maxYaw, pitch: s.maxPitch, roll: s.maxRoll, x: s.maxLean, y: s.maxLean, z: s.maxLean});

  function mergeSettings(saved) {
    const s = JSON.parse(JSON.stringify(DEFAULTS));
    for (const k of Object.keys(saved || {})) {
      if (AXES.includes(k) && saved[k] && typeof saved[k] === "object") {
        if (Number.isFinite(saved[k].gain)) s[k].gain = Math.max(0, Math.min(8, saved[k].gain));
        s[k].invert = !!saved[k].invert;
      } else if (k in s && Number.isFinite(saved[k])) s[k] = saved[k];
    }
    return s;
  }

  // one channel's response: dead zone (continuous at its edge), then gain with the curve, then the limit
  function respond(axis, v, s) {
    const dz = ROT.has(axis) ? s.deadzone : s.deadzone / 3;
    const a = Math.max(0, Math.abs(v) - dz) * Math.sign(v);
    const out = s[axis].gain * a * (1 + s.curve * Math.abs(a) / REF[axis]);
    const lim = LIMIT(s)[axis];
    return Math.max(-lim, Math.min(lim, out)) * (s[axis].invert ? -1 : 1);
  }

  const cutoffOf = smoothness => 4.0 * Math.pow(0.1, smoothness);                         // 4 Hz down to 0.4 Hz
  const betaOf = quick => quick * 0.05;

  class Filter {
    constructor(settings) {
      this.s = mergeSettings(settings);
      this.f = {};
      for (const a of AXES) this.f[a] = new OneEuro(cutoffOf(this.s.smoothness), betaOf(this.s.quick));
      this.center = null;
      this.recent = [];                         // the last half second of raw poses, to centre on
    }
    setSettings(settings) {
      this.s = mergeSettings(settings);
      for (const a of AXES) { this.f[a].minCutoff = cutoffOf(this.s.smoothness); this.f[a].beta = betaOf(this.s.quick); }
    }
    reset() { for (const a of AXES) this.f[a].reset(); this.center = null; this.recent = []; }
    // call when the face goes away: the filters start afresh when it comes back, the centre stays
    lost() { for (const a of AXES) this.f[a].reset(); this.recent = []; }
    recenter(raw) {
      const pool = this.recent.length ? this.recent : (raw ? [raw] : []);
      if (!pool.length) return false;
      this.center = {};
      for (const a of AXES) this.center[a] = pool.reduce((t, p) => t + p[a], 0) / pool.length;
      for (const a of AXES) this.f[a].reset();
      return true;
    }
    // raw: poseFromMatrix(...) at time t (seconds). Returns {out, rel}: what to send, and the head's offset from the centre.
    update(raw, t) {
      this.recent.push({t, ...raw});
      while (this.recent.length && t - this.recent[0].t > 0.5) this.recent.shift();
      if (!this.center && this.recent.length >= 10) this.recenter();                      // the first steady half second is the centre
      if (!this.center) return null;
      const rel = {}, out = {};
      for (const a of AXES) {
        rel[a] = this.f[a].filter(raw[a] - this.center[a], t);
        out[a] = respond(a, rel[a], this.s);
      }
      return {out, rel};
    }
  }

  return {OneEuro, poseFromMatrix, Filter, respond, mergeSettings, DEFAULTS, AXES, cutoffOf, betaOf};
});
