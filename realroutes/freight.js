"use strict";
// Freight: the dispatcher (job board, garage, planner, history) and the cockpit (map with directions, live truck stats,
// the truck's own controls). Runs inside console.html: $, api, esc, show, mode, RRMap and allMaps come from there.

const fMap = RRMap($("fMap"), $("fRecenter"));
window.fMap = fMap;
const fm = v => "$" + Math.round(v || 0).toLocaleString();
const fkm = v => (v == null ? "–" : v >= 100 ? Math.round(v).toLocaleString() : v.toFixed(1)) + " km";
const fdur = m => m >= 60 ? `${Math.floor(m / 60)}:${String(Math.round(m) % 60).padStart(2, "0")} h` : `${Math.round(m)} min`;
const fclock = s => { s = Math.max(0, Math.round(s)); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60); return h ? `${h}:${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}` : `${m}:${String(s % 60).padStart(2, "0")}`; };
const NEEDS = {tractor: "Semi tractor", box: "Box truck", flatbed: "Flatbed", tanker: "Tanker", dump: "Dump truck", mixer: "Mixer", drawbar: "Eye hitch (drawbar)", light: "Pickup / van"};

let fv = null;                       // the latest state.freight
let fb = null, fbV = -1, fbBusy = false;   // the board, and the version it was fetched at
let fTab = "jobs", fFilter = "fits", fSearch = "", fSel = null, fPlan = {via: []}, fSummaryKey = null;
try { fFilter = localStorage.getItem("rrFFilter") || "fits"; } catch {}

// ---------------------------------------------------------------- small helpers
let fToastT;
function ftoast(msg) {
  let el = $("fToast");
  if (!el) { el = document.createElement("div"); el.id = "fToast"; el.style.cssText = "position:fixed;left:50%;bottom:26px;transform:translateX(-50%);z-index:80;background:var(--md-surface-container-highest);border:1px solid var(--md-outline-variant);padding:12px 22px;border-radius:16px;font-weight:700;display:none;max-width:80vw;text-align:center"; document.body.appendChild(el); }
  el.textContent = msg; el.style.display = "block";
  clearTimeout(fToastT); fToastT = setTimeout(() => el.style.display = "none", 3600);
}
const fpost = (body) => api("api/freight", body).then(r => { if (r && r.ok === false && r.error) ftoast(r.error); return r; });
const truckCmd = (name, extra) => api("api/truck", {name, ...(extra || {})});
function throttle(fn, ms) { let t = 0, last, pend; return (...a) => { last = a; const now = performance.now(); if (now - t >= ms) { t = now; fn(...a); } else if (!pend) { pend = setTimeout(() => { pend = null; t = performance.now(); fn(...last); }, ms - (now - t)); } }; }

async function fetchBoard(hub) {
  if (fbBusy) return;
  fbBusy = true;
  try { fb = await api("api/freight/board" + (hub ? "?hub=" + encodeURIComponent(hub) : "")); fbV = fb.v; paintPages(); } catch {} finally { fbBusy = false; }
}

// ---------------------------------------------------------------- tabs
$("fTabs").onclick = e => { const b = e.target.closest("button"); if (b) setFTab(b.dataset.v); };
function setFTab(v) {
  fTab = v;
  document.querySelectorAll("#fTabs button").forEach(b => b.classList.toggle("on", b.dataset.v === v));
  document.querySelectorAll("#freight .fpage").forEach(p => p.classList.toggle("on", p.id === "fp" + v[0].toUpperCase() + v.slice(1)));
  paintPages();
}
$("fTruckChip").onclick = () => setFTab("garage");

// ---------------------------------------------------------------- the board
const canCarry = (caps, need) => need === "light" ? caps.includes("light") || caps.includes("box") : caps.includes(need);
// what a job needs and which of the player's trucks can do it, with one tap to switch
function altsFor(o) {
  if (o.alts) return o.alts;
  const hits = fb.garage.filter(t => canCarry(t.caps, o.needs));
  return {n: hits.length, top: hits.slice(0, 3).map(t => ({model: t.model, key: t.key, name: t.name, label: t.label, custom: t.custom}))};
}
function swapHTML(o) {
  if (o.fits) return "";
  const a = altsFor(o), need = (NEEDS[o.needs] || o.needs).toLowerCase();
  if (!a.n) return `<div class="os fneed">None of your trucks can do this: it needs ${esc(need)}. Mark a custom config in the Garage if it can.</div>`;
  return `<div class="os fneed">Needs ${esc(need)}. Switch to: ${a.top.map(t => `<button class="cap swap" data-m="${esc(t.model)}" data-k="${esc(t.key)}">${esc(t.label)}${t.custom ? " ★" : ""}</button>`).join(" ")}${a.n > 3 ? ` <span class="muted">+${a.n - 3} more in the Garage</span>` : ""}</div>`;
}

function offerHTML(o) {
  const route = [o.from.name, ...o.stops.map(s => s.name)];
  const shown = route.length > 5 ? [...route.slice(0, 2), `… ${route.length - 3} more …`, route[route.length - 1]] : route;
  const tags = [`<span class="cap ${o.fits ? "fit" : "warn"}">${esc(NEEDS[o.needs] || o.needs)}</span>`];
  if (o.kind === "scenario") tags.unshift(`<span class="cap on">Scenario</span>`);
  if (o.tailored) tags.unshift(`<span class="cap fit">Made for your truck</span>`);
  if (o.kind === "custom") tags.unshift(`<span class="cap on">Your route</span>`);
  if (o.fragile >= 0.4) tags.push(`<span class="cap warn">Fragile</span>`);
  if (o.hazard) tags.push(`<span class="cap hot">Dangerous goods</span>`);
  if (o.rush) tags.push(`<span class="cap hot">Rush</span>`);
  if (o.kind === "long") tags.push(`<span class="cap">Long haul</span>`);
  if (o.stops.length > 1) tags.push(`<span class="cap">${o.stops.length} drops</span>`);
  if (o.contract_label) tags.push(`<span class="cap ${o.contract === "rush" ? "hot" : "warn"}">${esc(o.contract_label)}</span>`);
  if (fb && o.created && o.kind !== "scenario" && o.kind !== "custom" && fb.now - o.created < 150) tags.unshift(`<span class="cap on">New</span>`);
  if (fb && o.expires && o.expires - fb.now < 240) tags.push(`<span class="cap hot">Leaves in ${Math.max(1, Math.round((o.expires - fb.now) / 60))} min</span>`);
  return `<div class="offer ${o.fits ? "" : "nofit"} ${o.kind === "scenario" ? "scen" : ""}">
    <div class="oh"><div class="oi">${o.icon}</div><div><div class="ot">${esc(o.title)}</div><div class="os">${esc(o.load)} · ${o.weight} t</div></div></div>
    <div class="or">${shown.map(esc).join("<i>→</i>")}</div>
    ${o.blurb ? `<div class="os">${esc(o.blurb)}</div>` : ""}
    <div class="tags">${tags.join("")}</div>
    ${swapHTML(o)}
    <div class="on"><div class="pay">${fm(o.pay)}</div><span>${o.km} km</span><span>${fdur(o.mins)}</span>
      <button class="take" data-id="${o.id}" ${o.fits ? "" : "disabled"}>${o.fits ? "Take job" : esc(o.why || "Not for this truck")}</button></div>
  </div>`;
}

