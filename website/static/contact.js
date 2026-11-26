(() => {
  const form = document.querySelector('#contact');
  const button = form.querySelector('[type=submit]');
  const status = document.querySelector('#status');
  const names = ['name','company','email','message'];
  const storageKey = 'intelligent-contact-v1';
  let draft;
  try {
    const saved = JSON.parse(sessionStorage.getItem(storageKey));
    if (saved?.until > Date.now()) { draft = saved; for (const k of names) form.elements[k].value = draft.data[k] || ''; }
    else sessionStorage.removeItem(storageKey);
  } catch { /* In restricted browsers, retain the current form only. */ }
  const clear = () => {
    draft = null; form.reset();
    for (const kind of ['sessionStorage','localStorage']) try { window[kind].removeItem(storageKey); } catch { /* unavailable */ }
  };
  if (!globalThis.crypto?.randomUUID) { status.textContent = 'Abre la web en HTTPS para enviar.'; return; }
  button.disabled = false;
  function persist() {
    const data = Object.fromEntries(names.map(k => [k,form.elements[k].value]));
    if (!draft || JSON.stringify(draft.data) !== JSON.stringify(data)) draft = {data,key:crypto.randomUUID(),until:Date.now()+86400000};
    try { sessionStorage.setItem(storageKey,JSON.stringify(draft)); return true; } catch { return false; }
  }
  form.addEventListener('input',persist);
  document.querySelector('#clear').addEventListener('click',() => {clear();status.textContent='Borrador eliminado.';});
  form.addEventListener('submit',async event => {
    event.preventDefault(); if (button.disabled) return;
    const stored = persist();
    const body = JSON.stringify({...draft.data,key:draft.key,website:form.elements.website.value});
    for (const field of form.elements) field.disabled = true;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(),15000);
    status.textContent = 'Guardando consulta…';
    try {
      const response = await fetch(form.action,{method:'POST',headers:{'Content-Type':'application/json'},body,signal:controller.signal});
      const result = await response.json().catch(() => ({}));
      if (!response.ok || typeof result.reference !== 'string') throw new Error(result.error || 'No pudimos confirmar el guardado.');
      clear();status.textContent = `Consulta recibida. Referencia: ${result.reference}.`;
    } catch (e) {
      status.textContent = 'No pudimos confirmar la consulta. '+(stored?'Tu borrador sigue disponible. Reintenta sin editar para evitar duplicados.':'Copia el texto antes de cerrar la página.')+(e.name==='Error'?' '+e.message:'');
    } finally { clearTimeout(timer); for (const field of form.elements) field.disabled = false; }
  });
})();
