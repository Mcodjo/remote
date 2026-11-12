/* Outils communs LanShare */
const ic=(n,c='')=>`<svg class="ic ${c}" aria-hidden="true"><use href="/web/icons.svg#${n}"/></svg>`;
function gathered(pc){return new Promise(res=>{
  if(pc.iceGatheringState==='complete')return res();
  const t=setTimeout(res,2500);
  pc.addEventListener('icegatheringstatechange',()=>{if(pc.iceGatheringState==='complete'){clearTimeout(t);res();}});
});}
// Les navigateurs masquent l'IP locale derrière « xxxx.local » (mDNS) : on la remplace par l'IP réelle.
function fixSdp(sdp,ip){return sdp.replace(/(a=candidate:\S+ \d+ \w+ \d+ )\S+\.local( \d+ typ)/g,'$1'+ip+'$2');}
function post(body){return fetch('/signal',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});}
/* Copie avec retour visuel + annonce lecteur d'écran */
function copyText(text,btn){
  const done=()=>{const html=btn.innerHTML;btn.classList.add('ok');btn.innerHTML=ic('check')+'<span>Copié</span>';
    const live=document.getElementById('live-msg');if(live)live.textContent='Copié dans le presse-papiers';
    setTimeout(()=>{btn.classList.remove('ok');btn.innerHTML=html;},1600);};
  const fb=()=>{const t=document.createElement('textarea');t.value=text;document.body.appendChild(t);t.select();try{document.execCommand('copy');}catch(_){}t.remove();done();};
  (navigator.clipboard&&window.isSecureContext?navigator.clipboard.writeText(text).then(done,fb):fb());
}

/* Aide la montée en débit de l'émetteur (par défaut Chrome démarre à ~300 kb/s et met des secondes à gagner en netteté) */
function boostSdp(sdp,startK=12000,minK=4000,maxK=30000){
  return sdp.replace(/a=rtpmap:(\d+) (VP8|VP9|H264|AV1)\/90000\r?\n/g,(m,pt)=>{
    const add=`x-google-start-bitrate=${startK};x-google-min-bitrate=${minK};x-google-max-bitrate=${maxK}`;
    const re=new RegExp('a=fmtp:'+pt+' ');
    return re.test(sdp)?m:m+`a=fmtp:${pt} ${add}\r\n`;})
   .replace(/(a=fmtp:\d+ (?!.*x-google)[^\r\n]*)/g,(m)=>/apt=/.test(m)?m:m+`;x-google-start-bitrate=${startK};x-google-min-bitrate=${minK};x-google-max-bitrate=${maxK}`);
}
