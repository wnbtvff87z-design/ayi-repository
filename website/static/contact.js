(() => {
  'use strict';
  const form = document.querySelector('#contact');
  const button = document.querySelector('#contact-submit');
  const remember = document.querySelector('#remember');
  const clear = document.querySelector('#clear-draft');
  const notice = document.querySelector('#contact-status');
  const fields = ['name','company','email','message'];
  const name = 'intelligent-contact-v1';
  const maxAge = 86400000;
  const read = kind => {
    try {
      const storage = window[kind];
      const data = JSON.parse(storage.getItem(name));
      if (!data || !data.until || Date.now() >= data.until) { storage.removeItem(name); return null; }
      return data;
    } catch { return null; }
  };
  const write = (kind, value) => {
    try {
      if (value) window[kind].setItem(name,JSON.stringify(value));
      else window[kind].removeItem(name);
      return true;
    } catch { return false; }
  };
  let draft = read('sessionStorage') || read('localStorage');
  const values = () => Object.fromEntries(fields.map(k => [k,form.elements[k].value]));
  function persist() {
    const data = values();
    if (!draft || JSON.stringify(data) !== JSON.stringify(draft.data)) {
      draft = {data,key:crypto.randomUUID(),until:Date.now()+maxAge};
    }
    draft.remember = remember.checked;
    const inTab = write('sessionStorage',draft);
    const inDevice = write('localStorage',remember.checked ? draft : null);
    return inTab || (remember.checked && inDevice);
  }
  function forget() {
    write('sessionStorage',null); write('localStorage',null);
    draft = null; form.reset();
  }
  if (draft?.data) {
    for (const k of fields) form.elements[k].value = draft.data[k] || '';
    remember.checked = !!draft.remember;
    notice.textContent = 'Borrador recuperado. Todavía no se ha confirmado el envío.';
  }
  if (!globalThis.crypto?.randomUUID) {
    notice.textContent = 'Abre la web en HTTPS para enviar la consulta.';
    return;
  }
  button.disabled = false;
  form.addEventListener('input',persist);
  form.addEventListener('change',persist);
  clear.addEventListener('click',() => { forget(); notice.textContent = 'Borrador eliminado.'; form.elements.name.focus(); });
  form.addEventListener('submit',async event => {
    event.preventDefault();
    if (button.disabled) return;
    const stored = persist();
    const body = JSON.stringify({...draft.data,key:draft.key,website:form.elements.website.value});
    [...form.elements].forEach(el => { el.disabled = true; });
    notice.textContent='Guardando consulta…';
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(),15000);
    try {
      const response = await fetch(form.action,{method:'POST',headers:{'Content-Type':'application/json'},body,signal:controller.signal});
      let result;
      try { result = await response.json(); } catch { result = {}; }
      if (response.ok && typeof result.reference === 'string') {
        forget(); notice.textContent = `Consulta guardada. Referencia: ${result.reference}.`;
      } else {
        notice.textContent = `${result.error || 'No pudimos confirmar el guardado.'} ${stored ? 'Tu borrador sigue disponible.' : 'Copia el texto antes de cerrar esta página.'}`;
      }
    } catch {
      notice.textContent = `Sin confirmación del servidor. ${stored ? 'Conservamos el borrador para reintentar.' : 'Copia el texto antes de cerrar esta página.'}`;
    } finally {
      clearTimeout(timer);
      [...form.elements].forEach(el => { el.disabled = false; });
    }
  });
})();
