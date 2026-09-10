const fs = require('fs');
const models = JSON.parse(fs.readFileSync('F:/dpsk harness/dpdata/models.json','utf8')).models;

// Per-model stats from proxy_request_logs (group by model name)
const Database = (() => { try { return require('better-sqlite3'); } catch { return null; } })();

// We'll query via sqlite3 output later
console.log('MODELS:', models.length);
for (const m of models) console.log(m.slug, '|', m.display_name);
