(function () {
  const $ = id => document.getElementById(id);
  const ADMIN = document.currentScript.dataset.admin === "1";
  const CSRF = document.querySelector('meta[name="csrf-token"]').content;
  let file = null, busy = false;

  const status = (m, cls) => { const s = $("status"); s.textContent = m || ""; s.className = "note center" + (cls ? " " + cls : ""); };

  function pick(f) {
    if (!f || !f.type.startsWith("image/")) { status("Selecciona un archivo de imagen."); return; }
    file = f; $("prev").src = URL.createObjectURL(f);
    $("prevBox").hidden = false; $("drop").hidden = true; $("resCard").hidden = true; status("");
  }
  function reset() { file = null; $("file").value = ""; $("prevBox").hidden = true; $("drop").hidden = false; status(""); }

  const drop = $("drop");
  drop.onclick = () => $("file").click();
  drop.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("file").click(); } };
  drop.ondragover = e => { e.preventDefault(); drop.classList.add("over"); };
  drop.ondragleave = () => drop.classList.remove("over");
  drop.ondrop = e => { e.preventDefault(); drop.classList.remove("over"); pick(e.dataTransfer.files[0]); };
  $("file").onchange = e => pick(e.target.files[0]);
  $("clear").onclick = reset;

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

  $("go").onclick = async () => {
    if (busy || !file) return;
    busy = true; $("go").disabled = true; status("Preparando imagen…");
    try {
      const envio = await reducir(file);
      status("Leyendo credencial… (puede tardar unos segundos)");
      const fd = new FormData(); fd.append("imagen", envio, "credencial.jpg");
      const r = await api("/api/extraer", { method: "POST", body: fd });
      $("rNum").textContent = r.numero_consulta;
      $("rFh").textContent = r.fecha + " " + r.hora;
      $("rNom").textContent = r.nombre || "(no detectado)";
      $("rCurp").textContent = r.curp || "(no detectado)";
      $("rCurp").className = "mono" + (r.curp ? (r.curp_valido ? " ok" : " wa") : "");
      $("rVig").textContent = r.vigencia || "(no detectada)";
      const w = [];
      if (r.curp) w.push(r.curp_valido ? "✔ El CURP cumple la estructura oficial." : "⚠ El CURP no cumple la estructura oficial; revísalo contra la credencial.");
      if (!(r.nombre && r.curp && r.vigencia)) w.push("⚠ Algún dato no se pudo leer. Prueba con una foto más nítida, de frente y bien iluminada.");
      $("rWarn").textContent = w.join(" ");
      $("resCard").hidden = false; status(""); load();
    } catch (e) { status(e.message, "er"); }
    finally { busy = false; $("go").disabled = false; }
  };

  function td(tr, t, cls) { const c = document.createElement("td"); c.textContent = t; if (cls) c.className = cls; tr.appendChild(c); return c; }

  async function load() {
    try {
      const rows = await api("/api/consultas");
      $("hEmpty").hidden = rows.length > 0; $("hEmpty").textContent = "Aún no hay consultas.";
      const tb = $("hist"); tb.textContent = "";
      rows.forEach(r => {
        const tr = document.createElement("tr");
        td(tr, r.numero_consulta); td(tr, r.fecha_consulta); td(tr, r.hora_consulta);
        td(tr, r.nombre || "—"); td(tr, r.curp || "—", "mono"); td(tr, r.vigencia || "—"); td(tr, r.usuario);
        const ti = td(tr, "");
        if (r.imagen) {
          const a = document.createElement("a"); a.href = "/imagen/" + r.numero_consulta; a.target = "_blank"; a.rel = "noopener";
          const im = document.createElement("img"); im.className = "th"; im.alt = "Credencial " + r.numero_consulta; im.src = a.href;
          a.appendChild(im); ti.appendChild(a);
        } else ti.textContent = "—";
        if (ADMIN) {
          const c = td(tr, ""), b = document.createElement("button");
          b.className = "sec"; b.textContent = "Eliminar";
          b.onclick = async () => {
            if (!confirm("¿Eliminar la consulta " + r.numero_consulta + " y su imagen?")) return;
            try { await api("/api/consultas/" + r.numero_consulta, { method: "DELETE" }); load(); } catch (e) { status(e.message, "er"); }
          };
          c.appendChild(b);
        }
        tb.appendChild(tr);
      });
    } catch (e) { $("hEmpty").hidden = false; $("hEmpty").textContent = e.message; }
  }
  load();
})();