function paintStaging() {
  const on = !fv || fv.staging !== false;
  document.querySelectorAll("#fStaging button").forEach(b => b.classList.toggle("on", (b.dataset.v === "1") === on));
}
$("fStaging").onclick = async e => { const b = e.target.closest("button"); if (b) { await fpost({action: "staging", on: b.dataset.v === "1"}); paintStaging(); } };

function paintJobs() {
  if (!fb) { $("fJobsList").innerHTML = `<div class="empty">Loading the job board…</div>`; return; }
  // the market town
  const sel = $("fHub");
  if (sel.dataset.v !== String(fb.towns.length)) {
    sel.dataset.v = String(fb.towns.length);
    const cor = new Set(fb.corridor);
    const opt = n => `<option value="${esc(n)}">${esc(n)}</option>`;
    sel.innerHTML = `<optgroup label="Ottawa – Toronto">${fb.corridor.map(opt).join("")}</optgroup>` +
      `<optgroup label="Elsewhere">${fb.towns.map(t => t.name).filter(n => !cor.has(n)).sort().map(opt).join("")}</optgroup>`;
  }
  if (document.activeElement !== sel) sel.value = fb.hub;
  const F = {all: () => true, fits: o => o.fits, local: o => o.kind === "local", regional: o => o.kind === "regional",
             long: o => o.kind === "long", scenario: o => o.kind === "scenario" || o.kind === "custom"};
  document.querySelectorAll("#fFilters button").forEach(b => b.classList.toggle("on", b.dataset.f === fFilter));
  paintStaging();
  const list = fb.offers.filter(F[fFilter] || F.all);
  $("fJobsCount").textContent = `${list.length} of ${fb.offers.length} jobs`;
  const banner = !fb.truck ? `<div class="fbanner"><b>No truck picked yet.</b> Every job needs one: choose it in the Garage (your own saved configs are listed first, marked Custom).<button class="go" id="fToGarage" style="padding:10px 22px;font-size:16px">Open the Garage</button></div>` : "";
  $("fJobsList").innerHTML = banner + (list.length ? list.map(offerHTML).join("") :
    `<div class="empty">${fFilter === "fits" ? "Nothing here fits your truck. Try All, another town, or another truck." : "No jobs of this kind here. Try another town."}</div>`);
  const g2 = $("fToGarage"); if (g2) g2.onclick = () => setFTab("garage");
}
$("fFilters").onclick = e => { const b = e.target.closest("button"); if (!b) return; fFilter = b.dataset.f; try { localStorage.setItem("rrFFilter", fFilter); } catch {} paintJobs(); };
$("fHub").onchange = () => fetchBoard($("fHub").value);
$("fRefresh").onclick = async () => { await fpost({action: "market", hub: fb && fb.hub, refresh: true}); fetchBoard(); };
$("fJobsList").onclick = async e => {
  const sw = e.target.closest(".swap");
  if (sw) { await fpost({action: "truck", model: sw.dataset.m, config: sw.dataset.k}); ftoast("Switched truck: the board now shows jobs it can carry"); return fetchBoard(); }
  const b = e.target.closest(".take"); if (!b || b.disabled) return;
  b.disabled = true;
  const r = await fpost({action: "start", offer: b.dataset.id});
  if (!r.ok) b.disabled = false;
};

