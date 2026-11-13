import {safeUrl} from './workspace-data.mjs';

const names={telegram:'Telegram',youtube:'YouTube',facebook:'Facebook',instagram:'Instagram',reddit:'Reddit',rss:'Сайти',wordpress:'WordPress',x:'X',threads:'Threads',tiktok:'TikTok',vk:'VK',ok:'OK',max:'MAX',disqus:'Disqus'};
const fold=v=>String(v||'').toLocaleLowerCase('uk-UA');
const day=v=>v&&Number.isFinite(Date.parse(v))?new Intl.DateTimeFormat('sv-SE',{timeZone:'Europe/Kyiv'}).format(new Date(v)):'';
function el(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text)n.textContent=text;return n;}
function link(label,url){const n=el('a','comment-link',label);const valid=safeUrl(url);if(!valid)return el('span','',label);n.href=valid;n.target='_blank';n.rel='noopener noreferrer';return n;}
export function matches(row,f){
  const date=day(row.published_at||row.collected_at);
  return (!f.platform||row.platform===f.platform)&&(!f.source||row.source_id===f.source)&&(!f.post||row.post_id===f.post)&&
    (!f.from||date>=f.from)&&(!f.to||date<=f.to)&&(!f.sentiment||row.sentiment===f.sentiment)&&
    (!f.query||fold([row.text,row.author,row.post_text,row.source_name].join(' ')).includes(fold(f.query)));
}
export function candidatePages(catalog,f){return catalog.pages.filter(p=>(!f.platform||p.platforms.includes(f.platform))&&
  (!f.source||p.sources.includes(f.source))&&(!f.post||p.posts.includes(f.post))&&(!f.from||day(p.last)>=f.from)&&(!f.to||day(p.first)<=f.to))
  .sort((a,b)=>b.last.localeCompare(a.last));}

export function groupComments(rows){
  const groups=new Map();
  for(const row of rows){
    if(!groups.has(row.post_id))groups.set(row.post_id,{post:row,comments:new Map()});
    groups.get(row.post_id).comments.set(row.id,row);
  }
  return [...groups.values()].map(g=>({...g,comments:[...g.comments.values()]}));
}
export function threadOrder(rows){
  const byId=new Map(rows.map(r=>[r.id,r])),children=new Map(),seen=new Set(),out=[];
  for(const row of rows){const parent=byId.has(row.parent_id)&&row.parent_id!==row.id?row.parent_id:null;
    if(!children.has(parent))children.set(parent,[]);children.get(parent).push(row);}
  function visit(start){const stack=[{row:start,depth:0}];while(stack.length){
    const {row,depth}=stack.pop();if(seen.has(row.id))continue;seen.add(row.id);out.push({row,depth:Math.min(depth,4)});
    const nested=children.get(row.id)||[];for(let i=nested.length-1;i>=0;i--)stack.push({row:nested[i],depth:depth+1});}}
  for(const row of children.get(null)||[])visit(row);
  for(const row of rows)visit(row); // Defensive cycle handling; never lose a comment.
  return out;
}

export function sameCommentPages(a,b){
  return a.length===b.length&&a.every((p,i)=>p.sha256===b[i].sha256);
}

