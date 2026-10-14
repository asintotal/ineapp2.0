(function () {
  const { $, status, api, reducir } = App;
  let token = null, ocupado = false, urlPrev = null;

  function limpiarVista() {
    if (urlPrev) { URL.revokeObjectURL(urlPrev); urlPrev = null; }
    $("foto").value = "";
    $("vista").hidden = true; $("resultado").hidden = true;
    $("tomar").hidden = false; $("guardar").disabled = true;
  }

  function descartar() {            // libera el borrador del servidor (si no se llega a guardar)
    if (!token) return;
    const t = token; token = null;
    api("/api/descartar", { method: "POST", headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ token: t }) }).catch(() => {});
  }

  function mostrar(r) {
    $("curp").textContent = r.curp || "—";
    $("resultado").hidden = false;
    const v = $("validez");
    if (r.curp_valido) {
      token = r.token;
      v.textContent = "✔ El CURP tiene la estructura oficial. Compáralo con la credencial; si coincide, pulsa Guardar.";
      v.className = "note ok";
      $("guardar").disabled = false;
      status("");
    } else {
      v.className = "note wa";
      v.textContent = r.curp
        ? "⚠ El CURP leído no tiene la estructura oficial, así que no se puede guardar."
        : "⚠ No se encontró un CURP en la imagen.";
      status("Toma otra foto: de frente, nítida, bien iluminada y sin reflejos del holograma.", "wa");
    }
  }

  async function leer(f) {
    if (!f || !f.type.startsWith("image/")) { status("Selecciona una imagen.", "wa"); return; }
    descartar();
    if (urlPrev) URL.revokeObjectURL(urlPrev);
    urlPrev = URL.createObjectURL(f);
    $("prev").src = urlPrev;
    $("tomar").hidden = true; $("vista").hidden = false; $("resultado").hidden = true; $("guardar").disabled = true;
    ocupado = true; status("Leyendo CURP…");
    try {
      const envio = await reducir(f);
      const fd = new FormData(); fd.append("imagen", envio, "credencial.jpg");
      mostrar(await api("/api/leer", { method: "POST", body: fd }));
    } catch (e) { status(e.message, "er"); }
    finally { ocupado = false; }
  }

  $("tomar").onclick = () => $("foto").click();
  $("foto").onchange = e => leer(e.target.files[0]);
  $("otra").onclick = () => { if (ocupado) return; descartar(); limpiarVista(); status(""); $("foto").click(); };

  $("guardar").onclick = async () => {
    if (ocupado || !token) return;
    ocupado = true; $("guardar").disabled = true; status("Guardando…");
    try {
      const r = await api("/api/guardar", { method: "POST", headers: { "Content-Type": "application/json" },
                                            body: JSON.stringify({ token }) });
      token = null; limpiarVista();
      status("✔ Guardado: consulta " + r.numero_consulta + " (" + r.curp + "). Puedes tomar la siguiente foto.", "ok");
    } catch (e) {
      if (/expir|ya se guard/i.test(e.message)) { token = null; limpiarVista(); }
      else $("guardar").disabled = false;
      status(e.message, "er");
    } finally { ocupado = false; }
  };
})();
