// Show the name of the chosen holdings file (the real file input is visually hidden to keep the big tap target).
(function () {
  var input = document.getElementById("file");
  if (!input) return;
  var label = document.querySelector(".filepick .bc-t");
  var original = label ? label.textContent : "";
  input.addEventListener("change", function () {
    if (label) label.textContent = input.files && input.files[0] ? input.files[0].name : original;
  });
})();
