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
  var current = "indexes";

  function box(item) { return item.querySelector("input.switch"); }
  function num(item) { return item.querySelector("input[type=number]"); }

  function refresh() {
    var n = 0;
    items.forEach(function (it) {
      var on = box(it).checked;
      it.classList.toggle("is-on", on);
      num(it).disabled = !on;
      if (on) n++;
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
  }

  form.addEventListener("change", function (e) { if (e.target.classList.contains("switch")) refresh(); });

  form.addEventListener("click", function (e) {
    var step = e.target.closest(".stepper");
    if (step) {
      var input = num(step.closest(".item"));
      var v = parseFloat(input.value) || 1;
      v = Math.min(MAX, Math.max(MIN, Math.round((v + parseFloat(step.dataset.step)) * 10) / 10));
      input.value = String(v);
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
    tabs.forEach(function (t) { t.setAttribute("aria-selected", q === "" && t.dataset.tab === current ? "true" : "false"); });
    note.hidden = q === "";
    note.textContent = q === "" ? "" : shown + (shown === 1 ? " match" : " matches") + " across all lists for \u201c" + filterEl.value.trim() + "\u201d";
    empty.hidden = shown !== 0;
    if (shown === 0) {
      empty.textContent = q !== "" ? "Nothing matches \u201c" + filterEl.value.trim() + "\u201d. Try a shorter name or the ticker."
                                   : "Nothing selected yet. Switch on items in the other tabs, or search for one.";
    }
  }

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
  setTab(items.some(function (it) { return box(it).checked; }) ? "selected" : "indexes");
})();
