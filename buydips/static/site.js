// Small helpers shared by every page. No inline scripts: the site's CSP only allows files from itself.
document.addEventListener("submit", function (e) {
  var msg = e.target.getAttribute("data-confirm");
  if (msg && !window.confirm(msg)) e.preventDefault();
});

// Scroll reveals and count-up numbers. Only things below the fold are hidden (so nothing flashes),
// and people who prefer reduced motion, or browsers without IntersectionObserver, just see the finished page.
(function () {
  var calm = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var els = Array.prototype.slice.call(document.querySelectorAll(".reveal"));
  if (!els.length || calm || !("IntersectionObserver" in window)) return;

  function countUp(box) {
    var n = box.querySelector("[data-count]");
    if (!n) return;
    var end = parseInt(n.getAttribute("data-count"), 10), t0 = null;
    if (!end) return;
    function tick(t) {
      if (t0 === null) t0 = t;
      var p = Math.min(1, (t - t0) / 1100);
      n.textContent = String(Math.round(end * (1 - Math.pow(1 - p, 3))));
      if (p < 1) requestAnimationFrame(tick);
    }
    n.textContent = "0";
    requestAnimationFrame(tick);
  }

  var line = window.innerHeight * 0.92;
  els.forEach(function (el) { if (el.getBoundingClientRect().top > line) el.classList.add("pre"); });
  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      if (!e.isIntersecting) return;
      e.target.classList.remove("pre");
      countUp(e.target);
      io.unobserve(e.target);
    });
  }, { threshold: 0.18 });
  els.forEach(function (el) { io.observe(el); });
})();

// Settings switches save as soon as they're flipped.
document.addEventListener("change", function (e) {
  var t = e.target;
  if (t && t.hasAttribute && t.hasAttribute("data-autosubmit") && t.form) t.form.submit();
});

// Sharing: the phone's own share sheet (WhatsApp, Instagram, Messages...) with the card image attached when the
// browser allows it, otherwise the link alone, otherwise copy the link. Safari only lets share() run inside the tap
// itself, so the image is fetched when the page loads and the tap shares it without waiting for anything.
(function () {
  var file = null;
  function say(msg) {
    var el = document.querySelector("[data-share-status]");
    if (el) el.textContent = msg;
  }
  function copy(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text).then(function () { say("Link copied."); }, function () { say("Press and hold the link to copy it."); });
    }
    var box = document.getElementById("sharelink");
    if (box) { box.select(); try { document.execCommand("copy"); say("Link copied."); } catch (e) { say("Press and hold the link to copy it."); } }
  }
  var btn = document.querySelector("[data-share]");
  if (btn && window.fetch && window.File) {
    fetch(btn.getAttribute("data-image")).then(function (r) { return r.ok ? r.blob() : null; }).then(function (blob) {
      if (blob) file = new File([blob], "buydips-bucket.png", { type: "image/png" });
    }).catch(function () {});
  }
  document.addEventListener("click", function (e) {
    var c = e.target.closest && e.target.closest("[data-copy]");
    if (c) { copy(c.getAttribute("data-copy")); return; }
    var b = e.target.closest && e.target.closest("[data-share]");
    if (!b) return;
    var url = b.getAttribute("data-url"), title = b.getAttribute("data-title"), text = title + " on buyDips";
    if (!navigator.share) { copy(url); return; }
    var withImage = file && navigator.canShare && navigator.canShare({ files: [file] });
    var p = withImage ? navigator.share({ files: [file], title: title, text: text + " " + url })
                      : navigator.share({ title: title, text: text, url: url });
    p.catch(function (err) {
      if (err && err.name === "AbortError") return;
      if (withImage) { navigator.share({ title: title, text: text, url: url }).catch(function (e2) { if (!e2 || e2.name !== "AbortError") copy(url); }); }
      else copy(url);
    });
  });
})();
