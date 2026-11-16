/* cove UI behaviours. No inline JavaScript anywhere (strict CSP): templates declare intent with
   data-* attributes and this file wires them up with delegated listeners.
     data-theme-toggle            light/dark switch
     data-open="dialog-id"        open a <dialog class="sheet"> (and deep-link it as #dialog-id)
     data-copy="text" | data-copy-from="element-id"   copy to clipboard
     data-confirm="message"       on <form>: ask before submitting
     data-no-submit               on <form>: never submit (JS-only controls)
     data-check="all|none" data-target="css"          tick/untick checkboxes
     data-add-row="map" / data-remove-row             repeating rows in the contact form
*/
(function () {
  "use strict";

  function toggleTheme() {
    var r = document.documentElement;
    var dark = r.dataset.theme ? r.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    r.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("cove-theme", r.dataset.theme); } catch (e) {}
  }

  function copy(btn, text) {
    function done() { var t = btn.textContent; btn.textContent = "Copied"; setTimeout(function () { btn.textContent = t; }, 1200); }
    if (navigator.clipboard && window.isSecureContext) { navigator.clipboard.writeText(text).then(done); return; }
    var ta = document.createElement("textarea"); ta.value = text; document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); done(); } catch (e) {} ta.remove();
  }

  var rowCounter = 1000;
  function addRow(rep) {
    var tpl = document.getElementById("tpl-" + rep), rows = document.getElementById("rows-" + rep);
    if (tpl && rows) rows.insertAdjacentHTML("beforeend", tpl.innerHTML.split("__IDX__").join(String(rowCounter++)));
  }

  document.addEventListener("click", function (e) {
    var el;
    if ((el = e.target.closest("[data-theme-toggle]"))) { toggleTheme(); return; }
    if ((el = e.target.closest("[data-open]"))) {
      var d = document.getElementById(el.dataset.open);
      if (d && d.showModal) { d.showModal(); history.replaceState(null, "", "#" + d.id); }
      return;
    }
    if ((el = e.target.closest("[data-copy], [data-copy-from]"))) {
      var src = el.dataset.copyFrom ? document.getElementById(el.dataset.copyFrom) : null;
      copy(el, src ? src.textContent : el.dataset.copy);
      return;
    }
    if ((el = e.target.closest("[data-check]"))) {
      document.querySelectorAll(el.dataset.target).forEach(function (x) { if (!x.disabled) x.checked = el.dataset.check === "all"; });
      return;
    }
    if ((el = e.target.closest("[data-add-row]"))) { addRow(el.dataset.addRow); return; }
    if ((el = e.target.closest("[data-remove-row]"))) { var row = el.closest(".rrow"); if (row) row.remove(); }
  });

  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (f.hasAttribute("data-no-submit")) { e.preventDefault(); return; }
    if (f.dataset.confirm && !window.confirm(f.dataset.confirm)) e.preventDefault();
  });

  document.addEventListener("DOMContentLoaded", function () {
    // dialogs: click the backdrop to close; drop the #hash when closed; open from a deep link
    document.querySelectorAll("dialog.sheet").forEach(function (d) {
      d.addEventListener("click", function (e) { if (e.target === d) d.close(); });
      d.addEventListener("close", function () { if (location.hash === "#" + d.id) history.replaceState(null, "", location.pathname + location.search); });
    });
    var deep = location.hash && document.getElementById(location.hash.slice(1));
    if (deep && deep.tagName === "DIALOG") deep.showModal();
    // vault tree: keep the current item in view; start collapsed on phones
    var cur = document.querySelector(".tree .current"); if (cur) cur.scrollIntoView({ block: "center" });
    var vt = document.querySelector(".vault-tree");
    if (vt && window.matchMedia("(max-width: 900px)").matches) vt.removeAttribute("open");
  });
})();
