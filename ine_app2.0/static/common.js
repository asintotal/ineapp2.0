(function () {
  const $ = id => document.getElementById(id);
  const meta = document.querySelector('meta[name="csrf-token"]');
  const CSRF = meta ? meta.content : "";

  function status(m, cls) {
    const s = $("status"); if (!s) return;
    s.textContent = m || ""; s.className = "note" + (s.dataset.centro ? " center" : "") + (cls ? " " + cls : "");
  }

  async function api(url, opts) {
    opts = opts || {}; opts.headers = Object.assign({ "X-CSRFToken": CSRF }, opts.headers);
    let r;
    try { r = await fetch(url, opts); }
    catch (e) {   // "Load failed" (Safari) / "Failed to fetch" (Chrome): la conexión se cortó sin respuesta
      throw new Error("No se pudo conectar con el servidor. Puede estar iniciándose o tardó demasiado; espera un momento e inténtalo de nuevo.");
    }
    if (r.status === 401) { location.href = "/login"; throw new Error("Sesión expirada"); }
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || (r.status === 413 ? "La imagen es demasiado grande." : "Error del servidor (" + r.status + ")"));
    return d;
  }

  // Reduce la foto en el navegador (lado mayor <= 2000 px, JPEG) antes de subirla:
  // sube menos datos y el servidor gasta menos memoria y tiempo. Si algo falla, se envía el original.
  const MAX_LADO = 2000;
  async function reducir(f) {
    let url = null;
    try {
      url = URL.createObjectURL(f);
      const img = new Image(); img.src = url; await img.decode();
      const w = img.naturalWidth, h = img.naturalHeight;
      const esc = Math.min(1, MAX_LADO / Math.max(w, h));
      if (esc === 1 && f.type === "image/jpeg" && f.size <= 1.5 * 1024 * 1024) return f;   // ya es ligera
      const c = document.createElement("canvas");
      c.width = Math.max(1, Math.round(w * esc)); c.height = Math.max(1, Math.round(h * esc));
      c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
      const blob = await new Promise(res => c.toBlob(res, "image/jpeg", 0.9));
      c.width = c.height = 0;   // libera memoria del canvas (importante en iOS)
      return blob && (esc < 1 || blob.size < f.size) ? blob : f;
    } catch (e) { return f; }
    finally { if (url) URL.revokeObjectURL(url); }
  }

  window.App = { $, status, api, reducir };
})();
