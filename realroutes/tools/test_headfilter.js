// Head tracking maths: node tools/test_headfilter.js
const H = require("../headfilter.js");
let bad = 0;
const ok = (c, m) => { console.log((c ? "PASS " : "FAIL ") + m); if (!c) bad++; };
const near = (a, b, tol = 1e-6) => Math.abs(a - b) <= tol;
const D2R = Math.PI / 180;

// ---- the pose from the face model's matrix (built the way the decomposition says: R = Ry(a) Rx(b) Rz(c), column-major)
function matrix(a, b, c, tx = 0, ty = 0, tz = -40, scale = 1) {
  const [ca, sa, cb, sb, cc, sc] = [Math.cos(a * D2R), Math.sin(a * D2R), Math.cos(b * D2R), Math.sin(b * D2R), Math.cos(c * D2R), Math.sin(c * D2R)];
  const R = [[ca * cc + sa * sb * sc, -ca * sc + sa * sb * cc, sa * cb],
             [cb * sc, cb * cc, -sb],
             [-sa * cc + ca * sb * sc, sa * sc + ca * sb * cc, ca * cb]];
  const m = new Array(16).fill(0);
  for (let r = 0; r < 3; r++) for (let k = 0; k < 3; k++) m[k * 4 + r] = R[r][k] * scale;
  m[12] = tx; m[13] = ty; m[14] = tz; m[15] = 1;
  return m;
}
let p = H.poseFromMatrix(matrix(0, 0, 0));
ok(near(p.yaw, 0) && near(p.pitch, 0) && near(p.roll, 0), "a face looking straight at the camera is 0, 0, 0");
p = H.poseFromMatrix(matrix(-30, 0, 0));
ok(near(p.yaw, 30), "a head turned to the user's right (the picture's left) is a positive yaw");
p = H.poseFromMatrix(matrix(0, -20, 0));
ok(near(p.pitch, 20), "looking up is a positive pitch");
p = H.poseFromMatrix(matrix(0, 0, 15));
ok(near(p.roll, 15), "tilting the head to the user's right is a positive roll");
p = H.poseFromMatrix(matrix(-25, -10, 8));
ok(near(p.yaw, 25, 1e-4) && near(p.pitch, 10, 1e-4) && near(p.roll, 8, 1e-4), "the three angles come out right together");
p = H.poseFromMatrix(matrix(-25, -10, 8, 0, 0, -40, 1.7));
ok(near(p.yaw, 25, 1e-4) && near(p.pitch, 10, 1e-4) && near(p.roll, 8, 1e-4), "and do not depend on the face's size in the matrix");
p = H.poseFromMatrix(matrix(0, 0, 0, 5, 2, -30));
ok(near(p.x, -5) && near(p.y, 2) && near(p.z, -30), "a head right of the picture's centre is left for the user; up is up; z is the distance");

// ---- the response: dead zone, gain, curve, limits, invert
const S = H.mergeSettings({});
const r = (axis, v, over = {}) => H.respond(axis, v, {...S, ...over});
ok(r("yaw", 0) === 0 && r("yaw", 0.9) === 0 && r("yaw", -0.9) === 0, "inside the dead zone nothing moves");
ok(near(r("yaw", 1.0), 0) && r("yaw", 1.1) > 0 && r("yaw", 1.1) < 0.5, "and it starts smoothly at its edge (no jump)");
let mono = true, prev = -1e9;
for (let v = -60; v <= 60; v += 0.25) { const o = r("yaw", v); if (o < prev - 1e-9) mono = false; prev = o; }
ok(mono, "the response only ever rises with the head turn");
ok(near(r("yaw", -20), -r("yaw", 20)), "turning left is the mirror of turning right");
const straight = {...S, curve: 0, deadzone: 0, yaw: {gain: 2, invert: false}};
ok(near(H.respond("yaw", 10, straight), 20), "with no curve the gain is exact: 10 degrees of head is 20 of view");
const curved = {...straight, curve: 1};
ok(H.respond("yaw", 5, curved) / 5 < H.respond("yaw", 30, curved) / 30, "a curve makes the edges quicker than the centre");
ok(near(H.respond("yaw", 1, curved) / 1, 2 * (1 + 1 / 30), 1e-9), "and the centre keeps the gain (gain 2 stays 2 at the very centre)");
ok(r("yaw", 80, {deadzone: 0}) === S.maxYaw && r("yaw", -80, {deadzone: 0}) === -S.maxYaw, "the limit stops the view at its maximum");
ok(r("pitch", 10, {pitch: {gain: 1.5, invert: true}}) < 0, "invert flips an axis");
ok(r("x", 2) > 0 && r("x", 2) <= S.maxLean, "a lean in centimetres is handled too");
ok(r("yaw", 10, {yaw: {gain: 3, invert: false}}) > r("yaw", 10, {yaw: {gain: 1, invert: false}}), "more gain, more view");

