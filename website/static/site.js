
(()=>{'use strict';
const pages=[...document.querySelectorAll('[data-page]')];
const links=[...document.querySelectorAll('.nav a, .bar .hot')];
const form=document.querySelector('#simulador'), select=document.querySelector('#escenario'), result=document.querySelector('#resultado');
const baseline='Elige un flujo y pulsa «Ver ficha del caso».';
const names={inicio:'Inicio',soluciones:'Soluciones',control:'Control humano',demo:'Demo',contacto:'Contacto'};
const flows={
  siniestros:['Aviso de daños por agua',['Canal','WhatsApp'],['Resumen','Se informa de una fuga y de daños visibles en una pared.'],['Pendiente','Fecha del incidente y referencia de póliza.'],['Destino','Equipo de siniestros. Cobertura no evaluada.']],
  atencion:['Consulta sobre un expediente',['Canal','Voz'],['Resumen','El cliente pregunta por el estado de un caso anterior.'],['Pendiente','Verificar identidad y localizar el expediente.'],['Destino','Atención al cliente. No se comunica un estado sin verificar.']],
  revision:['Consulta de cobertura sin evidencia',['Canal','WhatsApp'],['Resumen','El cliente pregunta si el daño está cubierto.'],['Pendiente','Consultar la póliza aplicable y sus condiciones.'],['Destino','Revisión humana. No se emite una decisión.']]
};
function message(text){const p=document.createElement('p');p.textContent=text;result.replaceChildren(p)}
function route(move){const [key,variant]=location.hash.slice(1).split('/');const name=Object.hasOwn(names,key)?key:'inicio';const page=pages.find(item=>item.dataset.page===name);pages.forEach(item=>{item.hidden=item!==page});links.forEach(link=>{if(link.hash==='#'+name)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current')});document.title='Aseguradora Intelligent | '+names[name];if(name==='soluciones')page.querySelectorAll('[data-product]').forEach(card=>card.classList.toggle('feature',card.dataset.product===variant));if(name==='demo'){select.value=Object.hasOwn(flows,variant)?variant:'siniestros';message(baseline)}if(move){window.scrollTo(0,0);page.querySelector('h1').focus({preventScroll:true})}}
window.addEventListener('hashchange',()=>route(true));route(false);
document.querySelector('.skip').addEventListener('click',e=>{e.preventDefault();const main=document.querySelector('main');main.focus({preventScroll:true});main.scrollIntoView()});
form.addEventListener('submit',e=>{e.preventDefault();if(!Object.hasOwn(flows,select.value)){message('Selecciona un flujo válido.');return}const [title,...fields]=flows[select.value], heading=document.createElement('strong'), dl=document.createElement('dl');heading.textContent=title;for(const [key,value] of fields){const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=key;dd.textContent=value;dl.append(dt,dd)}result.replaceChildren(heading,dl)});
form.addEventListener('reset',()=>message(baseline));select.addEventListener('change',()=>message(baseline));
})();