// ---------------------------------------------------------------- the garage
const SPEC_LABEL = {power: "Power", torque: "Torque", weight: "Weight", top: "Top speed", drive: "Drivetrain", trans: "Transmission", body: "Body", fuel: "Fuel", years: "Years"};
function specRows(sp) {
  const rows = Object.entries(SPEC_LABEL).filter(([k]) => sp && sp[k] != null && sp[k] !== "").map(([k, l]) => {
    let v = sp[k];
    if (k === "power" && typeof v === "number") v = Math.round(v) + " hp";
    if (k === "torque" && typeof v === "number") v = Math.round(v) + " Nm";
    if (k === "weight" && typeof v === "number") v = Math.round(v).toLocaleString() + " kg";
    if (k === "top" && typeof v === "number") v = Math.round(v) + " km/h";
    return `<dt>${l}</dt><dd>${esc(v)}</dd>`;
  });
  return rows.length ? `<dl class="spec">${rows.join("")}</dl>` : `<div class="os muted">The game gave no specs for this config.</div>`;
}
function capChips(caps) { return caps.map(c => `<span class="cap fit">${esc(NEEDS[c] || c)}</span>`).join(""); }
function paintGarage() {
  if (!fb) return;
  const q = fSearch.trim().toLowerCase();
  const list = fb.garage.filter(t => !q || `${t.brand} ${t.name} ${t.label} ${t.key}`.toLowerCase().includes(q));
  const groups = new Map();
  list.forEach(t => { const k = t.model; if (!groups.has(k)) groups.set(k, {t, items: []}); groups.get(k).items.push(t); });
  const cur = fb.truck;
  $("fGarageList").innerHTML = groups.size ? [...groups.values()].map(g => `
    <div class="gmodel"><h3>${esc(g.t.brand)} ${esc(g.t.name)} <small>${g.items.length} config${g.items.length === 1 ? "" : "s"}${g.t.family === "eu" ? " · European" : ""}</small>
      <button class="star ${g.t.fav ? "on" : ""}" data-fav="${esc(g.t.model)}" title="Favourite">★</button></h3>
      ${g.items.map(t => `<button class="gcfg ${cur && cur.model === t.model && cur.config === t.key ? "sel" : ""}" data-m="${esc(t.model)}" data-k="${esc(t.key)}">
        <b>${esc(t.label)}${t.custom ? ` <span class="cap on">Custom</span>` : ""}</b><span class="caps">${t.caps.length ? capChips(t.caps) : `<span class="cap warn">Tap, then choose what it can do</span>`}${t.override ? `<span class="cap warn">set by you</span>` : ""}</span></button>`).join("")}
    </div>`).join("") : `<div class="empty">${fb.garage.length ? "No truck matches." : "No trucks yet. Start BeamNG with the BeamDeck mod, then press “Refresh from the game”."}</div>`;
  // the selected truck
  const t = cur && fb.garage.find(x => x.model === cur.model && x.key === cur.config);
  const fitN = fb.offers.filter(o => o.fits).length;
  $("fGarageSel").innerHTML = t ? `
    <h1>${esc(t.brand)} ${esc(t.name)}</h1><div class="os" style="font-size:15px">${esc(t.label)}${t.custom ? " · your saved config" : ""}</div>
    ${specRows(t.specs)}
    <div><div class="os" style="margin:6px 0">What this truck can do (tap to change if a mod truck is misread):</div>
      <div class="fchips" id="fCaps">${Object.keys(fb.needs).map(c => `<button class="${t.caps.includes(c) ? "on" : ""}" data-c="${c}">${esc(NEEDS[c] || c)}</button>`).join("")}</div>
      ${t.override ? `<button class="cap" style="margin-top:8px;cursor:pointer" id="fCapsReset">Back to automatic</button>` : ""}</div>
    <div class="os" style="font-size:15px">${fitN} job${fitN === 1 ? "" : "s"} on the board in ${esc(fb.hub)} fit this truck.</div>`
    : `<div class="empty">Pick a truck on the left.<br><small>A trailer that fits its hitch is added to each job automatically.</small></div>`;
}
$("fGarageList").onclick = async e => {
  const star = e.target.closest(".star");
  if (star) { await fpost({action: "favourite", model: star.dataset.fav}); return fetchBoard(); }
  const b = e.target.closest(".gcfg"); if (!b) return;
  await fpost({action: "truck", model: b.dataset.m, config: b.dataset.k}); fetchBoard();
};
$("fGarageSel").onclick = async e => {
  const t = fb && fb.truck; if (!t) return;
  if (e.target.id === "fCapsReset") { await fpost({action: "override", model: t.model, config: t.config, caps: []}); return fetchBoard(); }
  const b = e.target.closest("#fCaps button"); if (!b) return;
  const cur = fb.garage.find(x => x.model === t.model && x.key === t.config);
  const set = new Set(cur ? cur.caps : []); set.has(b.dataset.c) ? set.delete(b.dataset.c) : set.add(b.dataset.c);
  await fpost({action: "override", model: t.model, config: t.config, caps: [...set]}); fetchBoard();
};
$("fSearch").oninput = () => { fSearch = $("fSearch").value; paintGarage(); };
$("fTruckRefresh").onclick = async () => { await fpost({action: "trucks"}); ftoast("Asked the game for its trucks…"); setTimeout(fetchBoard, 1500); };

// ---------------------------------------------------------------- the planner: a custom route through any towns
function paintPlan() {
  if (!fb) return;
  const towns = fb.towns.map(t => t.name);
  const cor = fb.corridor, opts = (sel) => `<optgroup label="Ottawa – Toronto">${cor.map(n => `<option ${n === sel ? "selected" : ""}>${esc(n)}</option>`).join("")}</optgroup>` +
    `<optgroup label="Elsewhere">${towns.filter(n => !cor.includes(n)).sort().map(n => `<option ${n === sel ? "selected" : ""}>${esc(n)}</option>`).join("")}</optgroup>`;
  const from = fPlan.from || fb.hub, to = fPlan.to || (cor.includes(from) ? (from === "Toronto" ? "Ottawa" : "Toronto") : towns.find(n => n !== from));
  fPlan.from = from; fPlan.to = to;
  // towns between the two, in driving order, when both are on the corridor
  let between = [];
  if (cor.includes(from) && cor.includes(to)) {
    const i = cor.indexOf(from), j = cor.indexOf(to);
    between = i < j ? cor.slice(i + 1, j) : cor.slice(j + 1, i).reverse();
  }
  fPlan.via = fPlan.via.filter(v => between.includes(v));
  const cargo = fb.cargo.slice().sort((a, b) => b.fits - a.fits);
  fPlan.cargo = fPlan.cargo && cargo.some(c => c.id === fPlan.cargo) ? fPlan.cargo : (cargo.find(c => c.fits) || cargo[0]).id;
  $("fPlanForm").innerHTML = `
    <div class="row"><div><label>From</label><select id="pFrom">${opts(from)}</select></div><div style="font-size:26px;padding-top:18px">→</div><div><label>To</label><select id="pTo">${opts(to)}</select></div></div>
    ${between.length ? `<div><label>Drop-offs on the way (towns between, in driving order)</label><div class="fchips" id="pVia">${between.map(b => `<button data-t="${esc(b)}" class="${fPlan.via.includes(b) ? "on" : ""}">${esc(b)}</button>`).join("")}</div></div>`
      : `<div class="os">Pick two towns on the Ottawa – Toronto corridor to add drops in the towns between them.</div>`}
    <div><label>Cargo</label><select id="pCargo">${cargo.map(c => `<option value="${c.id}" ${c.id === fPlan.cargo ? "selected" : ""}>${c.icon} ${esc(c.name)} — ${c.fits ? "your truck can" : "needs " + esc((NEEDS[c.needs] || c.needs).toLowerCase())}</option>`).join("")}</select></div>
    <div class="row"><button class="go" id="pGo" style="padding:12px 26px;font-size:18px">Plan this job</button></div>
    <div id="pResult"></div>`;
  $("pFrom").onchange = () => { fPlan.from = $("pFrom").value; fPlan.via = []; paintPlan(); };
  $("pTo").onchange = () => { fPlan.to = $("pTo").value; fPlan.via = []; paintPlan(); };
  $("pCargo").onchange = () => { fPlan.cargo = $("pCargo").value; };
  const pv = $("pVia"); if (pv) pv.onclick = e => { const b = e.target.closest("button"); if (!b) return; const t = b.dataset.t; fPlan.via = fPlan.via.includes(t) ? fPlan.via.filter(x => x !== t) : [...fPlan.via, t]; paintPlan(); };
  $("pGo").onclick = async () => {
    const order = fPlan.via.slice().sort((a, b) => cor.indexOf(a) - cor.indexOf(b));
    if (cor.indexOf(from) > cor.indexOf(to)) order.reverse();
    const r = await fpost({action: "plan", from: fPlan.from, to: fPlan.to, via: order, cargo: fPlan.cargo});
    if (!r.ok || !r.offer) return;
    const o = r.offer; const fits = fb.cargo.find(c => c.id === o.cargo)?.fits;
    $("pResult").innerHTML = `<div class="fgrid" style="grid-template-columns:minmax(380px,520px)">${offerHTML({...o, fits: !!fits, why: fits ? "" : "Needs " + (fb.needs[o.needs] || o.needs)})}</div>`;
    $("pResult").onclick = async ev => {
      const sw = ev.target.closest(".swap");
      if (sw) { await fpost({action: "truck", model: sw.dataset.m, config: sw.dataset.k}); await fetchBoard(); return $("pGo").click(); }   // plan it again for the new truck
      const b = ev.target.closest(".take"); if (b && !b.disabled) { b.disabled = true; const x = await fpost({action: "start", offer: o.id}); if (!x.ok) b.disabled = false; }
    };
  };
}

