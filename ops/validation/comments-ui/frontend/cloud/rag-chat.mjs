// ARGUS_AI: server-owned history and verified SSE results. No provider secret here.
export function safeLink(value) {
  try {const u=new URL(value);return ['https:','http:'].includes(u.protocol)&&!u.username&&!u.password?u.href:'';} catch{return '';}
}

export function dateFilters(from,to) {
  const formatter=new Intl.DateTimeFormat('en-GB',{timeZone:'Europe/Kyiv',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
  const midnight=day=>{
    const desired=Date.parse(day+'T00:00:00Z');let value=desired;
    for(let i=0;i<3;i++){
      const p=Object.fromEntries(formatter.formatToParts(value).map(part=>[part.type,part.value]));
      value+=desired-Date.UTC(+p.year,+p.month-1,+p.day,+p.hour,+p.minute,+p.second);
    }
    return new Date(value).toISOString();
  };
  const result={};if(from)result.from=midnight(from);
  if(to)result.to=midnight(new Date(Date.parse(to+'T00:00:00Z')+86400000).toISOString().slice(0,10));
  return result;
}

export async function readEvents(body,onEvent,signal) {
  if(!body) throw new Error('Сервер не повернув потік відповіді.');
  const reader=body.getReader(),decoder=new TextDecoder();let pending='',done=false,total=0;
  try {
    while(true) {
      if(signal?.aborted) throw new DOMException('Скасовано','AbortError');
      const chunk=await reader.read();
      pending+=decoder.decode(chunk.value||new Uint8Array(),{stream:!chunk.done}).replace(/\r\n/g,'\n');
      total+=chunk.value?.length||0;
      if(total>2*1024*1024||pending.length>1024*1024) throw new Error('Відповідь перевищує допустимий розмір.');
      let boundary;
      while((boundary=pending.indexOf('\n\n'))>=0) {
        const frame=pending.slice(0,boundary);pending=pending.slice(boundary+2);
        const data=frame.split('\n').filter(line=>line.startsWith('data:')).map(line=>line.slice(5).trimStart()).join('\n');
        if(!data) continue;
        const event=JSON.parse(data);
        if(event.type==='error') throw new Error(event.code==='rate_limit'?'Ліміт AI-запитів вичерпано. Спробуйте пізніше.':'Запит не завершився. Повторіть його пізніше.');
        if(event.type==='done') done=true;
        onEvent(event);
      }
      if(chunk.done)break;
    }
    if(!done)throw new Error('З’єднання перервано до завершення відповіді.');
  } finally {await reader.cancel().catch(()=>{});reader.releaseLock();}
}

function el(tag,text='',className='') {const node=document.createElement(tag);node.textContent=text;if(className)node.className=className;return node;}
function button(text,click) {const node=el('button',text,'btn btn--secondary');node.type='button';node.onclick=click;return node;}

// Deliberately restricted Markdown: text, headings, emphasis, lists, tables and
// quotes. HTML, arbitrary URLs, images, iframes and script never become DOM markup.
export function markdown(target,text) {
  target.replaceChildren();const lines=String(text).split('\n');let table=null,list=null,code=null;
  const inline=(node,value)=>{
    for(const part of value.split(/(\*\*[^*]+\*\*|`[^`]+`)/g)) {
      if(part.startsWith('**')&&part.endsWith('**'))node.append(el('strong',part.slice(2,-2)));
      else if(part.startsWith('`')&&part.endsWith('`'))node.append(el('code',part.slice(1,-1)));
      else node.append(document.createTextNode(part));
    }
  };
  for(const line of lines) {
    if(/^\s*```/.test(line)) {if(code){code=null;}else{const pre=el('pre');code=el('code');pre.append(code);target.append(pre);}table=null;list=null;continue;}
    if(code){code.append(document.createTextNode(line+'\n'));continue;}
    if(!line.trim()){table=null;list=null;continue;}
    if(/^\s*\|.*\|\s*$/.test(line)) {
      if(/^\s*\|[\s:|\-]+\|\s*$/.test(line))continue;
      if(!table){table=el('table');target.append(table);}
      const row=el('tr');for(const value of line.trim().slice(1,-1).split('|')){const cell=el('td');inline(cell,value.trim());row.append(cell);}table.append(row);continue;
    }
    table=null;
    const bullet=line.match(/^\s*(?:[-*]|\d+\.)\s+(.+)/);
    if(bullet){if(!list){list=el('ul');target.append(list);}const li=el('li');inline(li,bullet[1]);list.append(li);continue;}
    list=null;const heading=line.match(/^#{1,3}\s+(.+)/);const node=el(heading?'h3':line.startsWith('> ')?'blockquote':'p');
    inline(node,heading?heading[1]:line.replace(/^> /,''));target.append(node);
  }
}

export function createRagChat(request) {
  let host=null,conversation=null,controller=null,generation=0,snapshot=null,busy=false,ready=false;
  let transcript,notice,history,form,field,send,stop,from,to,platform,origin,empty;
  const unmount=()=>{generation++;controller?.abort();controller=null;host=null;busy=false;ready=false;};
  const clear=()=>{unmount();conversation=null;snapshot=null;};
  const update=value=>{snapshot=value;if(platform&&active()){const selected=platform.value;platform.replaceChildren(new Option('Усі',''));for(const p of [...new Set((snapshot?.items||[]).map(r=>r.platform))].sort())platform.add(new Option(p,p));platform.value=selected;}};
  const active=()=>host?.isConnected;
  async function refreshHistory(){
    const current=generation;
    try{const response=await request('/api/ai/conversations',{signal:controller?.signal});const data=await response.json();
      if(current!==generation||!active())return;history.replaceChildren(new Option('Нова розмова',''));
      for(const row of data.conversations||[])history.add(new Option(row.title,row.id));history.value=conversation||'';
    }catch(error){if(current===generation&&active())notice.textContent=error.message;}
  }
  function openPost(source){
    const record=snapshot?.items?.find(row=>row.id===source.post_id);
    const dialog=el('dialog','', 'rag-dialog');dialog.append(el('h3',record?.source_name||source.source_name||'Публікація ARGUS'));
    dialog.append(el('p',record?.text||source.quote));dialog.append(button('Закрити',()=>{dialog.close();dialog.remove();}));
    dialog.addEventListener('close',()=>dialog.remove(),{once:true});document.body.append(dialog);dialog.showModal();
  }
  function sources(target,rows){
    if(!rows?.length)return;const details=el('details','','rag-evidence');details.append(el('summary',`Джерела ARGUS · ${rows.length}`));
    details.append(el('p','Цитати підтверджують факт публікації, а не правдивість її змісту.','rag-proof-note'));
    const cards=el('div','','rag-sources');
    for(const row of rows){const card=el('article','','rag-source');card.append(el('strong',`[${row.number}] ${row.source_name||row.platform||'Джерело'}`));
      card.append(el('small',[row.origin,row.platform,row.published_at?new Date(row.published_at).toLocaleString('uk-UA',{timeZone:'Europe/Kyiv'}):'Час невідомий'].join(' · ')));
      card.append(el('blockquote',row.quote));const href=safeLink(row.url);
      if(href){const link=el('a','Першоджерело ↗');link.href=href;link.target='_blank';link.rel='noopener noreferrer';card.append(link);}
      card.append(button('Відкрити в ARGUS',()=>openPost(row)));cards.append(card);
    }details.append(cards);target.append(details);
  }
  function message(role,text,rows){const card=el('article','','rag-message '+(role==='user'?'rag-user':'rag-assistant'));
    empty?.remove();
    card.append(el('strong',role==='user'?'Ви':'ARGUS_AI'));const body=el('div','','rag-markdown');markdown(body,text);card.append(body);sources(card,rows);transcript.append(card);return {card,body};}
  function filters(){const value=dateFilters(from.value,to.value);
    if(platform.value)value.platforms=[platform.value];if(origin.value)value.origins=[origin.value];return value;}
  async function submit(event){event.preventDefault();if(!ready||busy||!field.value.trim())return;
    const question=field.value.trim();field.value='';busy=true;send.disabled=true;history.disabled=true;stop.hidden=false;
    const current=++generation;controller?.abort();controller=new AbortController();message('user',question);const reply=message('assistant','…');let text='';
    try{const response=await request('/api/ai/chat',{method:'POST',body:JSON.stringify({message:question,filters:filters(),conversation_id:conversation}),signal:controller.signal});
      await readEvents(response.body,event=>{if(current!==generation||!active())return;
        if(event.conversation_id)conversation=event.conversation_id;
        if(event.type==='status')notice.textContent=event.stage==='generating_general'?'DeepSeek відповідає…':event.stage==='generating'?'Готую відповідь за новинами…':'Перевіряю зібрані новини…';
        if(event.type==='retrieval')notice.textContent=event.matched_count?`Знайдено матеріалів: ${event.matched_count}`:'Готую відповідь…';
        if(event.type==='delta'){text+=event.text;reply.body.textContent=text;}
        if(event.type==='done'){markdown(reply.body,event.content);sources(reply.card,event.sources);notice.textContent=event.mode==='deepseek_general'?'DeepSeek · поза базою ARGUS':event.basis==='archive'?`Матеріали ARGUS · ${event.matched_count}`:event.mode==='unavailable'?'DeepSeek недоступний':'';}
        transcript.scrollTop=transcript.scrollHeight;
      },controller.signal);
      if(current===generation)await refreshHistory();
    }catch(error){if(current===generation&&active()){if(!text)reply.body.textContent=error.name==='AbortError'?'Відповідь зупинено.':error.message;notice.textContent=error.name==='AbortError'?'Зупинено':'Відповідь не завершена.';}}
    finally{if(current===generation){busy=false;send.disabled=!ready;history.disabled=!ready;stop.hidden=true;}}
  }
  function welcome(){empty=el('div','','rag-empty');empty.append(el('h2','Чим можу допомогти?'),el('p','Запитайте про новини або почніть розмову.'));transcript.replaceChildren(empty);}
  async function loadConversation(){
    if(busy)return;const current=++generation;controller?.abort();controller=new AbortController();conversation=history.value||null;welcome();if(!conversation)return;
    try{const response=await request('/api/ai/conversations?id='+encodeURIComponent(conversation),{signal:controller.signal});const data=await response.json();if(current!==generation||!active())return;for(const row of data.messages||[])message(row.role,row.content,row.sources);}
    catch(error){if(current===generation&&active()&&error.name!=='AbortError')notice.textContent=error.message;}
  }
  async function mount(main){
    generation++;controller?.abort();controller=new AbortController();busy=false;ready=false;host=main;main.replaceChildren();
    const layout=el('section','','rag-chat');layout.append(el('h1','ARGUS_AI'));
    const controls=el('div','','rag-controls');history=el('select');history.setAttribute('aria-label','Історія діалогів');history.add(new Option('Нова розмова',''));
    const newChat=button('Нова розмова',()=>{if(!ready)return;generation++;controller?.abort();controller=new AbortController();conversation=null;welcome();history.value='';busy=false;send.disabled=!ready;history.disabled=!ready;stop.hidden=true;notice.textContent='';field.focus();});newChat.disabled=true;history.disabled=true;controls.append(history,newChat);
    history.onchange=loadConversation;
    layout.append(controls);
    const searchOptions=el('details','','rag-search-options');searchOptions.append(el('summary','Пошук у новинах'));const filterRow=el('div','','rag-controls');searchOptions.append(filterRow);
    for(const [label,make] of [['Від',()=>{from=el('input');from.type='date';return from;}],['До',()=>{to=el('input');to.type='date';return to;}],['Платформа',()=>{platform=el('select');platform.add(new Option('Усі',''));for(const p of [...new Set((snapshot?.items||[]).map(r=>r.platform))].sort())platform.add(new Option(p,p));return platform;}],['Потік',()=>{origin=el('select');for(const [t,v] of [['Усі доступні',''],['Загальний','general'],['ДСНС','dsns']])origin.add(new Option(t,v));return origin;}]]){const wrapper=el('label',label);const control=make();control.setAttribute('aria-label',label);wrapper.append(control);filterRow.append(wrapper);}
    layout.append(searchOptions);notice=el('p','Перевіряю підключення…','rag-status');notice.setAttribute('role','status');layout.append(notice);
    const retry=button('Перевірити підключення',()=>void connect());retry.hidden=true;layout.append(retry);
    transcript=el('div','','rag-transcript');transcript.setAttribute('aria-live','polite');layout.append(transcript);
    welcome();form=el('form','','rag-compose');field=el('textarea');field.maxLength=4000;field.rows=2;field.required=true;field.setAttribute('aria-label','Запит до ARGUS_AI');field.placeholder='Напишіть повідомлення…';
    field.onkeydown=event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();if(ready&&!busy&&field.value.trim())form.requestSubmit();}};
    send=el('button','Надіслати','btn btn--primary');send.type='submit';send.disabled=true;stop=button('Зупинити',()=>controller?.abort());stop.hidden=true;form.append(field,stop,send);form.onsubmit=submit;layout.append(form,el('small','Enter — надіслати · Shift + Enter — новий рядок','rag-compose-hint'));main.append(layout);
    async function connect(){
      const current=generation;retry.disabled=true;notice.textContent='Перевіряю підключення…';
      try{const response=await request('/api/ai/status',{signal:controller.signal});const state=await response.json();if(current!==generation||!active())return;
        ready=true;send.disabled=false;history.disabled=false;newChat.disabled=false;retry.hidden=true;
        notice.textContent=state.providers?.configured?'':'DeepSeek ще не підключено. Доступний пошук у новинах.';await refreshHistory();
        if(current===generation&&conversation)await loadConversation();
      }catch(error){if(current===generation&&active()){notice.textContent=error.message;retry.hidden=false;}}
      finally{if(current===generation)retry.disabled=false;}
    }
    await connect();
  }
  window.addEventListener('hashchange',()=>{if(host&&!host.isConnected){generation++;controller?.abort();busy=false;}});
  return {mount,unmount,clear,update};
}
