(function () {
  var form = document.getElementById("wl");
  if (!form) return;
  var LIMIT = parseInt(form.dataset.limit, 10);
  var MIN = 0.5, MAX = 20;
  var items = Array.prototype.slice.call(form.querySelectorAll(".item"));
  var countEl = document.getElementById("count");
  var saveBtn = document.getElementById("save");
  var tabs = Array.prototype.slice.call(form.querySelectorAll(".tab"));
  var groups = Array.prototype.slice.call(form.querySelectorAll(".group"));
  var filterEl = document.getElementById("filter");
  var selTotal = document.getElementById("sel-total");
  var note = document.getElementById("view-note");
  var empty = document.getElementById("empty");
  var current = "india";
  var savebar = form.querySelector(".savebar");
  var clearAll = document.getElementById("clearall");
  var initiallyOn = items.filter(function (it) { return it.querySelector("input.switch").checked; }).length;
  var jump = document.getElementById("jump");
  var navbar = form.querySelector(".navbar");
  items.forEach(function (it) {  // first letter of each name; digits (like "360 ONE") go under "#"
    var c = it.querySelector(".item-name").textContent.trim().charAt(0).toUpperCase();
    it.dataset.initial = /[A-Z]/.test(c) ? c : "#";
  });

  function box(item) { return item.querySelector("input.switch"); }
  function num(item) { return item.querySelector("input[type=number]"); }

  function syncChip(it) {   // the collapsed "Alert -3%" button always shows the number in the field
    var v = it.querySelector(".thr-val");
    if (v) v.textContent = String(parseFloat(num(it).value) || "");
  }

  function refresh() {
    var n = 0;
    items.forEach(function (it) {
      var on = box(it).checked;
      it.classList.toggle("is-on", on);
      num(it).disabled = !on;
      if (on) { n++; syncChip(it); } else { it.classList.remove("open"); }
    });
    countEl.textContent = n + " of " + LIMIT + " selected";
    countEl.classList.toggle("over", n > LIMIT);
    saveBtn.disabled = n > LIMIT;
    document.querySelectorAll(".group").forEach(function (g) {
      var c = g.querySelectorAll(".item.is-on").length;
      var badge = g.querySelector(".n");
      if (badge) badge.textContent = c ? c + " selected" : badge.dataset.total + " available";
    });
    tabs.forEach(function (t) {
      var badge = t.querySelector(".tab-sel");
      if (!badge) return;
      var c = form.querySelectorAll('.group[data-tab="' + t.dataset.tab + '"] .item.is-on').length;
      badge.textContent = c;
      badge.hidden = c === 0;
    });
    selTotal.textContent = n ? String(n) : "";
    clearAll.hidden = !(n > 0 && current === "selected" && filterEl.value.trim() === "");   // only where you review your picks
    if (savebar) savebar.hidden = n === 0 && initiallyOn === 0;   // nothing to save yet: keep the screen clear
  }

  form.addEventListener("change", function (e) { if (e.target.classList.contains("switch")) refresh(); });
  form.addEventListener("input", function (e) { if (e.target.type === "number") syncChip(e.target.closest(".item")); });

  form.addEventListener("click", function (e) {
    var step = e.target.closest(".stepper");
    if (step) {
      var input = num(step.closest(".item"));
      var v = parseFloat(input.value) || 1;
      v = Math.min(MAX, Math.max(MIN, Math.round((v + parseFloat(step.dataset.step)) * 10) / 10));
      input.value = String(v);
      syncChip(step.closest(".item"));
      return;
    }
    var chip = e.target.closest(".thr-chip");
    if (chip) {   // phones: the stepper is tucked away until you tap the level
      var row = chip.closest(".item");
      var open = row.classList.toggle("open");
      chip.setAttribute("aria-expanded", open ? "true" : "false");
      return;
    }
    var preset = e.target.closest("[data-symbols]");
    if (preset) {
      var wanted = preset.dataset.symbols ? preset.dataset.symbols.split("|") : [];
      items.forEach(function (it) { box(it).checked = wanted.indexOf(box(it).value) !== -1; });
      refresh();
      setTab("selected");  // show what the preset just picked
    }
  });

  // Which items are visible: while searching, everything is searched; otherwise the chosen tab.
  function applyView() {
    var q = filterEl.value.trim().toLowerCase();
    var shown = 0;
    groups.forEach(function (g) {
      var inTab = q !== "" || current === "selected" || g.dataset.tab === current;
      var any = 0;
      Array.prototype.forEach.call(g.querySelectorAll(".item"), function (it) {
        var show = inTab && (q !== "" ? it.dataset.name.indexOf(q) !== -1
                                       : current !== "selected" || it.dataset.pinned === "1");
        it.hidden = !show;
        if (show) any++;
      });
      g.hidden = any === 0;
      shown += any;
    });
    form.classList.toggle("show-heads", q !== "" || current === "selected");   // list headings only where lists are mixed
    clearAll.hidden = !(current === "selected" && q === "" && form.querySelectorAll(".item.is-on").length > 0);
    tabs.forEach(function (t) { t.setAttribute("aria-selected", q === "" && t.dataset.tab === current ? "true" : "false"); });
    buildJump(q === "");
    var active = tabs.filter(function (t) { return t.getAttribute("aria-selected") === "true"; })[0];
    if (active) {  // on a narrow screen, bring the chosen tab into view inside the tab bar
      var bar = active.parentElement;
      bar.scrollTo({ left: Math.max(0, active.offsetLeft - (bar.clientWidth - active.offsetWidth) / 2), behavior: reduceMotion() ? "auto" : "smooth" });
    }
    note.hidden = q === "";
    note.textContent = q === "" ? "" : shown + (shown === 1 ? " match" : " matches") + " across all lists for \u201c" + filterEl.value.trim() + "\u201d";
    empty.hidden = shown !== 0;
    if (shown === 0) {
      empty.textContent = q !== "" ? "Nothing matches \u201c" + filterEl.value.trim() + "\u201d. Try a shorter name or the ticker."
                                   : "Nothing selected yet. Switch on items in the other tabs, or search for one.";
    }
  }

  // The A-Z bar appears on the long stock tabs and only enables letters that have something under them.
  function buildJump(on) {
    var long = on && (current === "india" || current === "us");
    jump.hidden = !long;
    if (!long) { jump.textContent = ""; return; }
    var have = {};
    items.forEach(function (it) { if (it.closest(".group").dataset.tab === current) have[it.dataset.initial] = true; });
    jump.textContent = "";
    "#ABCDEFGHIJKLMNOPQRSTUVWXYZ".split("").forEach(function (L) {
      if (L === "#" && !have["#"]) return;
      var b = document.createElement("button");
      b.type = "button"; b.textContent = L; b.dataset.letter = L; b.disabled = !have[L];
      b.setAttribute("aria-label", "Jump to names starting with " + L);
      jump.appendChild(b);
    });
  }

  function reduceMotion() {
    return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  jump.addEventListener("click", function (e) {
    var b = e.target.closest("button[data-letter]");
    if (!b || b.disabled) return;
    var target = items.filter(function (it) {
      return !it.hidden && it.dataset.initial === b.dataset.letter;
    })[0];
    if (target) window.scrollTo({ top: window.pageYOffset + target.getBoundingClientRect().top - navbar.offsetHeight - 6, behavior: reduceMotion() ? "auto" : "smooth" });
  });

  function setTab(name) {
    current = name;
    filterEl.value = "";
    if (name === "selected") {  // freeze the list so items don't vanish while you untick them
      items.forEach(function (it) { it.dataset.pinned = box(it).checked ? "1" : "0"; });
    }
    applyView();
  }

  tabs.forEach(function (t) { t.addEventListener("click", function () { setTab(t.dataset.tab); }); });
  filterEl.addEventListener("input", applyView);

  refresh();
  setTab(initiallyOn ? "selected" : (form.dataset.start || "india"));
})();