// ---------------------------------------------------------------- history and stats
function paintHistory() {
  if (!fb) return;
  const s = fb.stats;
  const stars = n => "★".repeat(Math.round(n)) + "☆".repeat(5 - Math.round(n));
  $("fStats").innerHTML = [[fm(s.balance), "Balance"], [s.jobs, "Jobs"], [Math.round(s.km).toLocaleString() + " km", "Driven"], [Math.round(s.tonne_km).toLocaleString(), "Tonne-km"],
    [fm(s.best), "Best job"], [s.late, "Late"], [Math.round(s.fuel_l || 0) + " L", "Fuel used"]].map(([b, l]) => `<div class="stat"><b>${b}</b><span>${l}</span></div>`).join("");
  $("fHistory").innerHTML = fb.history.length ? `<table class="hist"><tr><th></th><th>Job</th><th>Route</th><th class="n">km</th><th class="n">Time</th><th class="n">Load</th><th>Truck</th><th class="n">Rating</th><th class="n">Paid</th></tr>` +
    fb.history.map(h => `<tr><td style="font-size:22px">${h.icon || "🚚"}</td><td><b>${esc(h.title)}</b><br><small class="muted">${esc(h.load)} · ${h.weight} t</small></td>
      <td>${esc(h.from)} → ${esc(h.to)}${h.stops > 1 ? ` <small class="muted">(${h.stops} drops)</small>` : ""}</td><td class="n">${Math.round(h.km_driven || h.km)}</td>
      <td class="n">${fdur(h.mins)}${h.late ? ` <small class="bad">late</small>` : ""}</td><td class="n">${h.cond}%</td><td>${esc(h.label || h.truck || "")}</td>
      <td class="n" style="color:#ffd166">${stars(h.stars)}</td><td class="n"><b>${fm(h.pay)}</b></td></tr>`).join("") + `</table>`
    : `<div class="empty">No deliveries yet. Take a job from the board.</div>`;
}

function paintPages() {
  if (!fb) return;
  $("fTruckName").textContent = fb.truck ? fb.truck.name : "No truck";
  $("fTruckSub").textContent = fb.truck ? fb.truck.label + (fb.caps.length ? " · " + fb.caps.map(c => NEEDS[c] || c).join(", ") : "") : "Pick one in the Garage";
  if (fTab === "jobs") paintJobs();
  else if (fTab === "garage") paintGarage();
  else if (fTab === "plan" && !$("pFrom")) paintPlan();
  else if (fTab === "history") paintHistory();
}

