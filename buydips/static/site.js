// Small helpers shared by every page. No inline scripts: the site's CSP only allows files from itself.
document.addEventListener("submit", function (e) {
  var msg = e.target.getAttribute("data-confirm");
  if (msg && !window.confirm(msg)) e.preventDefault();
});
