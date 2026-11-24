// Instant search for adding stocks to a bucket. The stock list is fetched once (the browser caches it for a day) and
// filtered right here as you type, so nothing is sent to the server until you tap Add.
(function () {
  var form = document.querySelector("[data-bucket-search]");
  if (!form) return;
  var input = form.querySelector("input[name=q]");
  var list = document.getElementById("bsearch-results");
  var empty = document.getElementById("bsearch-empty");
  var have = {};
  (form.getAttribute("data-have") || "").split(" ").forEach(function (s) { if (s) have[s] = true; });
  var items = null;

  document.documentElement.classList.add("js");           // hides the no-JavaScript Search button
  form.addEventListener("submit", function (e) { e.preventDefault(); });  // Enter must not reload the page

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

  function row(it) {
    var li = document.createElement("li"); li.className = "lrow";
    var main = document.createElement("div"); main.className = "lmain";
    var name = document.createElement("span"); name.className = "lname"; name.textContent = it.n;
    var sub = document.createElement("span"); sub.className = "lsub"; sub.textContent = it.s + " · " + it.g + (it.e ? " ETF" : "");
    main.appendChild(name); main.appendChild(sub);
    var f = document.createElement("form"); f.method = "post"; f.action = form.getAttribute("data-add-url"); f.className = "rm";
    [["csrf", form.getAttribute("data-csrf")], ["symbol", it.s]].forEach(function (p) {
      var h = document.createElement("input"); h.type = "hidden"; h.name = p[0]; h.value = p[1]; f.appendChild(h);
    });
    var b = document.createElement("button"); b.className = "btn btn-primary btn-sm"; b.textContent = "Add"; f.appendChild(b);
    f.addEventListener("submit", function () { b.disabled = true; b.textContent = "Adding…"; setTimeout(function () { f.querySelector("button").disabled = true; }, 0); });
    li.appendChild(main); li.appendChild(f);
    return li;
  }

  function render() {
    var q = input.value.toLowerCase().replace(/\s+/g, " ").trim();
    list.textContent = "";
    if (!q || !items) { list.hidden = true; empty.hidden = true; return; }
    var words = q.split(" ");
    var hits = items.filter(function (it) {
      return !have[it.s] && words.every(function (w) { return it.hay.indexOf(w) !== -1; });
    }).sort(function (a, b) { return rank(a, q) - rank(b, q) || a.name.localeCompare(b.name); }).slice(0, 8);
    hits.forEach(function (it) { list.appendChild(row(it)); });
    list.hidden = hits.length === 0;
    empty.hidden = hits.length !== 0;
    if (!hits.length) {
      var inBucket = items.some(function (it) {
        return have[it.s] && words.every(function (w) { return it.hay.indexOf(w) !== -1; });
      });
      empty.textContent = inBucket ? "Already in this bucket." : "No match. Try part of the name or the ticker.";
    }
  }

  input.addEventListener("input", render);
  input.addEventListener("focus", load);
  load();                                                    // fetch it at once so the first keystroke already works
  if (input.value) render();
})();