// ---------------------------------------------------------------- the cockpit: gauges
const GAUGES = [
  ["speed", "Speed", "speed wide"], ["rpm", "Engine", "wide"], ["gear", "Gear"], ["load", "Load"],
  ["water", "Coolant"], ["oil", "Oil"], ["fuel", "Fuel"], ["mass", "Weight"],
  ["cond", "Cargo"], ["trail", "Trailer"], ["trip", "Trip"], ["odo", "Odometer"],
  ["cruise", "Cruise"], ["ret", "Retarder"], ["park", "Park brake"], ["eng", "Engine"],
];
function buildGauges() {
  $("fGauges").innerHTML = GAUGES.map(([id, label, cls]) => `<div class="g ${cls || ""}" id="g_${id}"><b>–</b><span>${label}</span><i></i></div>`).join("");
}
function gset(id, text, opts = {}) {
  const el = $("g_" + id); if (!el) return;
  el.querySelector("b").innerHTML = text;
  el.classList.toggle("warn", opts.level === "warn"); el.classList.toggle("hot", opts.level === "hot");
  el.querySelector("i").style.width = opts.bar != null ? Math.max(0, Math.min(100, opts.bar)) + "%" : "0";
}
function paintGauges(t, job) {
  const n = v => v == null || Number.isNaN(v);
  gset("speed", `${t.speed ?? 0} <small>km/h${t.limit ? ` · limit ${t.limit}` : ""}</small>`, {level: t.limit && t.speed > t.limit + 25 ? "hot" : t.limit && t.speed > t.limit + 8 ? "warn" : null});
  gset("rpm", n(t.rpm) ? "–" : `${Math.round(t.rpm).toLocaleString()} <small>rpm${t.pto ? " · PTO " + t.pto : ""}</small>`, {bar: n(t.rpm) ? 0 : t.rpm / 25});
  gset("gear", n(t.gear) ? "–" : String(t.gear));
  gset("load", n(t.load) ? "–" : `${Math.round(t.load * 100)}<small>%</small>`, {bar: n(t.load) ? 0 : t.load * 100});
  gset("water", n(t.water) ? "–" : `${Math.round(t.water)}<small>°C</small>`, {level: t.water > 112 ? "hot" : t.water > 100 ? "warn" : null});
  gset("oil", n(t.oil) ? "–" : `${Math.round(t.oil)}<small>°C</small>`, {level: t.oil > 135 ? "hot" : t.oil > 120 ? "warn" : null});
  const lit = t.fuelL != null ? ` <small>${Math.round(t.fuelL)} L</small>` : "";
  gset("fuel", n(t.fuel) ? "–" : `${Math.round(t.fuel * 100)}<small>%</small>${lit}`, {bar: n(t.fuel) ? 0 : t.fuel * 100, level: t.lowfuel || t.fuel < 0.1 ? "hot" : t.fuel < 0.2 ? "warn" : null});
  gset("mass", n(t.mass) ? "–" : `${(t.mass / 1000).toFixed(1)}<small>t</small>`);
  gset("cond", `${job ? job.cond : 100}<small>%</small>`, {bar: job ? job.cond : 100, level: job && job.cond < 60 ? "hot" : job && job.cond < 85 ? "warn" : null});
  const hooked = job && job.rig && job.rig.trailer;
  gset("trail", hooked ? (t.tr > 0 ? "Hooked" : "Unhooked") : "None", {level: hooked && !(t.tr > 0) ? "hot" : null});
  gset("trip", n(t.trip) ? "–" : `${(t.trip / 1000).toFixed(1)}<small>km</small>`);
  gset("odo", n(t.odo) ? "–" : `${Math.round(t.odo / 1000).toLocaleString()}<small>km</small>`);
  gset("cruise", t.cc ? `${t.ccT ?? ""}<small>km/h</small>` : "Off");
  gset("ret", t.retS > 0 ? `${Math.round(t.retS * 100)}<small>%</small>` : "Off");
  gset("park", (t.pb || 0) > 0.5 ? "ON" : "Off", {level: (t.pb || 0) > 0.5 && (t.speed || 0) > 5 ? "hot" : null});
  gset("eng", t.check ? "Check" : t.eng ? "Running" : "Off", {level: t.check ? "warn" : null});
}

