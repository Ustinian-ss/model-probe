const fs = require('fs');
const s = fs.readFileSync('C:/Users/28102/.codex/.codex-global-state.json','utf8');
const j = JSON.parse(s);
function walk(o, depth, path) {
  if (depth>5) return;
  if (o==null) return;
  if (typeof o === 'string') { if (o.length>3 && o.length<200 && /model|provider|api/i.test(path)) console.log(path+':', o.slice(0,150)); return; }
  if (Array.isArray(o)) { if (o.length>0 && typeof o[0]==='object') { const k=path+'[0]'; walk(o[0], depth+1, k); } return; }
  for (const k of Object.keys(o)) walk(o[k], depth+1, path+'.'+k);
}
walk(j, 0, 'root');
