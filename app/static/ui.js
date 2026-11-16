function toast(msg,kind){let w=document.querySelector('.toast-wrap');if(!w){w=document.createElement('div');w.className='toast-wrap';document.body.appendChild(w)}
const t=document.createElement('div');t.className='toast'+(kind==='err'?' err':'');t.textContent=String(msg);w.appendChild(t);setTimeout(()=>t.remove(),6000)}
function askConfirm(msg,okLabel){return new Promise(res=>{const d=document.createElement('dialog');
const f=document.createElement('form');f.method='dialog';const p=document.createElement('p');p.textContent=msg;
const r=document.createElement('div');r.className='row';r.style.justifyContent='flex-end';
const no=document.createElement('button');no.value='no';no.textContent='Cancel';
const ok=document.createElement('button');ok.value='yes';ok.className='primary';ok.textContent=okLabel||'OK';
r.append(no,ok);f.append(p,r);d.append(f);document.body.appendChild(d);
d.addEventListener('close',()=>{res(d.returnValue==='yes');d.remove()});d.showModal()})}
