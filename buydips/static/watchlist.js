(function () {
  var form = document.getElementById("wl");
  if (!form) return;
  var LIMIT = parseInt(form.dataset.limit, 10);
  var MIN = 0.5, MAX = 20;
  var items = Array.prototype.slice.call(form.querySelectorAll(".item"));
  var countEl = document.getElementById("count");
  var saveBtn = document.getElementById("save");

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
    }
  });

  document.getElementById("filter").addEventListener("input", function (e) {
    var q = e.target.value.trim().toLowerCase();
    items.forEach(function (it) { it.hidden = q !== "" && it.dataset.name.indexOf(q) === -1; });
    document.querySelectorAll(".group").forEach(function (g) {
      g.hidden = g.querySelectorAll(".item:not([hidden])").length === 0;
    });
  });

  refresh();
})();
