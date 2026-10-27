// @-mentions while writing: type "@" and a few letters, pick a person or group (↑ ↓ Enter/Tab, or
// tap), and "@이름" goes into the text. Saving tags them (the server reads @Name back).
// In a meeting's "@ who was there" field (the day's people card), picking adds them to that meeting
// at once and the field stays ready for the next name; a name nobody has can be added as new.
// On a phone the list is a strip just above the keyboard: a list at the caret would sit under the
// keyboard, and iOS scrolls the page as you type (which used to close it).
(() => {
  if (window.mentionReady) return;
  window.mentionReady = true;
  const FIELDS = 'textarea[name="body"], textarea#capture-text, input.meet-input';
  let pop = null, state = null, last = null, timer = 0;
  const touch = matchMedia("(pointer: coarse)").matches, vv = window.visualViewport;

  const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // Where the caret is on screen: mirror the textarea's text up to the caret in a hidden div.
  function caretXY(ta) {
    const cs = getComputedStyle(ta), m = document.createElement("div");
    for (const p of ["fontFamily", "fontSize", "fontWeight", "lineHeight", "letterSpacing", "padding", "border",
                     "boxSizing", "whiteSpace", "wordWrap", "wordBreak", "width"]) m.style[p] = cs[p];
    Object.assign(m.style, { position: "absolute", visibility: "hidden", whiteSpace: "pre-wrap", top: "0", left: "-9999px" });
    m.textContent = ta.value.slice(0, ta.selectionStart);
    const mark = document.createElement("span");
    mark.textContent = "​";
    m.append(mark);
    document.body.append(m);
    const r = ta.getBoundingClientRect();
    const xy = { x: r.left + mark.offsetLeft - ta.scrollLeft, y: r.top + mark.offsetTop - ta.scrollTop + mark.offsetHeight };
    m.remove();
    return xy;
  }

  // "@query" right before the caret (no line break; a name can have one space: "@Aaron Li").
  function query(ta) {
    const before = ta.value.slice(0, ta.selectionStart);
    if (ta.matches(".meet-input")) {  // the whole field is the name, "@" optional
      const m = before.match(/@?([^@]{0,40})$/);
      return m && m[1].trim() !== "" || before.endsWith("@") ? { start: before.length - m[0].length, q: m[1].trim(), meet: true } : null;
    }
    const m = before.match(/(^|[\s(（"'“])@([^\s@]{0,20}(?: [^\s@]{0,20})?)$/);
    return m ? { start: before.length - m[2].length - 1, q: m[2] } : null;
  }

  function close() { if (pop) pop.hidden = true; state = null; }

  // Docked: the bottom of what's visible, i.e. right above the keyboard.
  function place() {
    if (!state || pop.hidden) return;
    if (touch && vv) {
      pop.style.top = (vv.offsetTop + vv.height - pop.offsetHeight) + "px";
      return;
    }
    const { x, y } = caretXY(state.ta);
    pop.style.left = Math.max(8, Math.min(x, innerWidth - pop.offsetWidth - 8)) + "px";
    pop.style.top = (y + 6 + pop.offsetHeight > innerHeight ? y - pop.offsetHeight - 28 : y + 6) + "px";
  }

  function render(ta, at, items) {
    if (!pop) {
      pop = document.createElement("div");
      pop.className = "suggest mention-pop" + (touch && vv ? " docked" : "");
      pop.setAttribute("role", "listbox");
      document.body.append(pop);
      pop.addEventListener("mousedown", (e) => e.preventDefault());  // keep the textarea's focus
      pop.addEventListener("pointerdown", (e) => e.preventDefault());
      pop.addEventListener("click", (e) => {
        const b = e.target.closest("button[data-i]");
        state = state || last;  // a tap can land just after the textarea lost focus
        if (b && state) choose(+b.dataset.i);
      });
    }
    if (at.meet && at.q && !items.some((p) => p.name === at.q)) items = [...items, { name: at.q, sub: "New person", isNew: true }];
    if (!items.length) return close();
    state = last = { ta, at, items, sel: 0 };
    pop.innerHTML = items.map((p, i) => `<button type="button" data-i="${i}" class="${i ? "" : "on"}${p.isNew ? " new" : ""}">
      ${p.isNew ? `<span class="avatar sm plus">＋</span>` : p.photo ? `<img class="avatar sm" src="/media/${p.photo}/thumb" alt="">` : `<span class="avatar sm">${esc(p.name.slice(0, 2))}</span>`}
      <span class="who"><span class="name">${esc(p.name)}</span>${p.sub ? `<span class="sub">${esc(p.sub)}</span>` : ""}</span></button>`).join("");
    pop.hidden = false;
    place();
  }

  // A meeting's field: add them to that meeting. Only the chips are replaced, never the field, so
  // it keeps focus (and the phone keeps its keyboard) for the next name.
  async function addToMeeting(ta, p) {
    ta.value = "";
    const body = new FormData();
    p.isNew ? body.append("name", p.name) : body.append("person_id", p.id);
    const res = await fetch(`/day/${ta.dataset.day}/people?meeting=${ta.dataset.meeting}`,
                            { method: "POST", body, headers: { "HX-Request": "true" } });
    if (!res.ok) return;
    const fresh = new DOMParser().parseFromString(await res.text(), "text/html").getElementById("day-people");
    const card = document.getElementById("day-people");
    if (!fresh || !card) return;
    const olds = card.querySelectorAll(".meeting"), news = fresh.querySelectorAll(".meeting");
    if (olds.length !== news.length) {
      card.replaceWith(fresh);
      htmx.process(fresh);
      return;
    }
    news.forEach((m, i) => {
      for (const sel of [".chips-people", ".mentioned"]) {
        const a = olds[i].querySelector(sel), b = m.querySelector(sel);
        if (a && b) a.replaceWith(b); else if (a) a.remove(); else if (b) olds[i].append(b);
        if (b) htmx.process(b);
      }
    });
  }

  function choose(i) {
    const { ta, at, items } = state, p = items[i];
    if (at.meet) {
      close();
      last = null;
      addToMeeting(ta, p);
      return;
    }
    const end = ta.selectionStart, text = `@${p.name} `;
    ta.setRangeText(text, at.start, end, "end");
    ta.dispatchEvent(new Event("input", { bubbles: true }));
    close();
    last = null;
    ta.focus();
  }

  function move(d) {
    state.sel = (state.sel + d + state.items.length) % state.items.length;
    pop.querySelectorAll("button").forEach((b, i) => b.classList.toggle("on", i === state.sel));
  }

  document.addEventListener("input", (e) => {
    const ta = e.target;
    if (!ta.matches?.(FIELDS)) return;
    const at = query(ta);
    if (!at) return close();
    clearTimeout(timer);
    timer = setTimeout(async () => {
      try {
        const items = await (await fetch(`/people/mention.json?q=${encodeURIComponent(at.q)}`)).json();
        if (document.activeElement === ta && query(ta)?.start === at.start) render(ta, at, items);
      } catch { close(); }
    }, 100);
  });

  // Capture phase: runs before the capture window's own Enter-to-save.
  document.addEventListener("keydown", (e) => {
    if (!state || pop.hidden || e.target !== state.ta || e.isComposing) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); move(e.key === "ArrowDown" ? 1 : -1); }
    else if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); e.stopImmediatePropagation(); choose(state.sel); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopImmediatePropagation(); close(); }
  }, true);

  document.addEventListener("focusout", (e) => {
    if (state && e.target === state.ta) setTimeout(() => { if (document.activeElement !== e.target) close(); }, 300);
  });
  if (touch && vv) {
    vv.addEventListener("resize", place);
    vv.addEventListener("scroll", place);
  } else addEventListener("scroll", () => state && close(), { passive: true });
})();
