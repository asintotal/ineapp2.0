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
    const r = await fetch(url, opts);
    if (r.status === 401) { location.href = "/login"; throw new Error("Sesión expirada"); }
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.error || "Error del servidor (" + r.status + ")");
    return d;
  }

  $("go").onclick = async () => {
    if (busy || !file) return;
    busy = true; $("go").disabled = true; status("Procesando imagen…");
    try {
      const fd = new FormData(); fd.append("imagen", file);
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
