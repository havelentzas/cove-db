/* Graph pages (strict CSP: data comes from <script type="application/json"> blocks, not inline code).
   #graph       : full graph page, controls #g-*, data in #graph-data, focus in data-focus
   #local-graph : sidebar local graph on a note, data in #local-graph-data, focus in data-focus */
(function () {
  "use strict";
  function json(id) { var el = document.getElementById(id); return el ? JSON.parse(el.textContent) : null; }
  function open(n) { if (n.url) location.href = n.url; }

  document.addEventListener("DOMContentLoaded", function () {
    var local = document.getElementById("local-graph");
    if (local) {
      var ld = json("local-graph-data");
      if (!ld || ld.nodes.length < 2) local.innerHTML = '<p class="small muted" style="padding:12px">No links yet.</p>';
      else window.coveGraph(local, ld, { focus: local.dataset.focus, compact: true, preTicks: 200, onOpen: open });
    }

    var el = document.getElementById("graph");
    if (!el) return;
    var $ = function (id) { return document.getElementById(id); };
    var focus = el.dataset.focus || null, g = null;
    var toggles = ["tags", "attachments", "unresolved", "orphans"];
    function draw(data) {
      if (g) g.destroy();
      g = window.coveGraph(el, data, { focus: focus, preTicks: 300, onOpen: open });
      $("g-stats").textContent = g.stats.nodes + " nodes · " + g.stats.links + " links · scroll to zoom, drag to move, click to open";
      if ($("g-search").value) g.search($("g-search").value);
    }
    function load() {
      var p = new URLSearchParams();
      if (focus) { p.set("focus", focus); p.set("depth", $("g-depth").value); }
      toggles.forEach(function (k) { p.set(k, $("g-" + k).checked ? "1" : "0"); });
      fetch("/notes/graph.json?" + p).then(function (r) { return r.json(); }).then(draw);
    }
    toggles.forEach(function (k) { $("g-" + k).addEventListener("change", load); });
    if (focus) $("g-depth").addEventListener("change", load);
    $("g-fit").addEventListener("click", function () { if (g) g.fit(); });
    $("g-search").addEventListener("input", function (e) {
      if (!g) return; var hits = g.search(e.target.value); if (hits.length === 1) g.focusNode(hits[0]);
    });
    draw(json("graph-data"));
  });
})();
