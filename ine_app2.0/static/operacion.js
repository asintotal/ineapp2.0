(function () {
  const { $, status, api } = App;
  const ADMIN = document.body.dataset.admin === "1";
  const HOY = document.body.dataset.hoy;
  const tbody = $("filas");
  const filas = new Map();        // número de consulta -> { tr, btn, csf }
  let cargando = false, diaMostrado = null;

  function td(tr, texto, cls) {
    const c = document.createElement("td");
    c.textContent = texto; if (cls) c.className = cls;
    tr.appendChild(c); return c;
  }

  function resumen() {
    const total = filas.size; let hechos = 0;
    filas.forEach(f => { if (f.csf) hechos++; });
    $("resumen").textContent = total
      ? total + " registro(s) · " + hechos + " con CSF generado · " + (total - hechos) + " pendiente(s)"
      : "";
    $("vacio").hidden = total > 0;
    $("vacio").textContent = "No hay registros en este día.";
  }

  function pintar(f) {
    f.tr.classList.toggle("csf-ok", f.csf);
    f.btn.disabled = f.csf;
    f.btn.title = f.csf ? "CSF ya generado" : "";
  }

  async function marcar(numero) {
    const f = filas.get(numero); if (!f || f.csf) return;
    f.btn.disabled = true;
    try {
      await api("/api/registros/" + numero + "/csf", { method: "POST" });
      f.csf = true; pintar(f); resumen(); status("");
    } catch (e) { f.btn.disabled = false; status(e.message, "er"); }
  }

  async function eliminar(numero) {
    if (!confirm("¿Eliminar la consulta " + numero + " y su imagen? Se borra para todos.")) return;
    try {
      await api("/api/registros/" + numero, { method: "DELETE" });
      const f = filas.get(numero); if (f) { f.tr.remove(); filas.delete(numero); }
      resumen();
    } catch (e) { status(e.message, "er"); }
  }

  function crear(r) {
    const tr = document.createElement("tr");
    td(tr, r.numero); td(tr, r.fecha_hora); td(tr, r.curp || "—", "mono");
    const ti = td(tr, "");
    if (r.imagen) {
      const a = document.createElement("a");
      a.href = "/imagen/" + r.numero; a.target = "_blank"; a.rel = "noopener";
      const im = document.createElement("img");
      im.className = "th"; im.loading = "lazy"; im.alt = "Credencial " + r.numero; im.src = a.href;
      a.appendChild(im); ti.appendChild(a);
    } else ti.textContent = "—";
    const btn = document.createElement("button");
    btn.type = "button"; btn.className = "csf"; btn.textContent = "CSF Generado";
    btn.onclick = () => marcar(r.numero);
    td(tr, "").appendChild(btn);
    if (ADMIN) {
      const x = document.createElement("button");
      x.type = "button"; x.className = "sec"; x.textContent = "Eliminar";
      x.onclick = () => eliminar(r.numero);
      td(tr, "").appendChild(x);
    }
    return { tr, btn, csf: false };
  }

  async function cargar(silencioso) {
    if (cargando) return; cargando = true;
    try {
      const d = await api("/api/registros?fecha=" + encodeURIComponent($("fecha").value || HOY));
      if (d.fecha !== diaMostrado) { tbody.textContent = ""; filas.clear(); diaMostrado = d.fecha; }
      const vistos = new Set();
      let ref = tbody.firstChild;
      d.registros.forEach(r => {      // se actualizan las filas existentes (sin recargar sus imágenes) y se insertan las nuevas
        vistos.add(r.numero);
        let f = filas.get(r.numero);
        if (!f) { f = crear(r); filas.set(r.numero, f); }
        f.csf = r.csf; pintar(f);
        if (f.tr !== ref) tbody.insertBefore(f.tr, ref); else ref = ref.nextSibling;
      });
      filas.forEach((f, n) => { if (!vistos.has(n)) { f.tr.remove(); filas.delete(n); } });
      resumen();
      if (!silencioso) status("");
    } catch (e) { if (!silencioso) status(e.message, "er"); $("vacio").hidden = true; }
    finally { cargando = false; }
  }

  $("fecha").onchange = () => cargar();
  $("hoy").onclick = () => { $("fecha").value = HOY; cargar(); };
  $("actualizar").onclick = () => cargar();
  setInterval(() => { if (!document.hidden) cargar(true); }, 20000);
  cargar();
})();