export function createComments(drive){
  let main=null,catalog=null,epoch=0,loading=false,refreshing=false,pages=[],offset=0,shown=0,list,summary,more,filters={};
  const cache=new Map(),selectedRows=new Map(),visibleLimits=new Map(),loadedPages=new Set(),pendingRefresh=new Set();
  const alive=()=>main?.isConnected&&location.hash==='#/comments';
  function comment(row,depth){
    const node=el('div','comment-row'+(depth?' comment-row--reply':''));node.id='comment-'+row.id;
    node.style.marginLeft=(depth*18)+'px';
    const body=el('div','comment-row__body');node.append(body);
    const meta=el('div','comment-post-card__meta');
    meta.append(el('strong','',row.author||'Автор не вказаний'));
    const stamp=row.published_at||row.collected_at;
    if(stamp)meta.append(el('time','',(row.published_at?(row.published_at_precision==='approximate'?'Приблизно ':''):'Зібрано: ')+new Date(stamp).toLocaleString('uk-UA',{timeZone:'Europe/Kyiv'})));
    body.append(meta,el('p','comment-row__text',row.text));
    if(row.parent_id){const parent=el('a','','↳ Відповідь на коментар');parent.href='#comment-'+row.parent_id;
      parent.onclick=e=>{e.preventDefault();const target=document.getElementById('comment-'+row.parent_id);if(target)target.scrollIntoView({block:'center'});else {visibleLimits.set(row.post_id,Infinity);draw();document.getElementById('comment-'+row.parent_id)?.scrollIntoView({block:'center'});}};body.append(parent);}
    const footer=el('div','comment-post-card__meta');
    if(row.url)footer.append(link('Оригінал коментаря',row.url));
    if(Number.isInteger(row.likes))footer.append(el('span','','Реакції: '+row.likes));
    body.append(footer);
    return node;
  }
  function card(group){
    const row=group.post,node=el('article','comment-post-card');node.dataset.postId=row.post_id;
    const meta=el('div','comment-post-card__meta');
    meta.append(el('strong','',row.source_name||'Допис'),el('span','comment-platform-badge',names[row.platform]||row.platform),link('Відкрити допис',row.post_url));
    node.append(meta,el('p','comment-row__text',row.post_text));
    const title=el('h2','comment-discussion-title','Коментарі ('+group.comments.length+')');node.append(title);
    const discussion=el('div','comment-discussion'),ordered=threadOrder(group.comments),limit=visibleLimits.get(row.post_id)||100;
    discussion.append(...ordered.slice(0,limit).map(({row,depth})=>comment(row,depth)));node.append(discussion);
    if(ordered.length>limit){const expand=el('button','comments-load-more','Показати ще '+Math.min(100,ordered.length-limit)+' коментарів');
      expand.onclick=()=>{visibleLimits.set(row.post_id,limit+100);node.replaceWith(card(group));};node.append(expand);}
    if(!filters.post){const focus=el('button','comments-load-more','Відкрити все обговорення');focus.onclick=()=>{filters={post:row.post_id};render();};node.append(focus);}
    return node;
  }
  function draw(){
    // Preserve the reader's visible comment when prepending or updating a page.
    const anchor=[...list.querySelectorAll('.comment-row')].find(n=>n.getBoundingClientRect().top>=0);
    const top=anchor?.getBoundingClientRect().top;
    list.replaceChildren(...groupComments(selectedRows.values()).map(card));
    if(anchor){const replacement=document.getElementById(anchor.id);if(replacement){
      const delta=replacement.getBoundingClientRect().top-top;
      const scroller=main.closest('.dashboard-shell__wrapper');
      if(scroller)scroller.scrollTop+=delta;else window.scrollBy(0,delta);
    }}
  }
  async function next({live=false}={}){
    if(loading||!alive())return;loading=true;more.disabled=true;const current=epoch;let scanned=0;
    try{
      // Bound each interaction to three server-generated pages, never the archive.
      while(offset<pages.length&&scanned<3){
        const descriptor=pages[offset];let rows=cache.get(descriptor.sha256);
        if(live&&!pendingRefresh.has(descriptor.sha256))break;
        if(loadedPages.has(descriptor.sha256)){pendingRefresh.delete(descriptor.sha256);offset++;continue;}
        if(!rows){rows=await drive.readCommentsPage(descriptor);if(current!==epoch||!alive())return;
          cache.set(descriptor.sha256,rows);if(cache.size>12)cache.delete(cache.keys().next().value);}
        if(current!==epoch||!alive())return;
        offset++;scanned++;loadedPages.add(descriptor.sha256);pendingRefresh.delete(descriptor.sha256);
        for(const row of rows){if(matches(row,filters))selectedRows.set(row.id,row);else selectedRows.delete(row.id);}
        // A rolling browser window; the durable archive remains fully pageable.
        while(selectedRows.size>1500)selectedRows.delete(selectedRows.keys().next().value);
        shown=selectedRows.size;
      }
      draw();
      summary.textContent='Коментарів: '+shown+' · Дописів: '+groupComments(selectedRows.values()).length+
        (offset<pages.length?' · Є ще коментарі':'')+(shown>=1500?' · Вікно: до 1500 коментарів':'');
      more.hidden=offset>=pages.length;
      if(!shown&&more.hidden)list.append(el('p','query-state',catalog.metrics?.comments?'Коментарів за цими умовами поки немає.':'Коментарі ще не надійшли.'));
    }catch(e){if(current===epoch){summary.textContent='Не вдалося прочитати коментарі: '+e.message;more.hidden=false;}}
    finally{if(current===epoch){loading=false;more.disabled=false;}}
  }
  function render(){
    if(!alive())return;epoch++;loading=false;refreshing=false;offset=0;shown=0;selectedRows.clear();visibleLimits.clear();loadedPages.clear();pendingRefresh.clear();
    const section=el('section','cloud-comments');section.append(el('h1','page-header__title','Коментарі'));
    const controls=el('div','filter-row');
    function input(label,key,type='text'){const n=el('input','form-input');n.type=type;n.value=filters[key]||'';n.placeholder=label;n.setAttribute('aria-label',label);n.onchange=()=>{filters[key]=n.value;render();};controls.append(n);}
    function select(label,key,options){const n=el('select','form-select');n.setAttribute('aria-label',label);n.add(new Option(label,''));for(const [v,t] of options)n.add(new Option(t,v));n.value=filters[key]||'';n.onchange=()=>{filters[key]=n.value;render();};controls.append(n);}
    input('Пошук у коментарях','query','search');select('Усі платформи','platform',Object.entries(names));
    select('Усі джерела','source',Object.entries(catalog?.sources||{}));input('Від дати','from','date');input('До дати','to','date');
    select('Будь-яка тональність','sentiment',[['positive','Позитивна'],['neutral','Нейтральна'],['negative','Негативна'],['unknown','Не оцінено']]);
    const reset=el('button','comments-load-more','Скинути фільтри');reset.onclick=()=>{filters={};render();};controls.append(reset);section.append(controls);
    if(filters.post)section.append(el('p','','Обговорення вибраного допису'));
    summary=el('p','query-state__label','Завантаження коментарів…');summary.setAttribute('role','status');list=el('div','cloud-comments__list');more=el('button','comments-load-more','Завантажити наступні коментарі');more.onclick=next;
    section.append(summary,list,more);
    section.append(el('p','query-state__label','Показано зібрані коментарі. Обговорення може містити більше відповідей.'));
    main.replaceChildren(section);
    if(!catalog){summary.textContent='Хмарний модуль ще не опублікував індекс коментарів.';more.hidden=true;return;}
    pages=candidatePages(catalog,filters);void next();
  }
  return {
    async mount(target){main=target;const current=++epoch;main.replaceChildren(el('p','query-state','Завантаження коментарів…'));
      try{const value=await drive.readCommentsCatalog();if(current!==epoch||!alive())return;catalog=value;render();}
      catch(e){if(current===epoch)main.replaceChildren(el('p','query-state','Коментарі тимчасово недоступні: '+e.message));}},
    async refresh(){if(!alive()||loading||refreshing)return;refreshing=true;const current=epoch;
      try{const value=await drive.readCommentsCatalog();if(current!==epoch||!alive()||loading)return;
        if(value?.revision!==catalog?.revision){
          const previous=catalog;catalog=value;
          if(!previous||!value){render();return;}
          const updated=candidatePages(catalog,filters);
          if(!sameCommentPages(previous.pages,value.pages)){
            const previousHashes=new Set(previous.pages.map(p=>p.sha256)),retained=new Set(updated.map(p=>p.sha256));
            for(const hash of pendingRefresh)if(!retained.has(hash))pendingRefresh.delete(hash);
            for(const p of updated)if(!previousHashes.has(p.sha256))pendingRefresh.add(p.sha256);
            // A newly collected reply can be on an old post or an unread shard.
            // Drain those changed pages first, including on later unchanged polls.
            pages=[...updated.filter(p=>pendingRefresh.has(p.sha256)),...updated.filter(p=>!pendingRefresh.has(p.sha256))];offset=0;
            for(const hash of loadedPages)if(!retained.has(hash))loadedPages.delete(hash);
          }
        }
        if(pendingRefresh.size)await next({live:true});
      }finally{if(current===epoch)refreshing=false;}},
    clear(){epoch++;main=null;catalog=null;cache.clear();loadedPages.clear();pendingRefresh.clear();selectedRows.clear();visibleLimits.clear();filters={};loading=false;refreshing=false;}
  };
}
