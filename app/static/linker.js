// Link any words in a log to someone: select them, then right-click (or, on a phone, tap the
// 🔗 button that appears by the selection) and pick a person or group. Every place the words are
// written in that entry becomes a link to them (POST /entries/<id>/link).
(() => {
  if (window.linkerReady) return;
  window.linkerReady = true;
  const touch = matchMedia("(pointer: coarse)").matches;
  let menu = null, chip = null, sel = null, timer = 0;

  const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // The selected words and the entry they're in, if the selection is a short phrase inside one log.
  function current() {
    const s = getSelection();
    if (!s || s.isCollapsed || !s.rangeCount) return null;
    const text = s.toString().trim();
    const prose = s.getRangeAt(0).commonAncestorContainer.parentElement?.closest(".prose[data-raw]");
    if (!prose || !text || text.length > 40 || /\n/.test(text)) return null;
    return { text, raw: prose.dataset.raw, rect: s.getRangeAt(0).getBoundingClientRect() };
  }

  function close() {
    menu?.remove(); menu = null;
    chip?.remove(); chip = null;
  }

  async function search(q) {
    const items = await (await fetch(`/people/mention.json?q=${encodeURIComponent(q)}`)).json();
    const list = menu?.querySelector(".link-results");
    if (!list) return;
    list.innerHTML = items.length ? items.map((p) => `<button type="button" data-id="${p.id}">
      ${p.photo ? `<img class="avatar sm" src="/media/${p.photo}/thumb" alt="">` : `<span class="avatar sm">${esc(p.name.slice(0, 2))}</span>`}
      <span class="who"><span class="name">${esc(p.name)}</span>${p.sub ? `<span class="sub">${esc(p.sub)}</span>` : ""}</span></button>`).join("")
      : `<p class="hint">No one called “${esc(q)}”.</p>`;
  }

  function open(at, x, y) {
    close();
    sel = at;
    menu = document.createElement("div");
    menu.className = "suggest link-menu";
    menu.innerHTML = `<span class="q">Link “${esc(at.text)}” to…</span>
      <input type="search" value="${esc(at.text)}" autocomplete="off" aria-label="Who">
      <div class="link-results"></div>`;
    document.body.append(menu);
    menu.style.left = Math.max(8, Math.min(x, innerWidth - menu.offsetWidth - 8)) + "px";
    menu.style.top = Math.max(8, Math.min(y, innerHeight - 320)) + "px";
    const input = menu.querySelector("input");
    input.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(() => search(input.value.trim()), 120); });
    menu.addEventListener("click", async (e) => {
      const b = e.target.closest("button[data-id]");
      if (!b) return;
      const body = new FormData();
      body.append("text", sel.text);
      body.append("person_id", b.dataset.id);
      const res = await fetch(`/entries/${sel.raw}/link`, { method: "POST", body });
      if (res.ok) location.reload();
    });
    search(at.text);
    if (!touch) input.focus();
  }

  // Desktop: right-click on a selection.
  document.addEventListener("contextmenu", (e) => {
    const at = current();
    if (!at || !e.target.closest?.(".prose[data-raw]")) return;
    e.preventDefault();
    open(at, e.clientX, e.clientY);
  });

  // Phone: a long press selects; a 🔗 button shows up under the selection.
  if (touch) document.addEventListener("selectionchange", () => {
    if (menu) return;
    const at = current();
    chip?.remove(); chip = null;
    if (!at) return;
    chip = document.createElement("button");
    chip.type = "button";
    chip.className = "link-chip";
    chip.textContent = "🔗 Link";
    chip.style.left = Math.max(8, Math.min(at.rect.left, innerWidth - 90)) + "px";
    chip.style.top = (at.rect.bottom + 10) + "px";
    chip.addEventListener("click", () => open(at, at.rect.left, at.rect.bottom + 10));
    document.body.append(chip);
  });

  // Phone: the first tap on a name opens its menu (who it is, undo); a second tap opens their page.
  if (touch) document.addEventListener("click", (e) => {
    const name = e.target.closest?.(".who-is");
    document.querySelectorAll(".who-is.shown").forEach((x) => x !== name && x.classList.remove("shown"));
    if (name && e.target.closest("a") && !name.classList.contains("shown")) {
      e.preventDefault();
      name.classList.add("shown");
    }
  }, true);

  document.addEventListener("keydown", (e) => { if (e.key === "Escape") close(); });
  document.addEventListener("mousedown", (e) => { if (menu && !menu.contains(e.target) && e.target !== chip) close(); });
  addEventListener("scroll", () => chip && !menu && close(), { passive: true });
})();
