import {StreamHealth} from './tv-core.mjs';
const scripts=new Map();
function loadScript(url,ready) {
  if(ready())return Promise.resolve();
  if(scripts.has(url))return scripts.get(url);
  const promise=new Promise((resolve,reject)=>{
    const s=document.createElement('script');s.src=url;s.async=true;
    const end=(error)=>{clearTimeout(timer);clearInterval(poll);if(error){s.remove();scripts.delete(url);reject(error);}else resolve();};
    const timer=setTimeout(()=>end(new Error('Плеєр не завантажився')),20000);
    const poll=setInterval(()=>{if(ready())end();},100);
    s.onerror=()=>end(new Error('Мережа блокує бібліотеку плеєра'));document.head.append(s);
  });scripts.set(url,promise);return promise;
}
const ytReady=()=>loadScript('https://www.youtube.com/iframe_api',()=>!!window.YT?.Player);
const hlsReady=()=>loadScript(new URL('./tv-hls.js',import.meta.url).href,()=>!!window.Hls);
const dashReady=()=>loadScript(new URL('./tv-shaka.js',import.meta.url).href,()=>!!window.shaka?.Player);
export class TVPlayer {
  constructor({host,channel,onState,onSuccess,onAudioIntent,preferredSource,lastSuccess,volume=50}) {
    Object.assign(this,{host,channel,onState,onSuccess,onAudioIntent,volume});this.muted=true;this.epoch=0;this.destroyed=false;this.sourceIndex=0;
    this.sources=[...channel.sources];const index=this.sources.findIndex(s=>s.id===preferredSource);
    if(index>0)this.sources.unshift(...this.sources.splice(index,1));
    this.health=new StreamHealth({emit:value=>onState({...value,sourceId:this.sources[this.sourceIndex]?.id}),reconnect:()=>{this.sourceIndex=(this.sourceIndex+1)%Math.max(1,this.sources.length);void this.connect();}});
    if(Number.isFinite(Date.parse(lastSuccess)))this.health.lastSuccess=Date.parse(lastSuccess);
    this.cleanups=[];void this.connect();
  }
  cleanup(){this.epoch++;for(const fn of this.cleanups.splice(0)){try{fn();}catch{}}this.media=null;this.youtube=null;this.hls=null;this.shaka=null;this.host.replaceChildren();}
  async connect() {
    this.cleanup();if(this.destroyed)return;const epoch=this.epoch;
    const source=this.sources[this.sourceIndex];
    if(!source){this.health.state(this.channel.status==='RESTRICTED'?'RESTRICTED':'UNKNOWN',this.channel.reason||'Дозволене вбудоване джерело не підтверджено');return;}
    this.health.state('CONNECTING','З’єднання з офіційним джерелом');
    const active=()=>!this.destroyed&&epoch===this.epoch;
    const fail=(message,restricted=false)=>{if(!active())return;
      if(restricted&&this.sourceIndex+1<this.sources.length){this.sourceIndex++;void this.connect();return;}
      this.cleanup();this.health.failure(message,{restricted});};
    const success=()=>{if(!active())return;this.health.connected();this.onSuccess?.(source.id);};
    let lastProgress=Date.now(),previous=-1,confirmedLive=false,confirmYoutube=()=>{};
    const audioWatch=setInterval(()=>{if(active()&&this.youtube&&this.muted&&this.youtube.isMuted?.()===false)this.onAudioIntent?.();},500);
    this.cleanups.push(()=>clearInterval(audioWatch));
    const watchdog=setInterval(()=>{
      if(!active())return;
      if(['PAUSED','WAITING','RESTRICTED','OFFLINE'].includes(this.health.status))return;
      const position=this.youtube?.getCurrentTime?.()??this.media?.currentTime??-1;
      if(position>previous+.05){previous=position;lastProgress=Date.now();if(!confirmedLive&&this.youtube?.getPlayerState?.()===1)confirmYoutube();if(confirmedLive&&this.health.status!=='LIVE')success();this.health.progress();}
      else if(Date.now()-lastProgress>35000)fail('Потік не просувається');
    },5000);this.cleanups.push(()=>clearInterval(watchdog));
    try {
      if(source.type==='youtube') {
        await ytReady();if(!active())return;
        const iframe=document.createElement('iframe');iframe.title=this.channel.name+' — прямий ефір';
        const params=new URLSearchParams({enablejsapi:'1',autoplay:'1',mute:'1',playsinline:'1',controls:'1',origin:location.origin,rel:'0'});
        // A reviewed broadcaster video is only a candidate: playback must independently prove LIVE.
        const fresh=source.verifiedLiveAt&&Date.now()-Date.parse(source.verifiedLiveAt)<1200000;
        const videoId=fresh?source.videoId:source.candidateVideoId;
        if(!videoId)params.set('channel',source.channelId);
        iframe.src='https://www.youtube.com/embed/'+(videoId||'live_stream')+'?'+params;
        iframe.allow='autoplay; encrypted-media; picture-in-picture; fullscreen';iframe.allowFullscreen=true;
        iframe.referrerPolicy='strict-origin-when-cross-origin';this.host.append(iframe);
        const player=new YT.Player(iframe,{events:{
          onReady:()=>{if(active()){player.mute();player.setVolume(this.volume);player.playVideo();if(player.getPlayerState?.()===1)confirmYoutube();}},
          onAutoplayBlocked:()=>{if(active())this.health.state('WAITING','Натисніть Play у плеєрі');},
          onError:e=>fail(({100:'YouTube: ефір недоступний або приватний',101:'YouTube: мовник заборонив вбудовування; відкрийте «Офіційний ефір»',150:'YouTube: мовник заборонив вбудовування; відкрийте «Офіційний ефір»',153:'YouTube: не вдалося перевірити сайт вбудовування'})[e.data]||'YouTube: '+e.data,[100,101,150,153].includes(e.data)),
          onStateChange:e=>{
            if(!active())return;
            if(e.data===1)confirmYoutube();
            else if(e.data===0){delete source.videoId;delete source.candidateVideoId;delete source.verifiedLiveAt;fail('LIVE-сесію завершено; пошук актуального ефіру');}
            else if(e.data===2)this.health.state('PAUSED','На паузі');
            else if(e.data===3){lastProgress=Date.now();this.health.state('BUFFERING','Буферизація');}
          }
        }});
        confirmYoutube=()=>{if(!active())return;
              const data=player.getVideoData?.()||{};
              if(data.isLive===false){fail('Завершений ефір не відтворюється');return;}
              confirmedLive=data.isLive===true||(fresh&&data.video_id===source.videoId);
              if(confirmedLive)success();else {player.pauseVideo();this.health.state('UNKNOWN','LIVE не підтверджено; відтворення призупинено');}
              this.setAudio(this.muted,this.volume);lastProgress=Date.now();
        };this.youtube=player;this.cleanups.push(()=>player.destroy());
      } else if(source.type==='iframe') {
        const iframe=document.createElement('iframe');iframe.src=source.url;iframe.title=this.channel.name;
        iframe.allow='fullscreen; encrypted-media; picture-in-picture';iframe.allowFullscreen=true;
        iframe.referrerPolicy='strict-origin-when-cross-origin';this.host.append(iframe);
        clearInterval(watchdog);this.health.state('UNKNOWN','Офіційний плеєр: статус і звук контролює мовник');
      } else {
        const video=document.createElement('video');video.controls=true;video.autoplay=true;video.muted=true;video.playsInline=true;
        video.preload='metadata';video.setAttribute('aria-label',this.channel.name);this.media=video;this.host.append(video);
        this.cleanups.push(()=>{video.pause();video.removeAttribute('src');video.load();});
        video.onplaying=()=>{if(confirmedLive)success();lastProgress=Date.now();};
        video.onwaiting=()=>{if(active())this.health.state('BUFFERING','Буферизація');};
        video.onpause=()=>{if(active())this.health.state('PAUSED','На паузі');};
        video.onended=()=>fail('Прямий ефір завершено');video.onerror=()=>fail('Помилка мережі або декодування');
        video.onvolumechange=()=>{if(active()&&this.muted&&!video.muted)this.onAudioIntent?.();};
        const play=()=>video.play().catch(()=>{if(active())this.health.state('WAITING','Натисніть Play у плеєрі');});
        if(source.type==='hls') {
          await hlsReady();if(!active())return;
          if(window.Hls.isSupported()) {
            const hls=new Hls({lowLatencyMode:true,backBufferLength:15,maxBufferLength:20,capLevelToPlayerSize:true,
              manifestLoadingMaxRetry:1,levelLoadingMaxRetry:1,fragLoadingMaxRetry:1});this.hls=hls;
            this.cleanups.push(()=>hls.destroy());
            hls.on(Hls.Events.ERROR,(_,data)=>{if(data.fatal)fail('HLS: '+data.details,[401,403,451].includes(data.response?.code));});
            hls.on(Hls.Events.LEVEL_LOADED,(_,data)=>{if(!data.details.live)fail('Запис або завершений HLS не відтворюється');else {confirmedLive=true;if(!video.paused)success();}});
            hls.on(Hls.Events.MANIFEST_PARSED,play);hls.attachMedia(video);hls.loadSource(source.url);
          } else if(video.canPlayType('application/vnd.apple.mpegurl')) {
            const controller=new AbortController();this.cleanups.push(()=>controller.abort());
            let url=source.url,depth=0;
            const check=async()=>{
              const response=await fetch(url,{signal:controller.signal,credentials:'omit'});if(!response.ok)throw new Error('HLS HTTP '+response.status);
              const manifest=await response.text();
              if(manifest.includes('#EXT-X-STREAM-INF')){if(++depth>3)throw new Error('Циклічний HLS маніфест');const lines=manifest.split(/\r?\n/);const i=lines.findIndex(l=>l.startsWith('#EXT-X-STREAM-INF'));url=new URL(lines[i+1],url).href;return check();}
              if(!manifest.startsWith('#EXTM3U')||!manifest.includes('#EXTINF')||manifest.includes('#EXT-X-ENDLIST'))throw new Error('Немає активного LIVE HLS');
              confirmedLive=true;
            };
            await check();if(!active())return;video.src=source.url;void play();
            const timer=setInterval(()=>check().catch(e=>fail(e.message)),30000);this.cleanups.push(()=>clearInterval(timer));
          } else fail('Браузер не підтримує HLS',true);
        } else {
          await dashReady();if(!active())return;shaka.polyfill.installAll();
          if(!shaka.Player.isBrowserSupported()){fail('Браузер не підтримує MPEG-DASH',true);return;}
          const player=new shaka.Player();this.shaka=player;this.cleanups.push(()=>void player.destroy());
          await player.attach(video);if(!active())return;
          player.configure({streaming:{lowLatencyMode:true,bufferingGoal:12,retryParameters:{maxAttempts:2}},abr:{enabled:true}});
          player.addEventListener('error',e=>fail('DASH: '+e.detail.code));
          await player.load(source.url);if(!active())return;
          if(!player.isLive()){fail('DASH-запис не відтворюється');return;}confirmedLive=true;void play();
        }
      }
    } catch(error){if(active())fail(error.message);}
  }
  setAudio(muted,volume=this.volume){this.muted=muted;this.volume=volume;
    if(this.media){this.media.muted=muted;this.media.volume=volume/100;}
    if(this.youtube){this.youtube.setVolume?.(volume);if(muted)this.youtube.mute?.();else this.youtube.unMute?.();}}
  qualities(){return this.hls?this.hls.levels.map((l,i)=>({value:String(i),label:l.height+'p'})):
    this.shaka?this.shaka.getVariantTracks().map(t=>({value:String(t.id),label:t.height+'p'})):[];}
  quality(value){if(this.hls)this.hls.currentLevel=Number(value);
    if(this.shaka){this.shaka.configure({abr:{enabled:value==='-1'}});const track=this.shaka.getVariantTracks().find(t=>String(t.id)===value);if(track)this.shaka.selectVariantTrack(track,true);}}
  retry(){this.health.manual();}
  selectSource(id){const index=this.sources.findIndex(s=>s.id===id);if(index<0)return;this.sourceIndex=index;this.health.manual(()=>void this.connect());}
  suspend(){this.cleanup();this.health.dispose();this.onState({status:'PAUSED',reason:'Призупинено поза видимою областю'});}
  destroy(){this.destroyed=true;this.health.dispose();this.cleanup();}
}
