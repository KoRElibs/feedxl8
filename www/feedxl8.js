// Meilisearch is accessed via the server-side proxy — no secrets in the browser.
const MEILI_URL = "/meili";
const INDEX     = "news"; // must match meili_index in feedxl8.conf

const qInput = document.getElementById("q");
const limitSelect = document.getElementById("limit");
const countrySel = document.getElementById("country");
const publisherSel = document.getElementById("publisher");
const dateFrom = document.getElementById("date_from");
const dateTo = document.getElementById("date_to");
const clearBtn = document.getElementById("clear");
const resultsDiv = document.getElementById("results");

const HEADERS = { "Content-Type": "application/json" };

function escapeHtml(s){ return String(s||"").replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[m]); }
function escapeAttr(s){ return escapeHtml(s).replace(/"/g,'&quot;'); }

function toFlag(code){
  if(!code || code.length !== 2) return code;
  const base = 0x1F1E6 - 65;
  return String.fromCodePoint(
    code.toUpperCase().charCodeAt(0) + base,
    code.toUpperCase().charCodeAt(1) + base
  );
}

function fmtDate(iso){
  if(!iso) return "";
  try{ return new Date(iso).toLocaleString("en-GB",{day:"numeric",month:"short",year:"numeric",hour:"2-digit",minute:"2-digit"}); }
  catch{ return iso; }
}

function renderHit(hit){
  const container = document.createElement("div");
  container.className = "result";
  const left = document.createElement("div"); left.className = "left";
  const right = document.createElement("div"); right.className = "right";

  if(hit.image_url){
    const img = document.createElement("img");
    img.src = "/imgproxy?url=" + encodeURIComponent(hit.image_url); img.className = "thumb"; img.alt = "";
    left.appendChild(img);
  }

  const title = document.createElement("div"); title.className = "title";
  const link = hit.link || hit.url || "#";
  title.innerHTML = `<a href="${escapeAttr(link)}" target="_blank" rel="noopener noreferrer">${escapeHtml(hit.title||"(no title)")}</a>`;

  const meta = document.createElement("div"); meta.className = "meta";
  if(hit.publisher) meta.appendChild(Object.assign(document.createElement("span"),{textContent: hit.publisher}));
  if(hit.published) meta.appendChild(Object.assign(document.createElement("span"),{textContent: fmtDate(hit.published)}));
  const ai = document.createElement("span"); ai.className = "badge badge-ai"; ai.textContent = "AI translated";
  meta.appendChild(ai);
  if(hit.country){
    const b = document.createElement("span"); b.className = "badge";
    b.textContent = toFlag(hit.country);
    b.title = hit.country;
    meta.appendChild(b);
  }

  const summary = document.createElement("div"); summary.className = "summary";
  summary.textContent = hit.summary || "";

  right.append(title, meta, summary);
  container.append(left, right);
  return container;
}

function buildFilter(){
  const parts = [];
  const c = countrySel.value.trim();
  if(c) parts.push(`country = "${c.replace(/"/g,'\\"')}"`);
  const p = publisherSel.value.trim();
  if(p) parts.push(`publisher = "${p.replace(/"/g,'\\"')}"`);
  if(dateFrom.value) parts.push(`published >= ${JSON.stringify(dateFrom.value)}`);
  if(dateTo.value) parts.push(`published <= ${JSON.stringify(dateTo.value)}`);
  return parts.length ? parts.join(" AND ") : null;
}

async function search(q, limit=20){
  const res = await fetch(`${MEILI_URL}/indexes/${encodeURIComponent(INDEX)}/search`, {
    method: "POST", headers: HEADERS,
    body: JSON.stringify({ q, limit, filter: buildFilter(), sort: ["published:desc"],
      attributesToRetrieve: ["title","summary","link","url","image_url","published","publisher","feedid","country"] })
  });
  if(!res.ok) throw new Error(res.status + " " + await res.text());
  return res.json();
}

async function loadFacets(){
  try{
    const res = await fetch(`${MEILI_URL}/indexes/${encodeURIComponent(INDEX)}/search`, {
      method: "POST", headers: HEADERS,
      body: JSON.stringify({ q: "", limit: 0, facets: ["country", "publisher"] })
    });
    if(!res.ok) return;
    const { facetDistribution = {} } = await res.json();
    const addOptions = (sel, dist, label) => {
      Object.keys(dist).filter(Boolean).sort((a,b)=>a.localeCompare(b)).forEach(v => {
        const text = label === "country" ? toFlag(v) + " " + v : v;
        sel.appendChild(Object.assign(document.createElement("option"),{value:v,textContent:text}));
      });
    };
    if(facetDistribution.country)   addOptions(countrySel,   facetDistribution.country,   "country");
    if(facetDistribution.publisher) addOptions(publisherSel, facetDistribution.publisher, "publisher");
  }catch(e){ console.warn("Failed to load facets:", e); }
}

let timer = null;
function scheduleSearch(){ clearTimeout(timer); timer = setTimeout(doSearch, 250); }

qInput.addEventListener("input", scheduleSearch);
limitSelect.addEventListener("change", scheduleSearch);
countrySel.addEventListener("change", e => { if(publisherSel.value) publisherSel.value = ""; scheduleSearch(e); });
publisherSel.addEventListener("change", e => { if(countrySel.value) countrySel.value = ""; scheduleSearch(e); });
dateFrom.addEventListener("change", scheduleSearch);
dateTo.addEventListener("change", scheduleSearch);
clearBtn.addEventListener("click", () => {
  [countrySel, publisherSel].forEach(s => s.value = "");
  [dateFrom, dateTo, qInput].forEach(i => i.value = "");
  scheduleSearch();
});

async function doSearch(){
  const q = qInput.value.trim();
  const limit = parseInt(limitSelect.value, 10) || 20;
  const msg = document.createElement("div"); msg.className = "status"; msg.textContent = "Searching\u2026";
  resultsDiv.replaceChildren(msg);
  try{
    const { hits = [] } = await search(q, limit);
    if(!hits.length){
      const p = document.createElement("p"); p.className = "status"; p.textContent = "No results";
      resultsDiv.replaceChildren(p);
      return;
    }
    resultsDiv.replaceChildren(...hits.map(renderHit));
  }catch(err){
    const pre = document.createElement("pre"); pre.className = "status-error"; pre.textContent = err.message;
    resultsDiv.replaceChildren(pre);
    console.error(err);
  }
}

loadFacets().then(() => doSearch());