// ---------------------------------------------------------------- the cockpit: controls
// type: tog (toggle), step (- value +), seg (choices), act (a press), hold (while pressed)
const CTRL = [
  {g: "Braking", items: [
    {t: "step", id: "ret", label: "Retarder", has: "ret", dn: "retarder_down", up: "retarder_up", val: t => t.retS > 0 ? Math.round(t.retS * 100) + "%" : "Off", on: t => t.retS > 0},
    {t: "step", id: "cb", label: "Engine brake", has: "cb", dn: "jake_down", up: "jake_up", val: t => t.cbOn ? "Level " + (t.cb ?? "") : "Off", on: t => !!t.cbOn},
    {t: "tog", id: "park", label: "Parking brake", on: t => (t.pb || 0) > 0.5, send: t => (t.pb || 0) > 0.5 ? "park_off" : "park_on"},
  ]},
  {g: "Cruise control", items: [
    {t: "tog", id: "cc", label: "Cruise", sub: t => t.cc && t.ccT ? t.ccT + " km/h" : "set at current speed", on: t => !!t.cc, send: t => t.cc ? "cruise_off" : "cruise_on"},
    {t: "act", id: "ccd", label: "−5", name: "cruise_down"}, {t: "act", id: "ccd1", label: "−1", name: "cruise_down1"},
    {t: "act", id: "ccu1", label: "+1", name: "cruise_up1"}, {t: "act", id: "ccu", label: "+5", name: "cruise_up"},
  ]},
  {g: "Driveline", items: [
    {t: "tog", id: "dr", label: "Rear diff lock", has: "dr", on: t => (t.dm || {}).r === "locked", send: t => (t.dm || {}).r === "locked" ? "diff_rear_unlock" : "diff_rear_lock"},
    {t: "tog", id: "df", label: "Front diff lock", has: "df", on: t => (t.dm || {}).f === "locked", send: t => (t.dm || {}).f === "locked" ? "diff_front_unlock" : "diff_front_lock"},
    {t: "tog", id: "di", label: "Inter-axle lock", has: "di", on: t => (t.dm || {}).i === "locked", send: t => (t.dm || {}).i === "locked" ? "diff_inter_unlock" : "diff_inter_lock"},
    {t: "seg", id: "tc", label: "Transfer case", has: "tc", opts: [["2hi", "2 Hi", "tc_2hi"], ["4hi", "4 Hi", "tc_4hi"], ["4lo", "4 Lo", "tc_4lo"]], cur: t => (t.dm || {}).t},
    {t: "act", id: "la", label: "Lift axle", sub: "raise / lower", has: "la", name: "lift_axle"},
  ]},
  {g: "Trailer", show: (t, job) => !!(job && job.rig && job.rig.trailer) || t.tr > 0 || (t.has || {}).fw, items: [
    {t: "act", id: "fw", label: "Release fifth wheel", name: "fifth_release", danger: true},
    {t: "act", id: "pin", label: "Release eye hitch", name: "pintle_release", danger: true},
    {t: "act", id: "air", label: "Trailer air", sub: "supply on / off", name: "trailer_air"},
    {t: "act", id: "rec", label: "Hook up again", sub: "re-place the trailer", fa: "recouple"},
  ]},
  {g: "Body", show: (t) => !!((t.has || {}).pto || (t.has || {}).mix), items: [
    {t: "hold", id: "bu", label: "Bed up", sub: "hold", has: "pto", down: "bed_up", up: "bed_stop"},
    {t: "hold", id: "bd", label: "Bed down", sub: "hold", has: "pto", down: "bed_down", up: "bed_stop"},
    {t: "tog", id: "mx", label: "Mixer drum", has: "mix", on: t => !!t.mix, send: t => t.mix ? "mixer_off" : "mixer_on"},
    {t: "act", id: "mf", label: "Faster", has: "mix", name: "mixer_faster"}, {t: "act", id: "ms", label: "Slower", has: "mix", name: "mixer_slower"},
    {t: "act", id: "md", label: "Reverse", has: "mix", name: "mixer_dir"},
  ]},
];
let ctrlLast = {};
function buildControls() {
  const box = $("fControls"); box.innerHTML = "";
  CTRL.forEach((grp, gi) => {
    const wrap = document.createElement("div"); wrap.className = "card panel"; wrap.dataset.g = gi;
    wrap.innerHTML = `<h4>${grp.g}</h4><div class="cgroup"></div>`;
    const cg = wrap.querySelector(".cgroup");
    grp.items.forEach(it => {
      const el = document.createElement("div"); el.dataset.id = it.id; el.style.display = "contents";
      if (it.t === "step") el.innerHTML = `<div class="cstep"><button data-d="dn">−</button><div class="cv"><b></b><small>${it.label}</small></div><button data-d="up">+</button></div>`;
      else if (it.t === "seg") el.innerHTML = `<div class="cseg" title="${it.label}">${it.opts.map(o => `<button data-n="${o[2]}" data-k="${o[0]}">${o[1]}</button>`).join("")}</div>`;
      else el.innerHTML = `<button class="cb ${it.danger ? "danger" : ""}">${it.label}<small>${it.sub && typeof it.sub === "string" ? it.sub : ""}</small></button>`;
      cg.appendChild(el);
      wire(el, it);
    });
    box.appendChild(wrap);
  });
  // PTO lives in its own group at the top of the body controls
  const pto = document.createElement("div"); pto.className = "card panel"; pto.id = "fPtoCard";
  pto.innerHTML = `<h4>PTO and hydraulics</h4><div class="cgroup"><div class="ptowrap">
    <div class="ptopop" id="fPtoPop"><div class="ph"><b id="ptoVal">1500<small>rpm</small></b><span id="ptoLive">engine<br>– rpm</span></div>
      <div class="ptotrack" id="ptoTrack"><div class="fill"></div><div class="live"></div><div class="knob"></div></div>
      <div class="pre" id="ptoPre"></div></div>
    <button class="cb pto" id="ptoBtn">PTO<small>tap: engine speed high / low · hold: set the rpm</small></button></div></div>`;
  box.insertBefore(pto, box.firstChild);
  document.body.appendChild($("fPtoPop"));                // fixed to the screen: no scroll area clips it
  buildPto();
}
function wire(el, it) {
  const send = (n) => truckCmd(n);
  if (it.t === "step") el.querySelectorAll("button").forEach(b => b.onclick = () => send(b.dataset.d === "dn" ? it.dn : it.up));
  else if (it.t === "seg") el.querySelectorAll("button").forEach(b => b.onclick = () => send(b.dataset.n));
  else {
    const b = el.querySelector("button");
    if (it.t === "hold") {
      const stop = () => { if (b._held) { b._held = false; b.classList.remove("on"); send(it.up); } };
      b.addEventListener("pointerdown", e => { b._held = true; b.classList.add("on"); b.setPointerCapture(e.pointerId); send(it.down); });
      ["pointerup", "pointercancel", "lostpointercapture"].forEach(ev => b.addEventListener(ev, stop));
    } else b.onclick = () => {
      if (it.fa) fpost({action: it.fa}); else send(it.name || it.send(ctrlLast.t || {}));
    };
  }
}
function paintControls(t, job) {
  ctrlLast = {t};
  const has = t.has || {};
  document.querySelectorAll("#fControls > .card[data-g]").forEach(wrap => {
    const grp = CTRL[+wrap.dataset.g];
    let any = false;
    grp.items.forEach(it => {
      const el = wrap.querySelector(`[data-id="${it.id}"]`);
      let vis = (!it.has || has[it.has]) && (!it.show || it.show(t, job));
      if (it.has && !Object.keys(has).length) vis = false;                    // nothing known yet: don't guess
      el.classList.toggle("ctrlhide", !vis); el.style.display = vis ? "contents" : "none";
      if (vis) any = true; else return;
      if (it.t === "step") { el.querySelector(".cv b").textContent = it.val(t); el.querySelector(".cstep").classList.toggle("on", it.on(t)); }
      else if (it.t === "seg") { const c = it.cur(t); el.querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.k === c)); }
      else if (it.t === "tog") { const b = el.querySelector("button"); b.classList.toggle("on", !!it.on(t)); if (it.sub) b.querySelector("small").textContent = it.sub(t); }
    });
    wrap.style.display = any && (!grp.show || grp.show(t, job)) ? "" : "none";
  });
  paintPto(t);
}

