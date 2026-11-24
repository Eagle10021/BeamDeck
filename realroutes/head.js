// Head tracking: a webcam turns the view in the game. The camera is read and the face found in this page (MediaPipe's face model, on
// the integrated graphics, nothing leaves the computer), headfilter.js turns the head pose into what the view should do, and each
// result is sent to the helper (api/head), which hands it to the game's mod. Settings are kept per device, in this browser.
(function () {
  "use strict";
  const $ = id => document.getElementById(id);
  const KEY = "rrHead";
  const HF = window.HeadFilter;

  // ---------------------------------------------------------------- what is kept in this browser
  const PRESETS = {
    gentle:   {yaw: {gain: 1.2}, pitch: {gain: 1.0}, roll: {gain: 0.3}, curve: 0.3, deadzone: 1.5, smoothness: 0.65, quick: 0.4},
    standard: {yaw: {gain: 2.0}, pitch: {gain: 1.5}, roll: {gain: 0.5}, curve: 0.6, deadzone: 1.0, smoothness: 0.5, quick: 0.5},
    wide:     {yaw: {gain: 3.0}, pitch: {gain: 2.0}, roll: {gain: 0.5}, curve: 0.8, deadzone: 1.0, smoothness: 0.45, quick: 0.6},
  };
  const DEFAULT_RESOURCES = {device: "", size: 640, fps: 30, proc: "GPU", eco: true, show: true};
  function load() {
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch {}
    return {res: {...DEFAULT_RESOURCES, ...(saved.res || {})}, sens: HF.mergeSettings(saved.sens)};
  }
  let st = load();
  let saveT = 0;
  const save = () => { clearTimeout(saveT); saveT = setTimeout(() => {
    try { localStorage.setItem(KEY, JSON.stringify(st)); } catch {}
    if (run && run.remote) hostCmd("settings");
  }, 300); };
  // ---------------------------------------------------------------- the computer's camera, from this screen
  // A camera picked under "This computer" is read by the tracker BeamDeck runs on the computer (headhost.html); this
  // page sends it start / stop / recenter / settings and shows its status and preview.
  const ON_PC = ["127.0.0.1", "localhost"].includes(location.hostname);
  const isHost = dev => String(dev || "").startsWith("pc:");
  const hostCmd = (action, extra) => fetch("api/headhost", {method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({action, settings: {sens: st.sens, res: st.res}, ...(extra || {})})}).then(r => r.json()).catch(() => ({ok: false}));

  const filter = new HF.Filter(st.sens);
  let run = null;                 // the running tracker: {stream, lm, stop}
  let lm = null, lmProc = null;   // the face model, once loaded
  let stats = {fps: 0, ms: 0, frames: 0, t0: 0, face: false, lastFace: 0, sent: 0, err: 0};
  let last = {raw: null, out: null, rel: null};
  let sweep = null;
  const say = (text, bad) => { const e = $("hStatus"); if (e) { e.textContent = text; e.classList.toggle("bad", !!bad); } };

  // ---------------------------------------------------------------- sending to the game
  let inFlight = false, pending = null;
  function send(out) {
    pending = out;
    if (inFlight) return;                                       // never queue: the newest pose is sent when the last request is done
    inFlight = true;
    const body = JSON.stringify({...pending, on: true}); pending = null;
    fetch("api/head", {method: "POST", headers: {"Content-Type": "application/json"}, body}).then(r => { if (!r.ok) stats.err++; else stats.sent++; })
      .catch(() => { stats.err++; }).finally(() => { inFlight = false; if (pending) send(pending); });
  }

  // ---------------------------------------------------------------- the camera and the face model
  async function listCameras(ask) {
    if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) { say("This browser can't use a camera here. Open the page over https (the tablet link) or on this computer.", true); return []; }
    let devs = await navigator.mediaDevices.enumerateDevices();
    if (ask && devs.some(d => d.kind === "videoinput" && !d.label)) {                      // names only show once the camera has been allowed
      try { (await navigator.mediaDevices.getUserMedia({video: true})).getTracks().forEach(t => t.stop()); devs = await navigator.mediaDevices.enumerateDevices(); }
      catch (e) { say("The camera was not allowed: " + (e.message || e.name), true); }
    }
    const cams = devs.filter(d => d.kind === "videoinput");
    const sel = $("hCam"), cur = st.res.device;
    const esc = t => String(t).replace(/[<&>"]/g, "");
    let host = [];
    if (!ON_PC) { try { host = (await (await fetch("api/headhost", {cache: "no-store"})).json()).cameras || []; } catch {} }
    const local = cams.map((d, i) => `<option value="${d.deviceId}">${esc(d.label || "Camera " + (i + 1))}</option>`).join("");
    sel.innerHTML = (host.length ? `<optgroup label="This device">${local || `<option value="" disabled>No camera here</option>`}</optgroup>` +
      `<optgroup label="This computer (BeamDeck)">${host.map(n => `<option value="pc:${esc(n)}">${esc(n.split(":")[0])} (on the computer)</option>`).join("")}</optgroup>`
      : local || `<option value="">No camera found</option>`);
    if ([...sel.options].some(o => o.value === cur && !o.disabled)) sel.value = cur; else st.res.device = sel.value;
    return cams;
  }

  async function ensureModel() {
    if (lm && lmProc === st.res.proc) return lm;
    if (lm) { try { lm.close(); } catch {} lm = null; }
    say("Loading the face model (the first time it is downloaded, about 25 MB)…");
    const base = new URL("head/asset/", location.href).href;
    const mod = await import(base + "vision_bundle.mjs");
    const files = await mod.FilesetResolver.forVisionTasks(base.replace(/\/$/, ""));
    const make = proc => mod.FaceLandmarker.createFromOptions(files, {
      baseOptions: {modelAssetPath: base + "face_landmarker.task", delegate: proc},
      runningMode: "VIDEO", numFaces: 1, outputFacialTransformationMatrixes: true, outputFaceBlendshapes: false,
      minFaceDetectionConfidence: 0.5, minFacePresenceConfidence: 0.5, minTrackingConfidence: 0.5,
    });
    try { lm = await make(st.res.proc); lmProc = st.res.proc; }
    catch (e) {
      if (st.res.proc === "GPU") { lm = await make("CPU"); lmProc = "CPU"; say("The graphics chip could not run the face model, so the processor is doing it."); }   // never fails to start
      else throw e;
    }
    return lm;
  }

  async function startRemote() {
    const btn = $("hStart");
    const r = await hostCmd("start", {camera: st.res.device.slice(3)});
    if (!r.ok) { say(r.error || "BeamDeck on the computer does not answer.", true); return; }
    run = {remote: true, stop: false, still: 0};
    stats = {fps: 0, ms: 0, frames: 0, t0: performance.now(), face: false, lastFace: 0, sent: 0, err: 0};
    btn.textContent = "Stop tracking"; document.body.classList.add("headon"); paintHud();
    say(r.online ? "Starting the computer's camera…" : "Opening the tracker on the computer (a small window there)…");
    let img = $("hRemote");
    if (!img) { img = document.createElement("img"); img.id = "hRemote"; img.alt = "The computer's camera"; $("hPrev").appendChild(img); }
    $("hPrev").classList.add("remote");
    run.poll = setInterval(async () => {
      if (!run || !run.remote) return;
      let v;
      try { v = await (await fetch("api/headhost", {cache: "no-store"})).json(); } catch { return; }
      const s = v.status;
      if (!s) { if (!v.starting) say("The tracker on the computer is not answering. Is BeamDeck running there?", true); return; }
      stats.fps = s.fps; stats.ms = s.ms; stats.face = s.face; stats.err = s.err;
      last.raw = s.raw; last.out = s.out; last.rel = s.rel;
      if (s.preview && st.res.show) img.src = s.preview;
      if (!s.running || !s.face) say(s.message || "Waiting for the computer's camera…", !!s.bad);
      else if ($("hStatus").classList.contains("bad")) say("Tracking");
    }, 500);
  }

  async function start() {
    if (run) return;
    if (isHost(st.res.device)) return startRemote();
    const btn = $("hStart"); btn.disabled = true;
    try {
      const size = st.res.size, fps = st.res.fps;
      say("Starting the camera…");
      const want = id => ({video: {deviceId: id ? {exact: id} : undefined, width: {ideal: size}, height: {ideal: Math.round(size * 3 / 4)}, frameRate: {ideal: fps, max: fps}}});
      let stream;
      try { stream = await navigator.mediaDevices.getUserMedia(want(st.res.device)); }
      catch (e) { if (e.name !== "OverconstrainedError" || !st.res.device) throw e; st.res.device = ""; stream = await navigator.mediaDevices.getUserMedia(want("")); }   // the saved camera is gone: the default one
      const video = $("hVideo");
      video.srcObject = stream; await video.play();
      const model = await ensureModel();
      filter.reset(); stats = {fps: 0, ms: 0, frames: 0, t0: performance.now(), face: false, lastFace: 0, sent: 0, err: 0};
      run = {stream, stop: false, lastT: 0, still: 0, prev: null};
      stream.getVideoTracks()[0].addEventListener("ended", () => stop("The camera stopped."));
      loop(model);
      btn.textContent = "Stop tracking"; document.body.classList.add("headon"); paintHud();
      say("Looking for your face… sit as you do when you drive, then it centres on you.");
      listCameras(false);
    } catch (e) {
      say("Could not start: " + (e.message || e.name), true);
      if (run) stop(); else { const v = $("hVideo"); if (v.srcObject) { v.srcObject.getTracks().forEach(t => t.stop()); v.srcObject = null; } }
    } finally { btn.disabled = false; }
  }

  function stop(msg) {
    if (run && run.remote) {
      clearInterval(run.poll); hostCmd("stop");
      run = null; $("hPrev").classList.remove("remote");
      const btn = $("hStart"); if (btn) btn.textContent = "Start tracking";
      document.body.classList.remove("headon"); say(msg || "Tracking is off."); paintHud();
      return;
    }
    if (run) { run.stop = true; run.stream.getTracks().forEach(t => t.stop()); }
    run = null;
    const v = $("hVideo"); if (v) v.srcObject = null;
    fetch("api/head", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({on: true})}).catch(() => {});   // a centred pose, then silence: the view eases back to the centre
    const btn = $("hStart"); if (btn) btn.textContent = "Start tracking";
    document.body.classList.remove("headon");
    say(msg || "Tracking is off.");
    paintHud();
  }

  // one frame at a time, never faster than asked, and slower still while the head is still (this is where the processor and graphics
  // chip get their rest)
  function loop(model) {
    const video = $("hVideo");
    const next = cb => video.requestVideoFrameCallback ? video.requestVideoFrameCallback(cb) : requestAnimationFrame(cb);
    const frame = () => {
      if (!run || run.stop) return;
      const now = performance.now();
      const every = 1000 / (st.res.eco && run.still > 45 ? 10 : st.res.fps);
      if (now - run.lastT >= every - 3 && video.readyState >= 2) {
        run.lastT = now;
        const t0 = performance.now();
        let res;
        try { res = model.detectForVideo(video, now); } catch (e) { say("The face model stopped: " + (e.message || e), true); stop(); return; }
        const ms = performance.now() - t0;
        stats.ms += (ms - stats.ms) * 0.1; stats.frames++;
        const m = res && res.facialTransformationMatrixes && res.facialTransformationMatrixes[0];
        if (m) {
          stats.face = true; stats.lastFace = now;
          const raw = HF.poseFromMatrix(m.data);
          last.raw = raw; last.lm = res.faceLandmarks && res.faceLandmarks[0];
          const r = filter.update(raw, now / 1000);
          if (r) {
            last.out = r.out; last.rel = r.rel;
            send(r.out);
            const mv = run.prev ? Math.abs(r.rel.yaw - run.prev.yaw) + Math.abs(r.rel.pitch - run.prev.pitch) + Math.abs(r.rel.x - run.prev.x) + Math.abs(r.rel.y - run.prev.y) : 9;
            run.still = mv < 0.6 ? run.still + 1 : 0; run.prev = r.rel;
          }
        } else if (stats.face && now - stats.lastFace > 300) { stats.face = false; filter.lost(); last.out = null; run.still = 0; }
      }
      next(frame);
    };
    next(frame);
  }

  // ---------------------------------------------------------------- a head movement without a head, to feel the sensitivity
  function startSweep() {
    if (sweep) { clearInterval(sweep.timer); sweep = null; $("hSweep").textContent = "Try a sweep without a face"; return; }
    const tf = new HF.Filter(st.sens); tf.center = {yaw: 0, pitch: 0, roll: 0, x: 0, y: 0, z: 0};
    const t0 = performance.now();
    $("hSweep").textContent = "Stop the sweep";
    sweep = {tf, timer: setInterval(() => {
      const t = (performance.now() - t0) / 1000;
      tf.setSettings(st.sens);
      const tri = (p, a) => a * (2 / Math.PI) * Math.asin(Math.sin(2 * Math.PI * t / p));                    // a triangle wave, +-a, period p
      const raw = {yaw: tri(8, 35), pitch: tri(5, 15), roll: 0, x: 0, y: 0, z: 0};
      const r = tf.update(raw, t);
      if (r) { last.raw = raw; last.out = r.out; last.rel = r.rel; send(r.out); }
      if (t > 20) startSweep();
    }, 33)};
  }

  // ---------------------------------------------------------------- the settings page
  const SLIDERS = [
    ["yaw.gain", "Turning your head", 0, 6, 0.1, v => v.toFixed(1) + "×", "How far the view turns for each degree your head turns. 2× means a 30° head turn shows 60° (more with the curve)."],
    ["pitch.gain", "Looking up and down", 0, 6, 0.1, v => v.toFixed(1) + "×", "The same for nodding."],
    ["roll.gain", "Tilting your head", 0, 3, 0.05, v => v.toFixed(2) + "×", "Most people want little or none of this."],
    ["curve", "Curve", 0, 2, 0.05, v => v === 0 ? "straight" : v.toFixed(2), "Higher is gentler near the centre (for fine aim) and quicker toward the edges (to reach the mirrors)."],
    ["deadzone", "Dead zone", 0, 6, 0.1, v => v.toFixed(1) + "°", "Around the centre nothing moves, so a resting head does not wander."],
    ["smoothness", "Smoothness", 0, 1, 0.05, v => Math.round(v * 100) + "%", "Higher is steadier and a little slower to follow."],
    ["quick", "Quick moves", 0, 1, 0.05, v => Math.round(v * 100) + "%", "Higher lets fast head movements through without smoothing, so a glance to the mirror is not slow."],
    ["x.gain", "Leaning sideways", 0, 3, 0.1, v => v.toFixed(1) + "×", "Moving your head left or right moves the camera (to peek round a pillar)."],
    ["y.gain", "Rising and ducking", 0, 3, 0.1, v => v.toFixed(1) + "×", ""],
    ["z.gain", "Leaning in", 0, 3, 0.1, v => v.toFixed(1) + "×", "Leaning toward the screen moves the camera forward."],
    ["maxYaw", "Furthest turn", 30, 180, 5, v => v + "°", "The view stops turning here."],
    ["maxPitch", "Furthest up or down", 20, 90, 5, v => v + "°", ""],
  ];
  const getPath = (o, p) => p.split(".").reduce((x, k) => x[k], o);
  const setPath = (o, p, v) => { const ks = p.split("."); const last = ks.pop(); ks.reduce((x, k) => x[k], o)[last] = v; };

  function buildSettings() {
    const box = $("hSliders"); if (!box || box.dataset.built) return; box.dataset.built = "1";
    box.innerHTML = SLIDERS.map(([path, label, min, max, step, fmt, help], i) => `
      <label class="hslide" title="${help}"><span>${label}</span><output id="hv${i}"></output>
        <input type="range" id="hs${i}" min="${min}" max="${max}" step="${step}"></label>${help ? `<div class="hhelp">${help}</div>` : ""}`).join("") +
      `<div class="hinv"><span>Reverse a direction if the view moves the wrong way:</span>` +
      [["yaw", "Turn"], ["pitch", "Up and down"], ["roll", "Tilt"], ["x", "Lean sideways"]].map(([a, l]) => `<label><input type="checkbox" data-inv="${a}"> ${l}</label>`).join("") + `</div>`;
    SLIDERS.forEach(([path, , , , , fmt], i) => {
      const el = $("hs" + i);
      el.oninput = () => { setPath(st.sens, path, +el.value); st.sens = HF.mergeSettings(st.sens); filter.setSettings(st.sens); paintSettings(); save(); };
    });
    box.querySelectorAll("[data-inv]").forEach(c => c.onchange = () => { st.sens[c.dataset.inv].invert = c.checked; filter.setSettings(st.sens); save(); });
    document.querySelectorAll("#hPresets button").forEach(b => b.onclick = () => {
      const p = PRESETS[b.dataset.p], keep = {};
      for (const a of HF.AXES) keep[a] = {...st.sens[a]};
      st.sens = HF.mergeSettings({...st.sens, ...p, yaw: {...keep.yaw, ...p.yaw}, pitch: {...keep.pitch, ...p.pitch}, roll: {...keep.roll, ...p.roll}});
      filter.setSettings(st.sens); paintSettings(); save();
    });
  }
  function paintSettings() {
    SLIDERS.forEach(([path, , , , , fmt], i) => { const v = getPath(st.sens, path), el = $("hs" + i); if (el && document.activeElement !== el) el.value = v; const o = $("hv" + i); if (o) o.textContent = fmt(v); });
    document.querySelectorAll("[data-inv]").forEach(c => c.checked = !!st.sens[c.dataset.inv].invert);
    document.querySelectorAll("#hPresets button").forEach(b => {
      const p = PRESETS[b.dataset.p];
      b.classList.toggle("on", Math.abs(st.sens.yaw.gain - p.yaw.gain) < 1e-6 && Math.abs(st.sens.pitch.gain - p.pitch.gain) < 1e-6 && Math.abs(st.sens.curve - p.curve) < 1e-6);
    });
  }

  function buildResources() {
    const r = st.res;
    $("hSize").value = r.size; $("hFps").value = r.fps; $("hProc").value = r.proc; $("hEco").checked = r.eco; $("hShow").checked = r.show;
    $("hSize").onchange = () => { r.size = +$("hSize").value; save(); if (run) restart(); };
    $("hFps").onchange = () => { r.fps = +$("hFps").value; save(); if (run) restart(); };
    $("hProc").onchange = () => { r.proc = $("hProc").value; save(); if (run) restart(); };
    $("hEco").onchange = () => { r.eco = $("hEco").checked; save(); };
    $("hShow").onchange = () => { r.show = $("hShow").checked; save(); paintPreview(); };
    $("hCam").onchange = () => { r.device = $("hCam").value; save(); if (run) restart(); };
  }
  async function restart() { stop("Restarting…"); await start(); }

  // ---------------------------------------------------------------- what the page shows while it runs (kept light: a few times a second)
  const bar = (v, lim) => `<i style="left:${50 + Math.max(-1, Math.min(1, v / lim)) * 50}%"></i>`;
  let lastShown = 0;
  function paintLive() {
    const page = $("head");
    if (!page || !page.classList.contains("on")) return;
    const now = performance.now();
    if (now - lastShown < 120) return; lastShown = now;
    if (run && !run.remote && now - stats.t0 > 1000) { stats.fps = Math.round(stats.frames * 1000 / (now - stats.t0)); stats.frames = 0; stats.t0 = now; }
    const rows = [["Turn", "yaw", 60, 120], ["Up and down", "pitch", 40, 60], ["Tilt", "roll", 30, 45]];
    $("hLive").innerHTML = rows.map(([l, a, hl, ol]) => {
      const rel = last.rel && last.rel[a] || 0, out = last.out && last.out[a] || 0;
      return `<div class="hrow"><span>${l}</span><div class="hbar"><b></b>${bar(rel, hl)}</div><em>head ${rel.toFixed(0)}°</em><div class="hbar view"><b></b>${bar(out, ol)}</div><em>view ${out.toFixed(0)}°</em></div>`;
    }).join("");
    if (run) {
      const msg = stats.face ? `Tracking${run.remote ? " on the computer" : ""}: ${stats.fps} frames a second, ${stats.ms.toFixed(0)} ms each${st.res.eco && run.still > 45 ? " (resting while you are still)" : ""}${stats.err ? ", " + stats.err + " failed sends" : ""}`
                             : "No face in view: sit where the camera can see you";
      if (!$("hStatus").classList.contains("bad")) say(msg);
    }
  }
  function paintPreview() {
    const wrap = $("hPrev"); if (!wrap) return;
    wrap.classList.toggle("off", !st.res.show || !run);
  }
  function paintHud() {                      // the recentre button over the cockpit map, while tracking
    const b = $("fHeadBtn"); if (b) b.style.display = run ? "" : "none";
    const hb = $("headBtn"), cb = $("headCenterBtn");
    if (hb) hb.classList.toggle("live", !!run);
    if (cb) cb.style.display = run ? "" : "none";
    paintPreview();
  }
  function drawFace() {
    const c = $("hCanvas"), v = $("hVideo");
    if (!c || !run || !st.res.show || !last.lm) return;
    const page = $("head"); if (!page || !page.classList.contains("on")) return;
    if (run.remote) return;                       // the computer's preview comes with its markers drawn in
    const box = $("hPrev");
    const vw = v.videoWidth || 320, vh = v.videoHeight || 240;
    if (box && v.videoWidth && box.dataset.ar !== `${vw}/${vh}`) { box.dataset.ar = `${vw}/${vh}`; box.style.setProperty("--cam-ar", `${vw} / ${vh}`); }
    // the canvas is the box's own size, and each point goes where object-fit: contain put that pixel of the video
    const bw = c.width = c.clientWidth || vw, bh = c.height = c.clientHeight || vh;
    const k = Math.min(bw / vw, bh / vh), ox = (bw - vw * k) / 2, oy = (bh - vh * k) / 2;
    const g = c.getContext("2d"); g.clearRect(0, 0, bw, bh);
    g.fillStyle = "#f2b134";
    for (const i of [1, 33, 263, 61, 291, 199]) { const p = last.lm[i]; if (p) { g.beginPath(); g.arc(ox + p.x * vw * k, oy + p.y * vh * k, 3, 0, 7); g.fill(); } }
  }

  // ---------------------------------------------------------------- the game's side
  let gameT = 0;
  async function checkGame() {
    const el = $("hGame"); if (!el) return;
    try {
      const r = await (await fetch("api/head/probe", {cache: "no-store"})).json();
      el.textContent = !r.ok ? "The game is not running, or does not have the BeamDeck mod loaded yet."
        : `The game is listening (camera: ${r.cam || "unknown"}). ` + (r.firstPerson ? (r.age < 2 ? "First person: it is receiving your head." : "First person: ready.")
                                                                               : "Not in first person yet: press 2 or C in the game. Head tracking only turns that view.");
      el.classList.toggle("bad", !r.ok);
    } catch { el.textContent = "BeamDeck's helper does not answer."; el.classList.add("bad"); }
  }

  // ---------------------------------------------------------------- start-up
  let built = false;
  window.headOnShow = async function () {
    if (!built) {
      built = true;
      buildSettings(); buildResources(); paintSettings();
      $("hStart").onclick = () => run ? stop() : start();
      $("hCenter").onclick = recenter; if ($("fHeadBtn")) $("fHeadBtn").onclick = recenter; if ($("headCenterBtn")) $("headCenterBtn").onclick = recenter;
      $("hSweep").onclick = startSweep;
      $("hFindCams").onclick = () => listCameras(true);
      document.addEventListener("keydown", e => { if (e.altKey && e.code === "KeyC" && run) recenter(); });
      setInterval(() => { paintLive(); drawFace(); }, 60);
      setInterval(() => { const p = $("head"); if (p && p.classList.contains("on")) checkGame(); }, 3000);
    }
    await listCameras(false);
    paintSettings(); paintPreview(); checkGame();
  };
  function recenter() {
    if (!run) return;
    if (run.remote) { hostCmd("recenter"); say("Centred on where you are now."); return; }
    if (filter.recenter(last.raw)) { say("Centred on where you are now."); setTimeout(() => { if (run) say("Tracking"); }, 1500); }
  }
  window.headTracker = {start, stop, recenter, isRunning: () => !!run, state: () => ({stats, last, sens: st.sens, res: st.res}), set: (path, v) => { setPath(st.sens, path, v); filter.setSettings(st.sens); paintSettings(); }};
  window.addEventListener("pagehide", () => { if (run) stop(); });
  paintHud();
  if ($("head") && $("head").classList.contains("on")) window.headOnShow();      // opened by a link before this file had loaded
})();
