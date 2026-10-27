// A day's meetings (_day_people.html): drag a meeting by ⠿ to put them in order, drag a chip (a
// person, or a group with the people under it) into another meeting; long press or right-click a
// chip for a menu: 전체 해제 for a group (its label off, the people stay, not regrouped), move to
// another meeting, open their page. "Mentioned" follows the meeting you're at.
(() => {
  if (window.dayPeopleReady) return;
  window.dayPeopleReady = true;
  const touch = matchMedia("(pointer: coarse)").matches;
  let menu = null, pressTimer = 0, pressAt = null, swallowClick = false;

  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const dayOf = (box) => box.querySelector(".meet-input")?.dataset.day;
  const post = (box, path, vals) => htmx.ajax("POST", `/day/${dayOf(box)}${path}`,
    { target: "#day-people", swap: "outerHTML", values: vals });

  function setup(box) {
    if (!window.Sortable || box.dataset.sortable) return;
    box.dataset.sortable = "1";
    const opts = { animation: 150, delay: 250, delayOnTouchOnly: true, touchStartThreshold: 6 };
    if (box.classList.contains("many")) {
      Sortable.create(box, { ...opts, draggable: ".meeting", handle: ".meeting-grip", delay: 0,
        onEnd: (e) => {
          if (e.oldIndex === e.newIndex) return;
          const order = [...box.querySelectorAll(":scope > .meeting")].map((m) => m.dataset.meeting);
          post(box, "/meetings/order", { order: order.join(",") });
        } });
    }
    box.querySelectorAll(".meeting > .chips-people, .new-meeting > .chips-people").forEach((ul) => Sortable.create(ul, {
      ...opts, group: "day-people", sort: false, draggable: "li[data-ids]",
      filter: ".unpick", preventOnFilter: false,
      onStart: () => { closeMenu(); clearTimeout(pressTimer); box.classList.add("chip-dragging"); },
      onEnd: () => box.classList.remove("chip-dragging"),
      onAdd: (e) => post(box, "/move", { ids: e.item.dataset.ids, meeting: e.to.closest("[data-meeting]").dataset.meeting }),
    }));
  }
  // "Mentioned" (people the text only names) goes under the meeting you're at: the one the mouse
  // rests on, or you're typing in or touched. Its ＋ adds to that meeting.
  let active = 0, dwell = 0;
  function place(box) {
    const list = box.querySelector(":scope > .mentioned, .meeting > .mentioned");
    const mt = box.querySelector(`.meeting[data-meeting="${active}"]`) || box.querySelector(".meeting");
    if (list && mt && list.parentElement !== mt) mt.append(list);
  }
  const go = (mt) => {
    if (!mt || +mt.dataset.meeting === active) return;
    active = +mt.dataset.meeting;
    place(mt.closest("#day-people"));
  };
  const meetingAt = (t) => t.closest?.("#day-people.many .meeting");
  document.addEventListener("mousemove", (e) => {
    const mt = meetingAt(e.target);
    clearTimeout(dwell);
    if (mt) dwell = setTimeout(() => go(mt), 200);  // resting, not passing over (moving it shifts the layout)
  }, { passive: true });
  document.addEventListener("focusin", (e) => go(meetingAt(e.target)));
  document.addEventListener("touchstart", (e) => go(meetingAt(e.target)), { passive: true });
  document.addEventListener("htmx:configRequest", (e) => {
    const mt = e.detail.elt.closest?.(".mentioned")?.closest(".meeting");
    if (mt) e.detail.path = e.detail.path.replace(/meeting=\d+/, `meeting=${mt.dataset.meeting}`);
  });

  let day = null;
  const setupAll = () => document.querySelectorAll("#day-people").forEach((box) => {
    if (dayOf(box) !== day) { day = dayOf(box); active = 0; }  // another day: start at its first meeting
    place(box); setup(box);
  });
  addEventListener("load", setupAll);
  document.addEventListener("htmx:afterSettle", setupAll);
  if (document.readyState !== "loading") setupAll();

  function closeMenu() { menu?.remove(); menu = null; }

  function openMenu(chip, x, y) {
    closeMenu();
    if (chip.closest(".meeting.selecting")) return;  // 묶기 has its own taps
    const box = chip.closest("#day-people"), here = chip.closest(".meeting");
    const gid = chip.dataset.gid, name = chip.dataset.name;
    const others = [...box.querySelectorAll(".meeting")].filter((m) => m !== here);
    menu = document.createElement("div");
    menu.className = "suggest chip-menu";
    menu.innerHTML = `<span class="q">${esc(name)}</span>`
      + (gid ? `<button type="button" data-act="ungroup"><b>전체 해제</b><span class="sub">그룹 라벨만 떼고, 사람들은 그대로 (다른 그룹으로 다시 묶지 않음)</span></button>` : "")
      + others.map((m) => `<button type="button" data-act="move" data-to="${m.dataset.meeting}">→ ${esc(m.dataset.title)}(으)로 옮기기</button>`).join("")
      + `<button type="button" data-act="move" data-to="${box.querySelector(".new-meeting").dataset.meeting}">→ 새 미팅으로 옮기기</button>`
      + `<a href="/people/${gid || chip.dataset.pid}">${gid ? "그룹 페이지" : "프로필"} 열기</a>`;
    document.body.append(menu);
    menu.style.left = Math.max(8, Math.min(x, innerWidth - menu.offsetWidth - 8)) + "px";
    menu.style.top = Math.max(8, Math.min(y, innerHeight - menu.offsetHeight - 8)) + "px";
    menu.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-act]");
      if (!b) return;
      const ids = chip.dataset.ids.split(",");
      if (b.dataset.act === "ungroup") post(box, `/groups/${gid}/ungroup`, { ids: ids.filter((i) => i !== gid).join(",") });
      else post(box, "/move", { ids: ids.join(","), meeting: b.dataset.to });
      closeMenu();
    });
  }

  const chipAt = (t) => t.closest?.("#day-people .chips-people > li[data-ids]");

  // Desktop (and Android's long press): the context menu.
  document.addEventListener("contextmenu", (e) => {
    const chip = chipAt(e.target);
    if (!chip || chip.closest(".meeting.selecting")) return;
    e.preventDefault();
    openMenu(chip, e.clientX, e.clientY);
    if (touch) swallowClick = true;
  });

  // iPhone: no context menu on a long press, so time it (moving the finger means a drag instead).
  document.addEventListener("touchstart", (e) => {
    const chip = chipAt(e.target);
    if (!chip || e.touches.length > 1) return;
    const t = e.touches[0];
    pressAt = { x: t.clientX, y: t.clientY };
    clearTimeout(pressTimer);
    pressTimer = setTimeout(() => { swallowClick = true; openMenu(chip, pressAt.x, pressAt.y + 12); }, 550);
  }, { passive: true });
  document.addEventListener("touchmove", (e) => {
    const t = e.touches[0];
    if (pressAt && Math.hypot(t.clientX - pressAt.x, t.clientY - pressAt.y) > 8) clearTimeout(pressTimer);
  }, { passive: true });
  document.addEventListener("touchend", () => clearTimeout(pressTimer), { passive: true });
  // The tap that ends a long press doesn't also open the chip.
  document.addEventListener("click", (e) => {
    if (swallowClick && chipAt(e.target)) { e.preventDefault(); e.stopPropagation(); }
    swallowClick = false;
  }, true);

  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeMenu(); });
  document.addEventListener("pointerdown", (e) => { if (menu && !menu.contains(e.target)) closeMenu(); });
  addEventListener("scroll", closeMenu, { passive: true });
})();