// ---------------------------------------------------------------- PTO: tap = engine speed high / low, hold = the rpm slider
const PTO_MIN = 800, PTO_MAX = 2200, PTO_PRESETS = [800, 1200, 1500, 1800, 2200];
let ptoPreset = 1500, ptoOpenT = null, ptoActive = null, ptoLiveRpm = null;
try { ptoPreset = +localStorage.getItem("rrPto") || 1500; } catch {}
const ptoSend = throttle(rpm => truckCmd("pto", {rpm}), 110);
const ptoPos = rpm => (rpm - PTO_MIN) / (PTO_MAX - PTO_MIN);
function ptoApply(rpm, send = true) {
  rpm = Math.round(Math.max(PTO_MIN, Math.min(PTO_MAX, rpm)) / 10) * 10;
  ptoPreset = rpm; try { localStorage.setItem("rrPto", rpm); } catch {}
  ptoPaintPop();
  if (send) ptoSend(rpm);
}
function ptoPaintPop() {
  const tr = $("ptoTrack"); if (!tr) return;
  const p = ptoPos(ptoPreset) * 100;
  tr.querySelector(".fill").style.width = `calc(${p}% + 3px)`;
  tr.querySelector(".knob").style.left = `${p}%`;
  $("ptoVal").innerHTML = `${ptoPreset.toLocaleString()}<small>rpm</small>`;
  $("ptoPre").querySelectorAll("button[data-r]").forEach(b => b.classList.toggle("on", +b.dataset.r === ptoPreset));
  const lv = tr.querySelector(".live");
  if (ptoLiveRpm != null) { lv.style.opacity = ".9"; lv.style.left = `${Math.max(0, Math.min(1, ptoPos(ptoLiveRpm))) * 100}%`; }
  $("ptoLive").innerHTML = `engine now<br><b>${ptoLiveRpm != null ? Math.round(ptoLiveRpm).toLocaleString() : "–"}</b> rpm`;
}
function ptoPlace() {
  const pop = $("fPtoPop"), r = $("ptoBtn").getBoundingClientRect(), W = pop.offsetWidth || 430;
  const left = Math.max(10, Math.min(r.left, innerWidth - W - 10));
  let top = r.top - pop.offsetHeight - 16, below = false;
  if (top < 8) { top = r.bottom + 16; below = true; }
  pop.style.left = left + "px"; pop.style.top = top + "px"; pop.classList.toggle("below", below);
  pop.style.setProperty("--ax", Math.max(14, Math.min(W - 40, r.left + r.width / 2 - left - 9)) + "px");
}
function ptoOpen() {
  $("fPtoPop").classList.add("on"); ptoPaintPop(); ptoPlace(); ptoKeepOpen();
}
function ptoClose() { $("fPtoPop").classList.remove("on"); clearTimeout(ptoOpenT); }
function ptoKeepOpen(ms = 5500) { clearTimeout(ptoOpenT); ptoOpenT = setTimeout(ptoClose, ms); }
function buildPto() {
  const btn = $("ptoBtn"), track = $("ptoTrack");
  $("ptoPre").innerHTML = PTO_PRESETS.map(r => `<button data-r="${r}">${r}</button>`).join("") + `<button class="off" id="ptoOff">Low</button>`;
  $("ptoPre").onclick = e => {
    const b = e.target.closest("button"); if (!b) return;
    if (b.id === "ptoOff") { truckCmd("pto", {rpm: 0}); ptoClose(); return; }
    ptoApply(+b.dataset.r); ptoKeepOpen();
  };
  // the slider itself: drag anywhere on the track
  const fromX = x => { const r = track.getBoundingClientRect(); return PTO_MIN + Math.max(0, Math.min(1, (x - r.left) / r.width)) * (PTO_MAX - PTO_MIN); };
  track.addEventListener("pointerdown", e => { track.setPointerCapture(e.pointerId); track.classList.add("drag"); ptoApply(fromX(e.clientX)); ptoKeepOpen(9000); });
  track.addEventListener("pointermove", e => { if (track.classList.contains("drag")) { ptoApply(fromX(e.clientX)); ptoKeepOpen(9000); } });
  ["pointerup", "pointercancel"].forEach(ev => track.addEventListener(ev, () => { track.classList.remove("drag"); ptoKeepOpen(); }));
  // the button: a tap switches high / low; holding it opens the slider, and the same finger can slide the rpm
  let timer = null, held = false, down = false;
  btn.addEventListener("pointerdown", e => {
    down = true; held = false; btn.setPointerCapture(e.pointerId);
    timer = setTimeout(() => { held = true; btn.classList.add("holding"); navigator.vibrate && navigator.vibrate(18); ptoOpen(); if (!ptoActive) ptoApply(ptoPreset); }, 380);
  });
  btn.addEventListener("pointermove", e => { if (down && held) { ptoApply(fromX(e.clientX)); ptoKeepOpen(9000); } });
  const up = () => {
    if (!down) return; down = false; clearTimeout(timer); btn.classList.remove("holding");
    if (held) { ptoKeepOpen(); return; }
    // a tap
    if (ptoActive) truckCmd("pto", {rpm: 0}); else { truckCmd("pto", {rpm: ptoPreset}); }
    ptoClose();
  };
  btn.addEventListener("pointerup", up);
  btn.addEventListener("pointercancel", () => { down = false; clearTimeout(timer); btn.classList.remove("holding"); });
  document.addEventListener("pointerdown", e => { if (!e.target.closest(".ptowrap") && !e.target.closest("#fPtoPop")) ptoClose(); });
  if (location.hash === "#freight/pto") setTimeout(ptoOpen, 700);                       // for screenshots
}
function paintPto(t) {
  ptoActive = t.pto || null; ptoLiveRpm = t.rpm ?? null;
  const b = $("ptoBtn"); if (!b) return;
  b.classList.toggle("on", !!t.pto);
  b.innerHTML = `PTO · ${t.pto ? "High" : t.raised ? "High (auto)" : "Low"}<small>${t.pto ? t.pto.toLocaleString() + " rpm · tap for low · hold to adjust" : "tap: engine high · hold: set the rpm"}</small>`;
  if ($("fPtoPop").classList.contains("on")) ptoPaintPop();
}