// ---- settings from storage: bad values never break anything
const m = H.mergeSettings({yaw: {gain: 99, invert: 1}, pitch: {gain: -4}, curve: "x", deadzone: NaN, bogus: 5, smoothness: 0.2});
ok(m.yaw.gain === 8 && m.yaw.invert === true && m.pitch.gain === 0 && m.curve === S.curve && m.deadzone === S.deadzone && !("bogus" in m) && m.smoothness === 0.2,
   "stored settings are clamped, ignored when nonsense, and unknown keys dropped");

// ---- the One Euro filter
function rng(seed) { let s = seed; return () => (s = (s * 1664525 + 1013904223) % 4294967296) / 4294967296 - 0.5; }
const rand = rng(7);
function run(minCutoff, beta, fn, n = 300) {
  const f = new H.OneEuro(minCutoff, beta), out = [], inp = [];
  for (let i = 0; i < n; i++) { const t = i / 30, v = fn(t, i); inp.push(v); out.push(f.filter(v, t)); }
  return {inp, out};
}
const variance = a => { const mu = a.reduce((x, y) => x + y, 0) / a.length; return a.reduce((x, y) => x + (y - mu) ** 2, 0) / a.length; };
let rest = run(H.cutoffOf(0.5), H.betaOf(0.5), () => rand() * 1.0);                         // still head, 1 degree of camera noise
ok(variance(rest.out.slice(60)) < variance(rest.inp.slice(60)) * 0.2, `when the head is still the jitter drops to ${(variance(rest.out.slice(60)) / variance(rest.inp.slice(60)) * 100).toFixed(0)}% of the noise`);
function stepTime(smoothness, quick) {
  const {out} = run(H.cutoffOf(smoothness), H.betaOf(quick), (t, i) => i < 30 ? 0 : 30, 90);
  const k = out.findIndex((v, i) => i >= 30 && v >= 27); return k < 0 ? 999 : (k - 30) * 1000 / 30;
}
const slow = stepTime(1.0, 0), mid = stepTime(0.5, 0.5), fast = stepTime(0.0, 1.0);
ok(fast < mid && mid < slow, `the settings do what they say: a sudden 30 degree turn reaches 90% in ${fast.toFixed(0)} ms (quick), ${mid.toFixed(0)} ms (default), ${slow.toFixed(0)} ms (steady)`);
ok(mid <= 300, `the default follows a quick turn within 300 ms (${mid.toFixed(0)} ms)`);
const ramp = run(H.cutoffOf(0.5), H.betaOf(0.5), t => 40 * t, 60);
ok(ramp.out[59] > ramp.inp[59] - 12, "a steady sweep is followed closely");
const fe = new H.OneEuro(1, 0); fe.filter(5, 0); fe.filter(5, 0.5);
ok(near(fe.filter(5, 1), 5, 1e-9), "a constant input comes out unchanged");
const fe2 = new H.OneEuro(1, 0); fe2.filter(0, 0);
ok(Number.isFinite(fe2.filter(10, 5)) && fe2.filter(10, 5.0) !== undefined, "a long gap between frames does not blow it up");

// ---- the whole chain
function feed(filter, pose, seconds, t0 = 0) {
  let res = null;
  for (let i = 0; i < seconds * 30; i++) res = filter.update(pose, t0 + i / 30);
  return res;
}
let f = new H.Filter({});
ok(f.update(H.poseFromMatrix(matrix(0, 0, 0)), 0) === null, "nothing is sent until the first steady half second has set the centre");
f = new H.Filter({});
let res = feed(f, H.poseFromMatrix(matrix(-10, 5, 0)), 1.5);
ok(res && Math.abs(res.out.yaw) < 0.5 && Math.abs(res.out.pitch) < 0.5, "the pose it settled on is the centre, so a head held still sends zero");
res = feed(f, H.poseFromMatrix(matrix(-40, 5, 0)), 1.5, 2);                                  // turned another 30 degrees to the right
const exp = H.respond("yaw", 30, f.s);
ok(res.out.yaw > 0 && near(res.out.yaw, exp, 0.5), `turning 30 degrees right sends ${res.out.yaw.toFixed(1)} (the curve and gain say ${exp.toFixed(1)})`);
ok(Math.abs(res.out.pitch) < 0.5 && Math.abs(res.out.roll) < 0.5, "and nothing leaks into the other axes");
f.recenter();
res = feed(f, H.poseFromMatrix(matrix(-40, 5, 0)), 1.0, 4);
ok(Math.abs(res.out.yaw) < 0.5, "recentring makes the current pose the new zero");
f.lost();
res = feed(f, H.poseFromMatrix(matrix(-40, 5, 0)), 0.5, 6);
ok(res && Math.abs(res.out.yaw) < 0.5, "a face that goes and comes back keeps its centre");
f.setSettings({yaw: {gain: 4, invert: true}});
res = feed(f, H.poseFromMatrix(matrix(-70, 5, 0)), 1.5, 8);                                  // another 30 right of the centre
ok(res.out.yaw < -20, `changing a setting takes effect at once (gain 4, inverted: ${res.out.yaw.toFixed(1)})`);

console.log(bad ? `\n${bad} FAILED` : "\nall head filter tests passed");
process.exit(bad ? 1 : 0);
