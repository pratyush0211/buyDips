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