// ---------------------------------------------------------------- the cockpit: the job strip
function paintStrip(job, t, st) {
  const s = $("fStrip"), phase = st.state;
  const drops = job.stops.map(p => `<span class="${p.done ? "done" : p.now ? "now" : ""}">${p.done ? "✓ " : ""}${esc(p.name)}</span>`).join("");
  let body = "";
  if (phase === "resolving" || phase === "spawning") {
    body = `<div class="jstage">⏳ ${esc(job.stage)}</div>`;
  } else if (phase === "to_pickup") {
    const pk = job.pickup || {};
    body = `<div class="jstage">📍 Drive to the pickup: <b>${esc(pk.name || "")}</b></div>
      <div class="jnums"><div><b>${fkm(pk.dist_km)}</b><span>To the pickup</span></div><div><b>${job.mins ? fdur(job.mins) : "–"}</b><span>Time allowed</span></div>
      <div><b>–</b><span>Cargo · not loaded</span></div><div><b>${fm(job.pay)}</b><span>Pay</span></div></div>
      <div class="jstage" style="padding:2px 0">The clock starts once the load is on. Stop in the pickup area to load.
        <button class="cap" id="fSkip" style="cursor:pointer;margin-left:10px">Skip the drive</button></div>`;
  } else if (phase === "loading") {
    body = `<div class="jstage">📦 Loading ${esc(job.load)} · stay stopped · <b>${job.load_s ?? 0} s</b></div>
      <div class="jbar"><i style="width:${job.load_pct || 0}%"></i></div>`;
  } else if (phase === "driving") {
    const left = job.left, late = left < 0;
    const unl = job.unload_pct > 0 ? `<div class="jstage" style="padding:0">📤 Unloading · keep still</div><div class="jbar"><i style="width:${job.unload_pct}%"></i></div>` : "";
    body = `<div class="jnums">
      <div><b class="${late ? "late" : left < 600 ? "late" : ""}">${late ? "+" : ""}${fclock(Math.abs(left))}</b><span>${late ? "Late by" : "Time left"}</span></div>
      <div><b>${fkm(job.dist_left_km)}</b><span>To next drop</span></div>
      <div><b class="${job.cond < 60 ? "bad" : job.cond < 85 ? "late" : ""}">${job.cond}%</b><span>Cargo${job.jolts ? ` · ${job.jolts} jolts` : ""}</span></div>
      <div><b class="${job.tickets ? "bad" : ""}">${fm(job.pay)}</b><span>${job.tickets ? job.tickets + " ticket" + (job.tickets > 1 ? "s" : "") : "Pay"}</span></div></div>${unl}`;
  }
  s.innerHTML = `<div class="jt"><div class="ji">${job.icon}</div><div><b>${esc(job.title)}</b><small>${esc(job.load)} · ${job.weight} t · from ${esc(job.from)}${job.rig && job.rig.trailer_name ? " · " + esc(job.rig.trailer_name) : ""}${job.rig && job.rig.load_name ? " · " + esc(job.rig.load_name) : ""}</small></div>
    <button class="jcancel" id="fCancel">Cancel job</button></div><div class="jdrops">${drops}</div>${body}`;
  $("fCancel").onclick = () => { if (confirm("Cancel this job? The pay is lost.")) fpost({action: "cancel"}); };
  const sk = $("fSkip"); if (sk) sk.onclick = () => fpost({action: "skip_to_pickup"});
}

// ---------------------------------------------------------------- summary
function fSummary(sm) {
  const key = sm ? (sm.failed ? "f:" + sm.why : "d:" + sm.job.t) : null;
  if (key === fSummaryKey) return;
  fSummaryKey = key;
  const box = $("fSummary");
  if (!sm) { box.classList.remove("on"); return; }
  const stars = n => "★".repeat(Math.round(n)) + "☆".repeat(5 - Math.round(n));
  if (sm.failed) {
    box.querySelector(".card").innerHTML = `<div class="muted">${esc(sm.title || "Job")}</div><div class="big" style="color:var(--bad);font-size:38px">Couldn't start</div><p style="margin:14px 0">${esc(sm.why)}</p><button class="go" id="fSumDone" style="width:100%">OK</button>`;
  } else {
    const s = sm.score, j = sm.job;
    box.querySelector(".card").innerHTML = `<div class="muted">${esc(sm.title)} delivered</div><div class="big">${fm(s.pay)}</div><div class="stars">${stars(s.stars)}</div>
      <div class="rows"><div><b>${fm(s.base)}</b><span>Offered</span></div><div><b>×${s.cond_f}</b><span>Cargo ${j.cond}%</span></div><div><b>×${s.time_f}</b><span>${s.late ? "Late" : "On time"} · ${fdur(s.mins)}</span></div>
        <div><b>${fkm(j.km_driven)}</b><span>Driven</span></div><div><b>${j.avg_kmh} km/h</b><span>Average</span></div><div><b>${j.fuel_l ? j.fuel_l + " L" : "–"}</b><span>Fuel used</span></div>
        ${s.fines ? `<div><b class="bad">−${fm(s.fines)}</b><span>${sm.tickets} ticket${sm.tickets > 1 ? "s" : ""}</span></div>` : ""}</div>
      <button class="go" id="fSumDone" style="width:100%">Done — drop the trailer</button>`;
  }
  $("fSumDone").onclick = () => { box.classList.remove("on"); fpost({action: "dismiss"}); };
  box.classList.add("on");
}

// ---------------------------------------------------------------- state from the helper (every poll)
let lastActive = false;
function renderFreight(st) {
  fv = st.freight; if (!fv) return;
  const active = ["resolving", "spawning", "to_pickup", "loading", "driving"].includes(fv.state);
  const root = $("freight");
  root.classList.toggle("driving", active);
  document.body.classList.toggle("fdriving", active && mode === "freight");
  $("fBal").textContent = fm(fv.balance); $("fJobs").textContent = fv.jobs;
  if (fv.v !== fbV && mode === "freight") fetchBoard();
  if (active && mode === "freight" && !lastActive) setTimeout(() => fMap.resize(), 60);
  lastActive = active;
  fSummary(fv.summary);
  if (mode !== "freight") return;
  const g = st.game;
  if (active && fv.job) {
    paintStrip(fv.job, fv.tel || {}, fv);
    paintGauges(fv.tel || {}, fv.job); paintControls(fv.tel || {}, fv.job);
    const stop = fv.job.stop, pk = fv.job.pickup;
    const rest = fv.job.stops.filter(p => !p.done && p.lat != null);
    fMap.hud(g, fv.state === "to_pickup" && pk ? "Pickup: " + pk.name : stop ? (fv.job.idx === fv.job.stops.length ? "Drop-off: " : "Stop: ") + stop.name : null);
    fMap.turn(st.nav);
    const pins = rest.map(p => ({lat: p.lat, lon: p.lon, color: p.now ? "#f06ac0" : "#ffb547", label: (p.now ? "Drop-off: " : "") + p.name, small: !p.now, next: p.now}));
    if (fv.state === "to_pickup" && pk) pins.unshift({lat: pk.lat, lon: pk.lon, color: "#4fd18b", label: "Pickup: " + pk.name});
    fMap.pins(pins);
  } else fMap.pins([]);
}

// ---------------------------------------------------------------- start
buildGauges(); buildControls();
document.querySelectorAll("#fControls > .card[data-g]").forEach(c => c.style.display = "none");   // shown once the truck says what it has
const hashTab = (location.hash.match(/^#freight\/(\w+)/) || [])[1];
if (["garage", "plan", "history"].includes(hashTab)) setFTab(hashTab);
window.renderFreight = renderFreight;
window.fOnShow = () => { fetchBoard(); setTimeout(() => fMap.resize(), 60); };
