const fs = require('fs');
const s = fs.readFileSync('C:/Users/28102/.codex/.codex-global-state.json','utf8');
const j = JSON.parse(s);
const keys = Object.keys(j);
console.log('TOP KEYS:', keys.length);
for (const k of keys.slice(0,30)) console.log(k.slice(0,80));
