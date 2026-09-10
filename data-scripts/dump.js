const fs = require('fs');
const d = fs.readFileSync("F:/dpsk harness/dpdata/models.json", 'utf8');
const m = JSON.parse(d).models;
const keys = ["slug","display_name","priority","context_window","max_context_window","visibility","supported_in_api","description"];
for (const x of m) {
  const o = {};
  for (const k of keys) o[k] = x[k];
  console.log(JSON.stringify(o));
}
