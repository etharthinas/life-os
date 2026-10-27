// Chip fields (person details: 학교, 전공): the values are chips (hidden inputs named after the
// field), typed text suggests the names already in use, Enter or a tap adds one. A single field
// (학교) holds one chip; picking another replaces it. Cancel (form reset) puts the chips back.
(() => {
  if (window.chipfieldReady) return;
  window.chipfieldReady = true;
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const norm = (s) => s.replace(/\s+/g, "").toLowerCase();
  const values = (f) => [...f.querySelectorAll(".cf-chip input")].map((i) => i.value);
  const changed = (f) => f.closest("form")?.dispatchEvent(new Event("change", { bubbles: true }));

  function setup() {
    document.querySelectorAll(".chipfield:not([data-ready])").forEach((f) => {
      f.dataset.ready = "1";
      f.dataset.initial = JSON.stringify(values(f));
    });
  }
  addEventListener("load", setup);
  document.addEventListener("htmx:afterSettle", setup);
  if (document.readyState !== "loading") setup();

  function render(f, vals) {
    f.querySelectorAll(".cf-chip").forEach((c) => c.remove());
    const input = f.querySelector(".cf-input");
    for (const v of vals) input.insertAdjacentHTML("beforebegin",
      `<span class="cf-chip">${esc(v)}<input type="hidden" name="${esc(f.dataset.name)}" value="${esc(v)}"><button type="button" class="cf-x" aria-label="${esc(v)} 빼기">✕</button></span>`);
    if (!("multi" in f.dataset)) input.placeholder = vals.length ? "" : f.dataset.placeholder;
  }

  function add(f, v) {
    v = v.trim();
    if (!v) return;
    const known = JSON.parse(f.querySelector(".cf-options").textContent).find((o) => norm(o) === norm(v));
    v = known || v;  // written the way it already is ("산업 공학과" → 산업공학과)
    const vals = "multi" in f.dataset ? values(f).filter((x) => norm(x) !== norm(v)).concat(v) : [v];
    render(f, vals);
    const input = f.querySelector(".cf-input");
    input.value = "";
    changed(f);
    suggest(f);
    if (!("multi" in f.dataset)) { hide(f); input.blur(); }
  }

  function suggest(f) {
    const q = f.querySelector(".cf-input").value.trim();
    const have = new Set(values(f).map(norm));
    const opts = JSON.parse(f.querySelector(".cf-options").textContent).filter((o) => !have.has(norm(o)));
    const hits = (q ? opts.filter((o) => norm(o).includes(norm(q))) : opts).slice(0, 8);
    const box = f.querySelector(".cf-suggest");
    const exact = opts.some((o) => norm(o) === norm(q)) || have.has(norm(q));
    box.innerHTML = hits.map((o) => `<button type="button" data-v="${esc(o)}">${esc(o)}</button>`).join("")
      + (q && !exact ? `<button type="button" class="cf-new" data-v="${esc(q)}">＋ “${esc(q)}” 추가</button>` : "");
    box.hidden = !box.innerHTML;
  }
  const hide = (f) => { const b = f.querySelector(".cf-suggest"); b.hidden = true; };

  document.addEventListener("focusin", (e) => { const f = e.target.closest?.(".chipfield"); if (f && e.target.matches(".cf-input")) suggest(f); });
  document.addEventListener("input", (e) => { if (e.target.matches?.(".chipfield .cf-input")) suggest(e.target.closest(".chipfield")); });
  document.addEventListener("keydown", (e) => {
    if (!e.target.matches?.(".chipfield .cf-input") || e.isComposing) return;
    const f = e.target.closest(".chipfield");
    if (e.key === "Enter" || (e.key === "," && "multi" in f.dataset)) {
      e.preventDefault();  // not the form's submit
      const first = f.querySelector(".cf-suggest:not([hidden]) button");
      add(f, e.target.value.trim() ? (first && !first.classList.contains("cf-new") && norm(first.dataset.v).startsWith(norm(e.target.value)) ? first.dataset.v : e.target.value) : "");
    } else if (e.key === "Backspace" && !e.target.value) {
      const vals = values(f);
      if (vals.length) { render(f, vals.slice(0, -1)); changed(f); suggest(f); }
    } else if (e.key === "Escape") hide(f);
  });
  // Pressing a suggestion would blur the input first and hide the list: pick on pointerdown.
  document.addEventListener("pointerdown", (e) => {
    const b = e.target.closest?.(".chipfield .cf-suggest button");
    if (b) { e.preventDefault(); add(b.closest(".chipfield"), b.dataset.v); return; }
    document.querySelectorAll(".chipfield").forEach((f) => { if (!f.contains(e.target)) hide(f); });
  });
  document.addEventListener("click", (e) => {
    const x = e.target.closest?.(".chipfield .cf-x");
    if (x) {
      e.preventDefault();
      const f = x.closest(".chipfield");
      x.closest(".cf-chip").remove();
      render(f, values(f));
      changed(f);
      return;
    }
    const f = e.target.closest?.(".chipfield");
    if (f && !e.target.closest(".cf-suggest")) f.querySelector(".cf-input").focus();
  });
  document.addEventListener("reset", (e) => {
    e.target.querySelectorAll?.(".chipfield").forEach((f) => { render(f, JSON.parse(f.dataset.initial || "[]")); hide(f); });
  });
})();
