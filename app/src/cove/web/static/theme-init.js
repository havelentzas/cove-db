// Apply the saved light/dark choice before first paint (loaded synchronously in <head>).
try { var t = localStorage.getItem("cove-theme"); if (t) document.documentElement.dataset.theme = t; } catch (e) {}
