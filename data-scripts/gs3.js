const fs = require('fs');
const s = fs.readFileSync('C:/Users/28102/.codex/.codex-global-state.json','utf8');
const j = JSON.parse(s);
const atom = j['electron-persisted-atom-state'];
const keys = Object.keys(atom || {});
console.log('atom keys:', keys.length);
for (const k of keys.slice(0,80)) console.log(k.slice(0,120));
