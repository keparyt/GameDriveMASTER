const networkPanel=document.querySelector("#network-panel"),networkQr=document.querySelector("#network-qr"),networkQrWrap=document.querySelector(".network-qr-wrap"),networkUrl=document.querySelector("#network-url"),playniteButton=document.querySelector("#playnite-fullscreen"),playniteStatus=document.querySelector("#playnite-status"),playnitePromptTitle=document.querySelector("#playnite-prompt-title"),playnitePromptCopy=document.querySelector("#playnite-prompt-copy");
const isDesktop=window.matchMedia("(min-width: 900px) and (hover: hover) and (pointer: fine)").matches;
const GAME_LIBRARY_PORT=8765;
function phoneUrl(){
  const host=window.location.hostname;
  return `http://${host}:${GAME_LIBRARY_PORT}`;
}
async function setupNetworkPanel(){
  if(!networkPanel)return;
  networkPanel.hidden=false;
  if(isDesktop){
    if(networkQrWrap)networkQrWrap.style.display="";
    if(playnitePromptTitle)playnitePromptTitle.textContent="Use Playnite to launch your games";
    if(playnitePromptCopy)playnitePromptCopy.textContent="For the best experience on this PC, open Playnite Fullscreen. On your phone? Scan the QR code to open GameDrive on your phone.";
    const url=phoneUrl();
    networkUrl.textContent=url;
    if(networkQr){
      networkQr.alt=`QR code to open ${url} on your phone`;
      networkQr.src=`/api/network/qr?text=${encodeURIComponent(url)}`;
      networkQr.onerror=()=>{networkQrWrap.classList.add("qr-error");networkQr.alt="QR code unavailable"};
      networkQr.onload=()=>networkQrWrap.classList.remove("qr-error");
    }
    try{
      const r=await fetch("/api/network",{cache:"no-store"});
      if(r.ok){
        const d=await r.json();
        if(d.playnite_configured===false){
          playniteButton.disabled=true;
          playniteStatus.textContent="Set playnite_fullscreen_path in config.json";
        }
      }
    }catch(e){console.debug("Playnite status unavailable",e)}
  }else{
    if(networkQrWrap)networkQrWrap.style.display="none";
    if(playnitePromptTitle)playnitePromptTitle.textContent="Use Playnite for the best experience";
    if(playnitePromptCopy)playnitePromptCopy.textContent="If this page is open on your PC, use Playnite Fullscreen to browse and launch games. From your phone, you can use GameDrive as the remote library and launcher.";
    networkUrl.textContent="";
    try{
      const r=await fetch("/api/network",{cache:"no-store"});
      if(r.ok){
        const d=await r.json();
        if(!d.playnite_configured){
          playniteButton.disabled=true;
          playniteStatus.textContent="Playnite Fullscreen is not configured on this PC";
        }
      }
    }catch(e){console.debug("Playnite status unavailable",e)}
  }
}
if(playniteButton)playniteButton.addEventListener("click",async()=>{
  playniteButton.disabled=true;playniteStatus.textContent="Opening Playnite…";
  try{
    const r=await fetch("/api/playnite/fullscreen",{method:"POST"}),d=await r.json();
    if(!r.ok||!d.ok)throw Error(d.error||`HTTP ${r.status}`);
    playniteStatus.textContent="Playnite Fullscreen started";
  }catch(e){playniteStatus.textContent=`Unable to open: ${e.message}`}
  finally{setTimeout(()=>{playniteButton.disabled=false},1200)}
});
setupNetworkPanel();
