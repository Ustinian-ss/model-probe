const fs = require('fs');
const d = fs.readFileSync("C:/Users/28102/.codex/cc-switch-model-catalog.json", 'utf8');
const j = JSON.parse(d);
console.log("top-level keys:", Object.keys(j));
console.log("type:", typeof j);
if (Array.isArray(j)) console.log("array len:", j.length);
else for (const k of Object.keys(j)) {
  const v = j[k];
  console.log(k, typeof v, Array.isArray(v) ? "len="+v.length : (typeof v === "object" ? Object.keys(v).slice(0,10) : v));
}
