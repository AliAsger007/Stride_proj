const $=s=>document.querySelector(s);let slot=null;$("#yr").textContent=new Date().getFullYear();
$("#burger").onclick=()=>$("#links").classList.toggle("open");
$("#links").onclick=()=>$("#links").classList.remove("open");
// scroll reveal + count-up
const io=new IntersectionObserver(es=>es.forEach(e=>{if(e.isIntersecting){e.target.classList.add("in");io.unobserve(e.target)}}),{threshold:.15});
document.querySelectorAll(".rv").forEach(el=>io.observe(el));
document.querySelectorAll("[data-n]").forEach(el=>{const n=+el.dataset.n,t0=performance.now();
 (function f(t){const p=Math.min((t-t0)/1800,1);el.textContent=Math.round(n*(1-Math.pow(1-p,3))).toLocaleString()+el.dataset.s;if(p<1)requestAnimationFrame(f)})(t0)});
// service shortcut
document.querySelectorAll(".book-link").forEach(a=>a.onclick=()=>{$("#svc").value=a.dataset.svc});
// live slots
function show(t,c){const m=$("#msg");m.className=c;m.textContent=t;m.style.display="block"}
$("#day").onchange=async()=>{slot=null;const box=$("#slots");box.innerHTML="<small>Loading...</small>";
 try{const r=await fetch("/api/slots?date="+$("#day").value);const j=await r.json();
  box.innerHTML=j.slots&&j.slots.length?"":"<small style='color:var(--mut)'>No slots that day (Sundays closed). Try another.</small>";
  (j.slots||[]).forEach((s,i)=>{const b=document.createElement("button");b.className="slot";b.textContent=s;b.style.animationDelay=i*40+"ms";
   b.onclick=()=>{slot=s;box.querySelectorAll(".slot").forEach(x=>x.classList.remove("on"));b.classList.add("on")};box.appendChild(b)})
 }catch{box.innerHTML="<small>Could not load slots.</small>"}};
$("#go").onclick=async()=>{const btn=$("#go");btn.disabled=true;btn.textContent="Booking...";
 try{const r=await fetch("/api/book",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(
  {name:$("#name").value,email:$("#email").value,service:$("#svc").value,date:$("#day").value,slot:slot,note:$("#note").value})});
  const j=await r.json();if(r.ok){if(j.url){location.href=j.url;return}show(j.message,"ok");slot=null;$("#day").onchange()}else{show(j.error,"err");if(r.status===409)$("#day").onchange()}
 }catch{show("Network error. Please try again.","err")}
 btn.disabled=false;btn.textContent="Confirm booking"};

if(location.search.includes("paid=1"))show("Payment received. Check your email for confirmation.","ok");
if(location.search.includes("cancelled=1"))show("Payment cancelled. Your slot was released.","err");