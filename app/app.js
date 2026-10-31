const SECTIONS = {
  all: "All", world: "World", india: "India", northeast: "Northeast", lucknow: "Lucknow",
  business: "Business", ai: "AI", china: "China", uber: "Uber & rivals", science: "Science", auto: "Auto",
  australia: "Australia", politics: "World Politics", india_politics: "Indian Politics", f1: "F1", cricket: "Cricket",
  india_sports: "Indian Sports", parenting: "Baby & Child", japan: "Japan", money: "Money & Productivity", tech: "Tech",
};
const TINT = {
  world: "#dfe7ea", india: "#efe2d3", northeast: "#dce8dc", lucknow: "#eadfe8", business: "#e3e1ee",
  ai: "#dde4f1", china: "#f0dddb", uber: "#e2e2e2", science: "#dcebe6", auto: "#ebe6d6",
  australia: "#e6e9d8", politics: "#e9dfdf", india_politics: "#efe4d8", f1: "#f0dcdc", cricket: "#dde9df",
  india_sports: "#e4ecd9", parenting: "#f3e3e6", japan: "#f1e0e2", money: "#e0e8e1", tech: "#dfe3ec",
};

let cards = [];
let section = "all";
const feedEl = document.getElementById("feed");
const navEl = document.getElementById("sections");
const countEl = document.getElementById("count");

function ago(iso) {
  if (!iso) return "";
  const h = Math.round((Date.now() - new Date(iso)) / 36e5);
  return h < 1 ? "just now" : h < 24 ? `${h}h ago` : `${Math.round(h / 24)}d ago`;
}

// BBC feed images are 240px wide; their CDN serves larger sizes from the same path
const bigger = (url) => url && url.replace("/standard/240/", "/standard/800/");

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function cardHTML(c, i, total) {
  const lead = c.sources[0];
  const others = c.sources.length - 1;
  const media = c.image
    ? `<div class="media"><img src="${esc(bigger(c.image))}" alt="" loading="lazy" onerror="this.parentNode.classList.add('plain');this.remove()"></div>`
    : `<div class="media plain" style="--tint:${TINT[c.section] || "var(--chip)"}"><span class="label">${esc(SECTIONS[c.section] || c.section)}</span></div>`;
  const chips = c.sources.slice(0, 3).map((s) => `<a class="chip" href="${esc(s.link)}" target="_blank" rel="noopener">${esc(s.name)}</a>`).join("")
    + (c.sources.length > 3 ? `<span class="chip">+${c.sources.length - 3}</span>` : "");
  const original = c.rewritten && c.original_headline !== c.headline
    ? `<button class="toggle" onclick="this.nextElementSibling.classList.toggle('show')">Original headline</button>
       <div class="original">“${esc(c.original_headline)}” — ${esc(lead.name)}</div>` : "";
  return `<article class="card" data-i="${i}"><div class="card-inner">
    ${media}
    <div class="body">
      <div class="meta"><span class="sec">${esc(SECTIONS[c.section] || c.section)}</span><span>${ago(c.published)}</span>${others ? `<span>${c.sources.length} outlets</span>` : ""}</div>
      <h2>${esc(c.headline)}</h2>
      <p class="summary">${esc(c.summary)}</p>
      <p class="why">${esc(c.why)}</p>
      ${original}
    </div>
    <div class="foot">
      <div class="chips">${chips}</div>
      <a class="read" href="${esc(lead.link)}" target="_blank" rel="noopener">Read at ${esc(lead.name)} →</a>
    </div>
  </div></article>`;
}

function doneHTML(total) {
  return `<article class="card done"><div class="card-inner">
    <h2>You're caught up</h2>
    <p>That's all ${total} stories ${section === "all" ? "for now" : `in ${SECTIONS[section]}`}.<br>The feed refreshes every couple of hours.</p>
  </div></article>`;
}

function render() {
  const list = section === "all" ? cards : cards.filter((c) => c.section === section);
  feedEl.innerHTML = list.map((c, i) => cardHTML(c, i, list.length)).join("") + doneHTML(list.length);
  feedEl.scrollTop = 0;
  updateCount(list.length);
  observe(list.length);
}

let observer;
function observe(total) {
  observer?.disconnect();
  observer = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (e.isIntersecting) {
        const i = e.target.dataset.i;
        countEl.textContent = i === undefined ? "Caught up" : `${+i + 1} / ${total}`;
      }
    }
  }, { root: feedEl, threshold: 0.6 });
  feedEl.querySelectorAll(".card").forEach((el) => observer.observe(el));
}
const updateCount = (total) => (countEl.textContent = total ? `1 / ${total}` : "");

function renderNav() {
  const present = new Set(cards.map((c) => c.section));
  navEl.innerHTML = Object.entries(SECTIONS)
    .filter(([k]) => k === "all" || present.has(k))
    .map(([k, v]) => `<button data-s="${k}" aria-pressed="${k === section}">${v}</button>`).join("");
}
navEl.addEventListener("click", (e) => {
  const s = e.target.dataset?.s;
  if (!s) return;
  section = s;
  renderNav();
  render();
});

// Keyboard: arrows / j k move between cards on desktop
document.addEventListener("keydown", (e) => {
  const dir = { ArrowDown: 1, j: 1, ArrowUp: -1, k: -1 }[e.key];
  if (dir) { e.preventDefault(); feedEl.scrollBy({ top: dir * feedEl.clientHeight, behavior: "smooth" }); }
});

async function load() {
  try {
    const res = await fetch("feed.json", { cache: "no-cache" });
    cards = (await res.json()).cards;
  } catch {
    feedEl.innerHTML = `<article class="card done"><div class="card-inner"><h2>Couldn't load the feed</h2><p>Check your connection and reopen the app.</p></div></article>`;
    return;
  }
  renderNav();
  render();
}

if ("serviceWorker" in navigator) navigator.serviceWorker.register("sw.js");
load();
