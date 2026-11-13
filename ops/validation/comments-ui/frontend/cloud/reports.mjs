import {topicLabels} from './analysis.mjs';
import {archiveReport} from './archive-report.mjs';
const REVIEW = '—*';
const text = value => typeof value === 'string' ? value : '';
const safeUrl = value => {try {const u=new URL(value);return ['https:','http:'].includes(u.protocol)?u.href:'';}catch{return '';}};
const date = value => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString('uk-UA',{timeZone:'Europe/Kyiv'}) : REVIEW;
const number = value => Number.isFinite(value) ? String(value) : REVIEW;
const staticDaily = '.doc-title,.doc-org,.sec-title,.card-lbl,.top5-label,.post-num,thead th,.rank,.spk-row-lbl,.pn-head,.pn-head-icon,.inno-row-icon,.inno-row-lbl,.partner-row-lbl,.enemy-sec-title,.enemy-row-icon,.enemy-row-lbl,.prop-num,.sm-platform-head,.sm-stat-lbl';
const staticZvit = '.ph-dept,.ph-sub,.ph-title,.ph-num,.sec,.m-lbl,.nt-hd,thead th,.lbl,.ev-n,.risk-hd,.box-hd';

// Keep the source templates, CSS, heading text and DOM element structure.
// Replace EVERY non-label text node before the document can be displayed;
// otherwise the sample template contains plausible but fictitious incidents.
export function cleanTemplate(doc, template) {
  const keep = template === 'daily' ? staticDaily : staticZvit;
  let id = 0;
  const walker = doc.createTreeWalker(doc.body,4), nodes=[];
  while(walker.nextNode()) nodes.push(walker.currentNode);
  const seen = new Set();
  for (const node of nodes) {
    const parent=node.parentElement;
    if (!node.textContent.trim() || parent.closest(keep)) continue;
    const key='field-'+(++id);
    node.textContent=seen.has(parent)?'':REVIEW;
    seen.add(parent);
    parent.dataset.review='true';
    if (!parent.children.length) parent.dataset.reportField=key;
  }
  for (const cell of doc.querySelectorAll('tbody td')) {
    if (!cell.textContent.trim()) {direct(cell,REVIEW);if(!cell.children.length)cell.dataset.reportField='field-'+(++id);cell.dataset.review='true';}
  }
  for (const a of doc.querySelectorAll('a')) {a.removeAttribute('href');a.removeAttribute('target');}
  // These widths/arrows encode fictional metrics in the original example.
  for (const bar of doc.querySelectorAll('.spk-bar,.sent-pos,.sent-neu,.sent-neg')) bar.style.width='0%';
  for (const arrow of doc.querySelectorAll('.ad')) arrow.textContent='';
  for (const img of doc.querySelectorAll('img')) {
    if (img.getAttribute('src') === 'dsns-emblem.svg') img.setAttribute('src',new URL('./report-templates/dsns-emblem.svg',location.href).href);
    else img.removeAttribute('src');
  }
  // The original Zvit footer attributes data to YouScan; this is a data-source
  // field, not a heading. Never misattribute ARGUS statistics to another system.
  set(doc,'.fn','*За даними архіву ARGUS. Оцінки TypeSafe AI потребують перевірки.');
  set(doc,'.nt-foot','Наративне кодування та середні показники потребують окремої перевірки.');
  return doc;
}
function direct(el,value) {
  if (!el) return;
  const nodes=[...el.childNodes].filter(n=>n.nodeType===3);
  if (nodes.length) {nodes[0].textContent=String(value);for(const n of nodes.slice(1)) n.textContent='';}
  else el.append(el.ownerDocument.createTextNode(String(value)));
  if (!el.children.length) el.dataset.reportField ||= 'value-'+Math.random().toString(36).slice(2);
  el.dataset.review=String(value===REVIEW);
}
function set(root,selector,value,index=null) {
  const matches=[...root.querySelectorAll(selector)];
  for(const el of index===null?matches:[matches[index]]) direct(el,value);
}
function sourceLink(root,selector,post) {
  const a=root.querySelector(selector), url=safeUrl(post?.url);
  if(!a || !url) return;
  direct(a,text(post.source_name)||'Джерело ARGUS');a.href=url;a.target='_blank';a.rel='noopener noreferrer';
}
function excerpt(post) {
  const value=text(post?.text).trim();
  return value ? 'У джерелі: «'+value.slice(0,360)+(value.length>360?'…':'')+'»' : REVIEW;
}
function fillCards(doc,selector,ids,items,fields) {
  [...doc.querySelectorAll(selector)].forEach((card,i)=>{
    const post=items.get(ids[i]);if(!post)return;
    for(const [target,kind] of Object.entries(fields)) set(card,target,kind==='date'?date(post.published_at):kind==='source'?(text(post.source_name)||REVIEW):excerpt(post));
    sourceLink(card,'a',post);
  });
}
export function fillReport(doc,template,report,snapshot) {
  cleanTemplate(doc,template);
  const s=report.statistics, items=new Map(snapshot.items.map(p=>[p.id,p]));
  const percent = value => value == null ? REVIEW : `${value}%`;
  const period=(report.filters.start==null?'Увесь архів':date(new Date(report.filters.start*1000).toISOString()))+' — '+date(new Date(report.filters.end*1000).toISOString());
  if(template==='daily') {
    set(doc,'.doc-date','Період: '+period+' | Підготовлено: '+date(report.generated_at)+' | * — перевірити');
    // Media-only mention counts and reach are not present in the archive.
    // Leave these exact fields for verification instead of relabeling them.
    set(doc,'.card-val',percent(s.positive_neutral_percent),1);
    set(doc,'.card-val',percent(s.negative_percent),2);
    const recent=!report.topics?.length && report.recent?.length;
    if(recent) set(doc,'.top5-label','Останні публікації за вибраний період',0);
    [...doc.querySelectorAll('.post-card')].forEach((card,i)=>{
      const topic=report.topics?.[i], p=items.get(recent?report.recent[i]:topic?.post_id);if(!p)return;
      set(card,'.post-title',recent?(text(p.title)||'Публікація архіву'):(topicLabels[topic.topic] || REVIEW));
      set(card,'.post-text',excerpt(p));set(card,'.post-source',text(p.source_name)||p.platform);
      set(card,'.post-date',date(p.published_at));set(card,'.post-mentions',recent?p.platform:topic.count+' публікацій');
      set(card,'.post-footer span:last-child',safeUrl(p.url)?new URL(p.url).hostname:REVIEW);
      const source=card.querySelector('.post-footer span:last-child');if(source)source.title=safeUrl(p.url);
    });
    const columns=doc.querySelectorAll('.pn-col');
    for(const [i,key] of [[0,'positive'],[1,'negative']]) if(columns[i]) fillCards(columns[i],'.pn-card',report.evidence[key],items,{'.pn-card-title':'excerpt','.pn-card-text':'excerpt','.pn-date':'date'});
    fillCards(doc,'.inno-card',report.evidence.innovation,items,{'.inno-title':'excerpt'});
    fillCards(doc,'.partner-card',report.evidence.partners,items,{'.partner-org':'source'});
    const crisis=[...new Set([...report.evidence.critical,...report.evidence.complaints])];
    fillCards(doc,'.crisis-card',crisis,items,{'.crisis-quote':'excerpt'});
    set(doc,'.pn-priority',`Оцінено ${s.assessed} з ${s.posts} публікацій. `+(s.assessed?`Серед оцінених: критичні — ${s.critical}, скарги — ${s.complaints}, сигнали резонансу — ${s.resonance}.`:'Оцінок TypeSafe для цієї вибірки поки немає.'));
    for(const [i,key,label] of [[0,'positive','Позитивний'],[1,'neutral','Нейтральний'],[2,'negative','Негативний']])
      set(doc,'.sent-lbl',`${label} — ${s.sentiment_classified ? s[key]+' / '+s.sentiment_classified : REVIEW}`,i);
  } else {
    set(doc,'.ph-date',period+' | * — перевірити');
    set(doc,'.m-val',number(s.posts),1);set(doc,'.m-val',number(s.engagement),2);
    set(doc,'.m-val',percent(s.negative_percent),3);
    set(doc,'.m-val',s.sentiment_classified ? number(s.negative) : REVIEW,4);
    // Distinct incidents, disinformation and named-person activity have no
    // verified structured evidence. Their existing slots remain marked.
    const risks=[...new Set([...report.evidence.critical,...report.evidence.complaints])];
    [...doc.querySelectorAll('.ri')].forEach((el,i)=>{const p=items.get(risks[i]);if(p)direct(el,excerpt(p)+' '+safeUrl(p.url));});
  }
  return doc;
}

