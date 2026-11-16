/* cove graph view: an Obsidian-style force-directed graph of notes and their links.
   Canvas rendering (fast with thousands of nodes), d3-force for layout, d3-zoom/d3-drag for interaction.
   Colours come from the brand tokens (cove.css), so light/dark themes just work.

   coveGraph(container, data, { focus, labels: "auto"|"always", onOpen, compact })
   data = { nodes: [{id, kind, title, path, degree}], links: [{source, target, kind}] }
*/
(function () {
  "use strict";

  function tokens(el) {
    const cs = getComputedStyle(el);
    const v = n => cs.getPropertyValue(n).trim();
    return { ink: v("--ink"), muted: v("--muted"), line: v("--line"), panel: v("--panel"), bg: v("--bg"),
             signal: v("--signal"), accent: v("--accent"), font: v("--font-ui") || "sans-serif" };
  }

  function coveGraph(container, data, opts) {
    opts = opts || {};
    const d3 = window.d3;
    const canvas = document.createElement("canvas");
    canvas.style.display = "block";
    canvas.style.width = "100%";
    canvas.style.height = "100%";
    canvas.style.cursor = "grab";
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", `Graph of ${data.nodes.length} notes and ${data.links.length} links`);
    container.innerHTML = "";
    container.appendChild(canvas);
    const ctx = canvas.getContext("2d");

    let T = tokens(container);
    let W = 0, H = 0, dpr = 1;
    let transform = d3.zoomIdentity;
    let hover = null;
    let query = "";
    const focusId = opts.focus || null;

    const nodes = data.nodes.map(n => Object.assign({}, n));
    const byId = new Map(nodes.map(n => [n.id, n]));
    const links = data.links.filter(l => byId.has(l.source) && byId.has(l.target)).map(l => Object.assign({}, l));
    const neighbours = new Map(nodes.map(n => [n.id, new Set()]));
    links.forEach(l => { neighbours.get(l.source).add(l.target); neighbours.get(l.target).add(l.source); });
    const radius = n => (n.kind === "tag" ? 3 : n.kind === "unresolved" ? 2.5 : 3) + Math.sqrt(n.degree || 0) * (opts.compact ? 1.2 : 1.6)
      + (n.id === focusId ? 3 : 0);

    // ---- layout
    const sim = d3.forceSimulation(nodes)
      .force("link", d3.forceLink(links).id(d => d.id).distance(opts.compact ? 30 : 45).strength(0.35))
      .force("charge", d3.forceManyBody().strength(opts.compact ? -90 : -90).distanceMax(opts.compact ? 300 : 500))
      .force("x", d3.forceX(0).strength(0.04))
      .force("y", d3.forceY(0).strength(0.04))
      .force("collide", d3.forceCollide(d => radius(d) + 2))
      .alphaDecay(0.03)
      .on("tick", draw);
    if (focusId && byId.has(focusId)) { const f = byId.get(focusId); f.fx = 0; f.fy = 0; }

    function resize() {
      const r = container.getBoundingClientRect();
      dpr = window.devicePixelRatio || 1;
      W = Math.max(50, r.width); H = Math.max(50, r.height);
      canvas.width = W * dpr; canvas.height = H * dpr;
      draw();
    }

    // ---- interaction
    const zoom = d3.zoom().scaleExtent([0.1, 6]).on("zoom", e => { transform = e.transform; draw(); });
    const sel = d3.select(canvas).call(zoom);
    sel.on("dblclick.zoom", null);

    function toGraph(evt) {
      const r = canvas.getBoundingClientRect();
      const [x, y] = transform.invert([evt.clientX - r.left, evt.clientY - r.top]);
      return [x - W / 2, y - H / 2];
    }
    function nodeAt(evt) {
      const [x, y] = toGraph(evt);
      const n = sim.find(x, y, 14 / transform.k);
      return n && Math.hypot(n.x - x, n.y - y) <= radius(n) + 6 / transform.k ? n : null;
    }

    sel.call(d3.drag()
      .container(canvas)
      .subject(evt => nodeAt(evt.sourceEvent))
      .on("start", e => { if (!e.active) sim.alphaTarget(0.25).restart(); e.subject.fx = e.subject.x; e.subject.fy = e.subject.y; canvas.style.cursor = "grabbing"; })
      .on("drag", e => { const [x, y] = toGraph(e.sourceEvent); e.subject.fx = x; e.subject.fy = y; })
      .on("end", e => { if (!e.active) sim.alphaTarget(0); if (e.subject.id !== focusId) { e.subject.fx = null; e.subject.fy = null; } canvas.style.cursor = "grab"; }));

    canvas.addEventListener("mousemove", evt => {
      const n = nodeAt(evt);
      if (n !== hover) { hover = n; canvas.style.cursor = n ? "pointer" : "grab"; draw(); }
    });
    canvas.addEventListener("mouseleave", () => { hover = null; draw(); });
    canvas.addEventListener("click", evt => {
      const n = nodeAt(evt);
      if (n && n.url !== null && opts.onOpen) opts.onOpen(n);
    });

    // ---- drawing
    function matches(n) { return query && (n.title || "").toLowerCase().includes(query); }

    function draw() {
      if (!W) return;
      const lit = hover ? neighbours.get(hover.id) : null;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, W, H);
      ctx.translate(transform.x, transform.y);
      ctx.scale(transform.k, transform.k);
      ctx.translate(W / 2, H / 2);

      // edges
      ctx.lineWidth = 1 / transform.k;
      for (const l of links) {
        const on = hover && (l.source === hover || l.target === hover);
        ctx.strokeStyle = on ? T.signal : T.line;
        ctx.globalAlpha = hover && !on ? 0.25 : l.kind === "tag" || l.kind === "unresolved" ? 0.55 : 0.9;
        ctx.setLineDash(l.kind === "unresolved" ? [3 / transform.k, 3 / transform.k] : []);
        ctx.lineWidth = (on ? 1.6 : 1) / transform.k;
        ctx.beginPath(); ctx.moveTo(l.source.x, l.source.y); ctx.lineTo(l.target.x, l.target.y); ctx.stroke();
      }
      ctx.setLineDash([]);

      // nodes
      for (const n of nodes) {
        const r = radius(n);
        const active = n === hover || n.id === focusId || matches(n);
        const near = lit && lit.has(n.id);
        ctx.globalAlpha = hover && !active && !near ? 0.3 : 1;
        ctx.beginPath();
        if (n.kind === "attachment") ctx.rect(n.x - r, n.y - r, r * 2, r * 2);
        else ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
        if (n.kind === "unresolved") {
          ctx.lineWidth = 1.2 / transform.k; ctx.strokeStyle = T.muted; ctx.stroke();
        } else if (n.kind === "tag") {
          ctx.fillStyle = T.panel; ctx.fill(); ctx.lineWidth = 1.5 / transform.k; ctx.strokeStyle = T.accent; ctx.stroke();
        } else {
          ctx.fillStyle = active ? T.signal : near ? T.accent : n.kind === "attachment" ? T.muted : T.ink;
          ctx.fill();
          if (active) { ctx.lineWidth = 1.5 / transform.k; ctx.strokeStyle = T.ink; ctx.stroke(); }
        }
      }

      // labels: when zoomed in, or for hovered/neighbour/focus/search hits
      const showAll = opts.labels === "always" || transform.k >= (opts.compact ? 1.6 : 1.6);
      ctx.font = `${12 / transform.k}px ${T.font}`;
      ctx.textAlign = "center"; ctx.textBaseline = "top";
      for (const n of nodes) {
        const important = n === hover || n.id === focusId || matches(n) || (lit && lit.has(n.id));
        if (!showAll && !important) continue;
        if (hover && !important) continue;
        ctx.globalAlpha = important ? 1 : 0.75;
        const label = n.title.length > 40 ? n.title.slice(0, 38) + "…" : n.title;
        const y = n.y + radius(n) + 3 / transform.k;
        ctx.lineWidth = 3 / transform.k; ctx.strokeStyle = T.bg; ctx.strokeText(label, n.x, y);
        ctx.fillStyle = n.kind === "unresolved" ? T.muted : T.ink; ctx.fillText(label, n.x, y);
      }
      ctx.globalAlpha = 1;
    }

    function fit(immediate) {
      if (!nodes.length) return;
      const xs = nodes.map(n => n.x), ys = nodes.map(n => n.y);
      const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
      const k = Math.min(opts.compact ? 1.4 : 3, 0.9 / Math.max((x1 - x0 + 40) / W, (y1 - y0 + 40) / H));
      const cx = (x0 + x1) / 2 + W / 2, cy = (y0 + y1) / 2 + H / 2;
      const t = d3.zoomIdentity.translate(W / 2, H / 2).scale(k).translate(-cx, -cy);
      if (immediate) sel.call(zoom.transform, t); else sel.transition().duration(400).call(zoom.transform, t);
    }
    if (!opts.preTicks) sim.on("end", () => fit());

    const ro = new ResizeObserver(resize); ro.observe(container);
    const retheme = () => { T = tokens(container); draw(); };
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", retheme);
    new MutationObserver(retheme).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    resize();
    if (opts.preTicks) {
      // settle the layout before the first paint (no tangled start), then keep it gently alive for dragging
      sim.stop();
      sim.tick(opts.preTicks);
      fit(true);
      sim.alpha(0.02).restart();
    }

    return {
      search(q) { query = (q || "").trim().toLowerCase(); draw(); return nodes.filter(matches); },
      fit,
      focusNode(n) {
        const k = Math.max(transform.k, 1.8);
        sel.transition().duration(500).call(zoom.transform, d3.zoomIdentity.translate(W / 2, H / 2).scale(k).translate(-(n.x + W / 2), -(n.y + H / 2)));
      },
      destroy() { sim.stop(); ro.disconnect(); },
      stats: { nodes: nodes.length, links: links.length },
    };
  }

  window.coveGraph = coveGraph;
})();
