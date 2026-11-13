const formatter = new Intl.DateTimeFormat('en-CA',{timeZone:'Europe/Kyiv',year:'numeric',month:'2-digit',day:'2-digit'});
export function publicationTime(value) {
  if(typeof value!=='string'||!/^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value))return NaN;
  return Date.parse(value);
}
export function publicationDay(value) {
  const time=publicationTime(value);if(!Number.isFinite(time))return '';
  const parts=Object.fromEntries(formatter.formatToParts(time).map(p=>[p.type,p.value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}
export function withinPublicationRange(value,from,to,now) {
  const time=publicationTime(value);
  if(!Number.isFinite(time))return !from&&!to;
  if(time>now)return false;
  const day=publicationDay(value), dateOnly=v=>/^\d{4}-\d{2}-\d{2}$/.test(v);
  return (!from||(dateOnly(from)?day>=from:time>=Date.parse(from))) &&
         (!to||(dateOnly(to)?day<=to:time<=Date.parse(to)));
}
export function continuousDays(days,from,to) {
  const dates=[...days.keys()].sort();if(!dates.length)return [];
  const first=from?(from.length===10?from:publicationDay(from)):dates[0];
  const last=to?(to.length===10?to:publicationDay(to)):dates.at(-1);
  const result=[];
  for(let t=Date.parse(first+'T00:00:00Z'),end=Date.parse(last+'T00:00:00Z');t<=end;t+=86400000){
    const date=new Date(t).toISOString().slice(0,10);
    result.push({date,posts:days.get(date)||0});
  }
  return result;
}