export function createReportsController(loadInputs) {
  let snapshot=null, analysis=null, session=0, mountedTitle, refreshChoices;
  const drafts=new Map(), templateEdits=new Map();
  return {
    update(s,a) {snapshot=s;analysis=a;if(mountedTitle?.isConnected) refreshChoices?.();},
    isMounted() {return !!mountedTitle?.isConnected;},
    clear() {snapshot=null;analysis=null;refreshChoices=null;mountedTitle=null;drafts.clear();templateEdits.clear();session++;},
    mount(main) {
      const current=session;
      const el=(tag,cls,value)=>{const e=document.createElement(tag);e.className=cls||'';if(value!=null)e.textContent=value;return e;};
      const title=el('h1','page-header__title','Аналітична довідка');main.append(title);mountedTitle=title;
      if(!snapshot) {main.append(el('p','empty-state','Увійдіть, щоб відкрити архів.'));return;}
      const choices=field=>[...new Set(snapshot.items.filter(p=>!p.sample).map(p=>p[field]).filter(Boolean))].sort().map(v=>[v,v]);
      const grid=el('div','reports-new-grid'), panel=el('div','report-param-panel'), card=el('div','section-card');
      const preview=el('div','section-card'), header=el('div','report-preview-header'), tools=el('div','report-preview-header__toolbar');
      header.append(el('div','report-preview-header__title','Попередній перегляд довідки'),tools);preview.append(header);
      const notice=el('p','report-summary-box');preview.append(notice);
      const frame=el('iframe','report-preview-frame');frame.title='Попередній перегляд довідки';frame.style.cssText='width:100%;min-height:1150px;border:0';
      frame.setAttribute('sandbox','allow-same-origin allow-modals');preview.append(frame);
      card.style.padding='20px';card.append(el('div','report-param-panel__title','Параметри довідки'));
      panel.append(card);grid.append(preview,panel);main.append(grid);
      const choose=(label,values)=>{const wrap=el('label','form-field'),select=el('select','form-input');wrap.style.cssText='display:flex;flex-direction:column;gap:6px;margin:14px 0';select.style.cssText='width:100%;padding:8px';wrap.append(el('span','form-field__label',label),select);for(const [id,name] of values){const o=el('option','',name);o.value=id;select.append(o);}card.append(wrap);return select;};
      const template=choose('Шаблон',[['daily','Щоденна аналітична довідка'],['zvit','Моніторинг відкритих джерел']]);
      const period=choose('Період',[['7d','7 днів'],['today','Сьогодні'],['24h','24 години'],['48h','48 годин'],['30d','30 днів'],['all','Весь період']]);
      const region=choose('Регіон',[['all','Усі регіони']]);
      const platform=choose('Платформа джерел',[['all','Усі платформи']]);
      const origin=choose('Джерела',[['all','Усі'],['dsns','Тільки офіційні ресурси ДСНС']]);
      const customPeriod=f=>'custom:'+f.start+':'+f.end;
      const addChoices=(select,values)=>{
        const known=new Set([...select.options].map(o=>o.value));
        for(const [id,label] of values) if(!known.has(id)) {select.add(new Option(label,id));known.add(id);}
      };
      refreshChoices=()=>{
        addChoices(region,choices('region'));addChoices(platform,choices('platform'));
        const custom=(analysis?.reports||[]).map(r=>r.filters).filter(f=>f.period==='custom');
        addChoices(period,custom.map(f=>[customPeriod(f),f.customFrom+' — '+f.customTo]));
      };
      refreshChoices();
      const button=(parent,label,fn)=>{const b=el('button','report-toolbar-btn',label);b.onclick=fn;parent.append(b);return b;};
      let active=null, activeKey='', editable=false, ready=false, request=0;
      const capture=()=>{if(ready && active) drafts.set(activeKey,{html:frame.contentDocument.documentElement.outerHTML,status:active.status,report:active.report});};
      const editState=()=>{
        edit.disabled=!ready || active?.status==='approved';
        edit.textContent=editable?'Завершити редагування':'Редагувати';
        edit.setAttribute('aria-pressed',String(editable));
        for(const field of frame.contentDocument?.querySelectorAll('[data-report-field]')||[])field.contentEditable=editable?'plaintext-only':'false';
      };
      const begin=()=>{
        capture();request++;active=null;ready=false;editable=false;
        edit.disabled=approve.disabled=print.disabled=true;edit.textContent='Редагувати';edit.setAttribute('aria-pressed','false');
        frame.hidden=true;return request;
      };
      const loadTemplate=async name=>{
        const response=await fetch('./report-templates/'+name+'.html',{credentials:'omit'});
        if(!response.ok)throw new Error('Шаблон недоступний. Оновіть сторінку та спробуйте ще раз.');
        return new DOMParser().parseFromString(await response.text(),'text/html');
      };
      const show=(doc,seq,applyEdits)=>{
        // Both template and generated document have the same element order.
        // Stable addresses let a heading, table cell or mixed-text row retain
        // its edits without making the row's markup itself editable.
        if(!doc.documentElement.hasAttribute('data-editor-fields')) {
          [...doc.body.querySelectorAll('*')].forEach((el,index)=>{
            if(el.closest('script,style,svg,template'))return;
            if(!el.children.length) {
              if(el.textContent.trim() || el.matches('td,th,[data-report-field]'))
                el.dataset.reportField='field-'+index;
              return;
            }
            [...el.childNodes].forEach((node,i)=>{
              if(node.nodeType!==3 || !node.textContent.trim())return;
              const field=doc.createElement('span');field.dataset.reportField='text-'+index+'-'+i;
              if(el.hasAttribute('data-review'))field.dataset.review=el.dataset.review;
              node.replaceWith(field);field.append(node);
            });
            if(el.querySelector(':scope > [data-report-field]'))el.removeAttribute('data-review');
          });
          doc.documentElement.dataset.editorFields='true';
        }
        const edits=applyEdits && templateEdits.get(template.value);
        if(edits) for(const field of doc.querySelectorAll('[data-report-field]')) {
          if(edits.has(field.dataset.reportField)) direct(field,edits.get(field.dataset.reportField));
        }
        doc.documentElement.dataset.previewRequest=String(seq);
        if(!doc.getElementById('report-edit-style')) {
          const style=doc.createElement('style');style.id='report-edit-style';
          style.textContent='[contenteditable="plaintext-only"]{outline:1px dashed #687e99;cursor:text;min-width:1ch}[contenteditable="plaintext-only"]:focus{outline:2px solid #175cd3;background:#fff;color:#172b4d}';
          doc.head.append(style);
        }
        frame.srcdoc=doc.documentElement.outerHTML;frame.hidden=false;
      };
      frame.onload=()=>{
        const doc=frame.contentDocument;
        if(current!==session || !title.isConnected || doc?.documentElement?.dataset.previewRequest!==String(request))return;
        ready=true;editState();approve.disabled=!active || active.status==='approved';print.disabled=false;
        doc.addEventListener('input',event=>{
          const field=event.target;
          if(!editable || !field.dataset.reportField)return;
          field.dataset.review=String(!field.textContent.trim()||field.textContent.includes(REVIEW));
          if(active)capture();
          else {
            if(!templateEdits.has(template.value))templateEdits.set(template.value,new Map());
            templateEdits.get(template.value).set(field.dataset.reportField,field.textContent);
          }
        });
      };
      const generate=async()=>{
        const seq=begin();
        notice.textContent='Читаємо актуальний архів…';
        try {
          const inputs=loadInputs?await loadInputs():{snapshot,analysis};
          if(current!==session||seq!==request||!title.isConnected)return;
          const selection={period:period.value,platform:platform.value,region:region.value,dsns:origin.value==='dsns'};
          let r;
          if(period.value.startsWith('custom:')) {
            r=inputs.analysis?.reports.find(r=>r.filters.period==='custom' && customPeriod(r.filters)===period.value &&
              r.filters.platform===selection.platform && r.filters.region===selection.region && r.filters.dsns===selection.dsns);
            if(!r)throw new Error('В архіві немає актуальної довідки для цих параметрів.');
          } else r=archiveReport(inputs.snapshot,inputs.analysis,selection);
          const digest=inputs.snapshot.snapshot_sha256 || inputs.analysis?.snapshot_sha256 || null;
          const key=JSON.stringify([digest,template.value,[...(templateEdits.get(template.value)||[])],selection,inputs.analysis?.updated_at,
            r.statistics,r.recent,r.filters.period==='custom'?[r.filters.start,r.filters.end]:null]);
          const saved=drafts.get(key);
          if(saved) r=saved.report;
          const doc=saved?new DOMParser().parseFromString(saved.html,'text/html'):
            fillReport(await loadTemplate(template.value),template.value,r,inputs.snapshot);
          if(current!==session||seq!==request||!title.isConnected)return;
          active={report:r,snapshot:inputs.snapshot,digest,status:saved?.status||'draft'};activeKey=key;show(doc,seq,!saved);
          notice.textContent=inputs.warning || '';
        }catch(e){if(seq===request && current===session){active=null;frame.removeAttribute('srcdoc');notice.textContent=e.message;}}
      };
      button(card,'Згенерувати довідку',generate);
      const edit=button(tools,'Редагувати',()=>{editable=!editable;editState();capture();});
      const print=button(tools,'Друк',()=>{if(ready)frame.contentWindow.print();});
      const approve=button(tools,'Затвердити та завантажити',()=>{
        if(!active||!ready)return;
        const doc=frame.contentDocument;
        // Approval records review of the complete draft, including visibly
        // unresolved fields; no AI inference is promoted to a verified fact.
        active.status='approved';editable=false;editState();capture();
        const sourceIds=new Set([...Object.values(active.report.evidence).flat(),...(active.report.topics||[]).map(t=>t.post_id),...(active.report.recent||[])]);
        const artifact={format:'argus-reviewed-report-v1',status:'approved',approved_at:new Date().toISOString(),
          snapshot_sha256:active.digest,report:active.report,html:doc.documentElement.outerHTML,
          sources:active.snapshot.items.filter(p=>sourceIds.has(p.id))
            .map(p=>({id:p.id,url:safeUrl(p.url),published_at:p.published_at,text:p.text})),
          unresolved_fields:[...doc.querySelectorAll('[data-review="true"]')].map(e=>e.dataset.reportField||e.className)};
        const url=URL.createObjectURL(new Blob([JSON.stringify(artifact,null,2)],{type:'application/json'}));
        const a=el('a');a.href=url;a.download='argus-report-reviewed.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
        notice.textContent='Затверджений знімок завантажено. Файл містить джерела, параметри розрахунку та поля для перевірки.';
        approve.disabled=true;
      });
      const resetPreview=async()=>{
        const seq=begin(), name=template.value;
        notice.textContent='Завантажуємо шаблон…';
        try {
          const doc=await loadTemplate(name);
          if(seq!==request || current!==session || !title.isConnected)return;
          show(cleanTemplate(doc,name),seq,true);
          notice.textContent='Це бланк довідки. Натисніть «Редагувати», щоб змінити поля, або «Згенерувати довідку», щоб заповнити їх даними архіву.';
        } catch(e) {if(seq===request && current===session)notice.textContent=e.message;}
      };
      for(const select of [template,period,region,platform,origin])select.onchange=resetPreview;
      resetPreview();
    }
  };
}
