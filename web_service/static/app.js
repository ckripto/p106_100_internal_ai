'use strict';
const $ = id => document.getElementById(id);
const state = {sid:null, tasks:new Map(), hasMore:false, busy:false, refreshing:false, listKey:'', detailKey:''};
const labels = {queued:'В очереди',running:'Выполняется',success:'Готово',failed:'Не выполнено',interrupted:'Прервано'};
const actorLabels = {coordinator:'Координатор',executor:'Executor',developer:'Developer'};
function saved(key, value) { try { if(value === undefined) return localStorage.getItem(key); if(value === null) localStorage.removeItem(key); else localStorage.setItem(key,value); } catch(_) {} }
function notice(text='') { $('notice').textContent=text; $('notice').hidden=!text; }
async function api(path, method='GET', body) {
  const response = await fetch('/api'+path,{method,headers:{'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});
  if(response.status===204) return null;
  const data=await response.json();
  if(!response.ok) { const error=new Error(data.error||'Ошибка запроса'); error.status=response.status; throw error; }
  return data;
}
function element(tag, cls, text) { const e=document.createElement(tag); if(cls)e.className=cls; if(text!==undefined)e.textContent=text; return e; }
function date(ts) { return new Date(ts*1000).toLocaleString('ru-RU',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'}); }
function messageDate(ts) { return new Date(ts*1000).toLocaleString('ru-RU',{hour:'2-digit',minute:'2-digit',second:'2-digit'}); }
function duration(seconds) {
  if(seconds===null||seconds===undefined)return '';
  if(seconds<1)return Math.round(seconds*1000)+' мс';
  if(seconds<60)return seconds.toFixed(seconds<10?2:1)+' с';
  const minutes=Math.floor(seconds/60), rest=Math.round(seconds%60);
  return minutes+' мин '+rest+' с';
}
function agentMessages(task, opened) {
  const messages=task.agent_messages||[]; if(!messages.length)return null;
  const details=element('details','agent-messages'); details.dataset.id='messages-'+task.id;
  details.open=opened.has(details.dataset.id);
  details.append(element('summary','',`Сообщения агентов (${messages.length})`));
  const list=element('ol','message-list');
  for(const message of messages) {
    const item=element('li','agent-message '+message.kind), meta=element('div','message-meta');
    meta.append(
      element('strong','',actorLabels[message.sender]||message.sender),
      element('span','message-arrow','→'),
      element('span','',actorLabels[message.recipient]||message.recipient),
      element('span','message-attempt','Попытка '+message.attempt),
      element('time','message-time',messageDate(message.created))
    );
    if(message.response_seconds!==null)meta.append(element('span','response-time','Ответ за '+duration(message.response_seconds)));
    let content=message.content;
    if(message.kind==='response')try{content=JSON.stringify(JSON.parse(content),null,2);}catch(_){}
    item.append(meta,element('pre','message-content',content)); list.append(item);
  }
  details.append(list); return details;
}
function menu(open) { $('sidebar').classList.toggle('open',open); $('overlay').hidden=!open; }
function draft() { if(state.sid) saved('draft:'+state.sid,$('prompt').value); }
function count() { $('count').textContent=$('prompt').value.length+' / 1800'; $('send').disabled=state.busy||!state.sid||!$('prompt').value.trim(); }
function renderList(sessions) {
  const key=JSON.stringify([sessions,state.sid]); if(key===state.listKey)return; state.listKey=key;
  $('sessions').replaceChildren();
  if(!sessions.length)$('sessions').append(element('p','empty-sessions','Здесь появятся ваши задачи'));
  for(const session of sessions) {
    const button=element('button','session'+(session.id===state.sid?' active':''));
    button.setAttribute('aria-current',session.id===state.sid?'true':'false');
    button.append(element('strong','',session.title),element('small','',session.pending?session.pending+' в работе / очереди':date(session.updated)));
    button.onclick=()=>select(session.id); $('sessions').append(button);
  }
}
function renderTasks() {
  const tasks=[...state.tasks.values()].sort((a,b)=>a.id-b.id);
  const key=JSON.stringify(tasks); if(key===state.detailKey)return; state.detailKey=key;
  const area=$('conversation'); const bottom=area.scrollHeight-area.scrollTop-area.clientHeight<120;
  const opened=new Set([...document.querySelectorAll('details[open]')].map(d=>d.dataset.id));
  $('messages').replaceChildren(); $('welcome').hidden=tasks.length>0;
  for(const task of tasks) {
    const turn=element('article','turn'); turn.dataset.taskId=task.id;
    turn.append(element('div','prompt-bubble',task.prompt));
    const answer=element('div','answer'), label=element('div','answer-label');
    label.append(element('strong','','✳ Координатор'),element('span','badge '+task.status,labels[task.status]||task.status),element('time','task-time',date(task.created)));
    answer.append(label);
    const text=task.result?.summary || (task.status==='queued'?'Задача сохранена и ожидает своей очереди.':task.progress||'Выполняется. Можно закрыть браузер.');
    answer.append(element('p','answer-text'+(task.result?'':' pending-text'),text));
    const messages=agentMessages(task,opened); if(messages)answer.append(messages);
    if(task.result) {
      const details=element('details'); details.dataset.id='result-'+task.id; details.open=opened.has(details.dataset.id);
      details.append(element('summary','','Подробности результата'),element('pre','',JSON.stringify(task.result,null,2))); answer.append(details);
    }
    if(task.status==='failed'||task.status==='interrupted') {
      const retry=element('button','quiet retry','Использовать задачу снова');
      retry.onclick=()=>{ $('prompt').value=task.prompt; draft(); count(); $('prompt').focus(); }; answer.append(retry);
    }
    turn.append(answer); $('messages').append(turn);
  }
  $('load-older').hidden=!state.hasMore;
  if(bottom)area.scrollTop=area.scrollHeight;
}
async function select(sid) {
  draft(); state.sid=sid; saved('session',sid); state.tasks.clear(); state.detailKey='';
  $('prompt').value=saved('draft:'+sid)||''; $('prompt').disabled=false; $('delete-session').disabled=false; count(); menu(false);
  $('messages').replaceChildren(); $('welcome').hidden=false; $('title').textContent='Загрузка сессии…';
  await refresh(true);
  $('conversation').scrollTop=$('conversation').scrollHeight;
}
async function refresh(force=false) {
  if(state.refreshing&&!force)return;
  state.refreshing=true; const sid=state.sid;
  try {
    const data=await api('/sessions'); renderList(data.sessions);
    if(sid) {
      const detail=await api('/sessions/'+sid);
      if(state.sid!==sid)return;
      $('title').textContent=detail.title;
      if(!state.tasks.size)state.hasMore=detail.has_more;
      for(const task of detail.tasks)state.tasks.set(task.id,task);
      renderTasks();
    }
    notice();
  } catch(error) {
    if(state.sid===sid && error.status===404) {
      state.sid=null; saved('session',null); state.tasks.clear(); state.detailKey='';
      $('messages').replaceChildren(); $('welcome').hidden=false; $('title').textContent='Выберите или создайте сессию';
      $('prompt').disabled=true; $('delete-session').disabled=true; count();
    }
    notice(error.message==='Failed to fetch'?'Нет связи с сервисом. Сохранённые задачи продолжат выполняться.':error.message);
  } finally { state.refreshing=false; }
}
$('new-session').onclick=async()=>{
  $('new-session').disabled=true;
  try { const session=await api('/sessions','POST',{}); await select(session.id); }
  catch(e){notice(e.message);} finally{$('new-session').disabled=false;}
};
$('delete-session').onclick=async()=>{
  const sid=state.sid;
  if(!sid||!confirm('Удалить сессию со всеми задачами и результатами?'))return;
  try {
    await api('/sessions/'+sid,'DELETE',{}); saved('draft:'+sid,null); saved('pending:'+sid,null);
    state.sid=null; saved('session',null); state.tasks.clear(); state.detailKey='';
    $('messages').replaceChildren(); $('welcome').hidden=false; $('load-older').hidden=true;
    $('title').textContent='Новая задача начинается здесь'; $('prompt').value=''; $('prompt').disabled=true; $('delete-session').disabled=true; count(); await refresh();
  }catch(e){notice(e.message);}
};
$('composer').onsubmit=async event=>{
  event.preventDefault(); if(state.busy||!state.sid||!$('prompt').value.trim())return;
  state.busy=true; count(); const sid=state.sid, prompt=$('prompt').value.trim();
  let pending; try{pending=JSON.parse(saved('pending:'+sid)||'null');}catch(_){}
  if(!pending||pending.prompt!==prompt)pending={prompt,request_id:crypto.randomUUID?crypto.randomUUID():Date.now().toString(36)+'-'+Math.random().toString(36).slice(2)};
  saved('pending:'+sid,JSON.stringify(pending));
  try {
    await api('/sessions/'+sid+'/tasks','POST',pending);
    saved('pending:'+sid,null); saved('draft:'+sid,null);
    if(state.sid===sid && $('prompt').value.trim()===prompt){$('prompt').value='';}
    await refresh(true); if(state.sid===sid)$('conversation').scrollTop=$('conversation').scrollHeight;
  }catch(e){notice('Не удалось подтвердить отправку. Текст сохранён; можно повторить отправку. '+e.message);}
  finally{state.busy=false;count();}
};
$('load-older').onclick=async()=>{
  const sid=state.sid; const before=Math.min(...state.tasks.keys()); $('load-older').disabled=true;
  try {
    const data=await api('/sessions/'+sid+'?before='+before); if(sid!==state.sid)return;
    const oldHeight=$('conversation').scrollHeight;
    for(const task of data.tasks)state.tasks.set(task.id,task); state.hasMore=data.has_more; renderTasks();
    $('conversation').scrollTop=$('conversation').scrollHeight-oldHeight;
  }catch(e){notice(e.message);}finally{$('load-older').disabled=false;}
};
$('prompt').oninput=()=>{draft();count();};
$('prompt').onkeydown=event=>{if(event.key==='Enter'&&(event.ctrlKey||event.metaKey)){event.preventDefault();$('composer').requestSubmit();}};
$('open-menu').onclick=()=>menu(true); $('close-menu').onclick=()=>menu(false); $('overlay').onclick=()=>menu(false);
document.addEventListener('keydown',e=>{if(e.key==='Escape')menu(false);});
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
const last=saved('session'); if(last)select(last); else refresh();
setInterval(()=>{if(!document.hidden)refresh();},3000);
