// Adding stocks to a bucket. The stock list is fetched once (the browser caches it for a day) and searched right here
// as you type. Tapping Add only puts a stock in a "Ready to add" list on this page; nothing is sent to the server until
// you tap Save, which sends them all in a single request.
(function () {
  var form = document.querySelector("[data-bucket-search]");
  if (!form) return;
  var input = form.querySelector("input[name=q]");
  var list = document.getElementById("bsearch-results");
  var empty = document.getElementById("bsearch-empty");
  var stage = document.getElementById("stage");
  var stageList = document.getElementById("stage-list");
  var stageForm = document.getElementById("stage-form");
  var stageSave = document.getElementById("stage-save");
  var stageCount = document.getElementById("stage-count");
  var room = parseInt(form.getAttribute("data-room"), 10);
  var have = {};
  (form.getAttribute("data-have") || "").split(" ").forEach(function (s) { if (s) have[s] = true; });
  var staged = [];            // [{s: symbol, n: name, d: detail line}] in the order they were tapped
  var items = null;

  document.documentElement.classList.add("js");           // hides the no-JavaScript Search button
  form.addEventListener("submit", function (e) { e.preventDefault(); });  // Enter must not reload the page

  function isStaged(sym) { return staged.some(function (x) { return x.s === sym; }); }
  function atCapacity() { return staged.length >= room; }

  function load() {
    if (items || load.started) return;
    load.started = true;
    fetch("/catalog.json").then(function (r) { return r.json(); }).then(function (rows) {
      items = rows.map(function (r) {
        return { s: r.s, n: r.n, e: !!r.e, g: r.g === "I" ? "India" : "US",
                 name: r.n.toLowerCase(), sym: r.s.toLowerCase(), hay: (r.n + " " + r.s + " " + (r.a || "")).toLowerCase() };
      });
      render();
    }).catch(function () { load.started = false; });
  }

  function rank(it, q) {            // lower is better: name or ticker starting with the text comes first
    if (it.sym.indexOf(q) === 0 || it.name.indexOf(q) === 0) return 0;
    if (it.name.indexOf(" " + q) !== -1) return 1;
    return 2;
  }

  function el(tag, cls, text) {
    var e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e;
  }

  function addForm(sym) {          // same shape as the server-rendered ones, so one handler covers both
    var f = el("form", "rm"); f.method = "post"; f.action = form.getAttribute("data-add-url");
    var h = el("input"); h.type = "hidden"; h.name = "symbol"; h.value = sym; f.appendChild(h);
    var b = el("button", "btn btn-primary btn-sm", "Add"); b.disabled = atCapacity(); f.appendChild(b);
    return f;
  }

  function row(it) {
    var li = el("li", "lrow"), main = el("div", "lmain");
    main.appendChild(el("span", "lname", it.n));
    main.appendChild(el("span", "lsub", it.s + " · " + it.g + (it.e ? " ETF" : "")));
    li.appendChild(main); li.appendChild(addForm(it.s));
    return li;
  }

  function render() {
    var q = input.value.toLowerCase().replace(/\s+/g, " ").trim();
    list.textContent = "";
    if (!q || !items) { list.hidden = true; empty.hidden = true; return; }
    var words = q.split(" ");
    function matches(it) { return words.every(function (w) { return it.hay.indexOf(w) !== -1; }); }
    var hits = items.filter(function (it) { return !have[it.s] && !isStaged(it.s) && matches(it); })
      .sort(function (a, b) { return rank(a, q) - rank(b, q) || a.name.localeCompare(b.name); }).slice(0, 8);
    hits.forEach(function (it) { list.appendChild(row(it)); });
    list.hidden = hits.length === 0;
    empty.hidden = hits.length !== 0;
    if (!hits.length) {
      var already = items.some(function (it) { return (have[it.s] || isStaged(it.s)) && matches(it); });
      empty.textContent = already ? "Already in this bucket." : "No match. Try part of the name or the ticker.";
    }
  }

  function renderStage() {
    stageList.textContent = "";
    staged.forEach(function (x) {
      var li = el("li", "lrow"), main = el("div", "lmain");
      main.appendChild(el("span", "lname", x.n)); main.appendChild(el("span", "lsub", x.d));
      var rm = el("button", "btn btn-ghost btn-sm", "Remove"); rm.type = "button";
      rm.setAttribute("aria-label", "Remove " + x.n + " from the list to add");
      rm.addEventListener("click", function () { unstage(x.s); });
      li.appendChild(main); li.appendChild(rm); stageList.appendChild(li);
    });
    stage.hidden = staged.length === 0;
    stageCount.textContent = staged.length ? "not saved yet" : "";
    stageSave.textContent = staged.length === 1 ? "Save 1 stock" : "Save " + staged.length + " stocks";
    // server-rendered suggestions: hide the staged ones, and switch Add off when there is no room left
    Array.prototype.forEach.call(document.querySelectorAll(".lrow form[action$='/add']"), function (f) {
      var sym = f.querySelector("input[name=symbol]").value, li = f.closest(".lrow");
      if (li && f !== stageForm) li.hidden = isStaged(sym);
      var b = f.querySelector("button"); if (b && f !== stageForm) b.disabled = atCapacity();
    });
    render();
  }

  function stageIt(f) {
    var sym = f.querySelector("input[name=symbol]").value;
    if (isStaged(sym) || have[sym] || atCapacity()) return;
    var li = f.closest(".lrow");
    var name = li && li.querySelector(".lname"), detail = li && li.querySelector(".lsub");
    staged.push({ s: sym, n: name ? name.textContent : sym, d: detail ? detail.textContent : sym });
    renderStage();
  }

  function unstage(sym) {
    staged = staged.filter(function (x) { return x.s !== sym; });
    renderStage();
  }

  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (f === stageForm) {          // the one real request: all the staged stocks together
      if (!staged.length) { e.preventDefault(); return; }
      Array.prototype.forEach.call(f.querySelectorAll("input[name=symbol]"), function (n) { n.remove(); });
      staged.forEach(function (x) {
        var h = el("input"); h.type = "hidden"; h.name = "symbol"; h.value = x.s; f.insertBefore(h, stageSave);
      });
      stageSave.disabled = true; stageSave.textContent = "Saving…";
      return;
    }
    if (f.getAttribute && /\/add$/.test(f.getAttribute("action") || "") && f.querySelector("input[name=symbol]")) {
      e.preventDefault();           // an Add button: keep it on the page for now
      stageIt(f);
    }
  });

  input.addEventListener("input", render);
  input.addEventListener("focus", load);
  load();                                                    // fetch it at once so the first keystroke already works
  if (input.value) render();
  renderStage();
})();
