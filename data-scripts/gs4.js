const fs = require('fs');
const s = fs.readFileSync('C:/Users/28102/.codex/.codex-global-state.json','utf8');
const j = JSON.parse(s);
const atom = j['electron-persisted-atom-state'];
const keys = Object.keys(atom || {});
for (const k of keys) {
  const v = atom[k];
  if (typeof v === 'string' && v.length < 400 && /model|provider|catalog|api|reason/i.test(k)) console.log(k, '->', v.slice(0,200));
  if (typeof v === 'object' && v && /model|provider|catalog/i.test(k)) {
    console.log(k, JSON.stringify(v).slice(0,400));
  }
}
