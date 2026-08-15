const networkPanel=document.querySelector("#network-panel"),networkQr=document.querySelector("#network-qr"),networkUrl=document.querySelector("#network-url"),playniteButton=document.querySelector("#playnite-fullscreen"),playniteStatus=document.querySelector("#playnite-status");
const isDesktop=window.matchMedia("(min-width: 900px) and (hover: hover) and (pointer: fine)").matches;
async function setupNetworkPanel(){
  if(!isDesktop||!networkPanel)return;
  try{
    const r=await fetch("/api/network",{cache:"no-store"});
    if(!r.ok)throw Error("Network information unavailable");
    const d=await r.json();
    if(!d.url)return;
    networkUrl.textContent=d.url;
    networkQr.src=`/api/network/qr?text=${encodeURIComponent(d.url)}`;
    networkPanel.hidden=false;
    if(!d.playnite_configured){
      playniteButton.disabled=true;
      playniteStatus.textContent="Set playnite_fullscreen_path in config.json";
    }
  }catch(e){console.debug("Network panel unavailable",e)}
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