const fs = require('fs');
const d = fs.readFileSync('F:/dpsk harness/dpdata/models.json','utf8');
const m = JSON.parse(d).models;
for (const x of m) {
  console.log(JSON.stringify({slug:x.slug,display:x.display_name,prio:x.priority,suppApi:x.supported_in_api,vis:x.visibility,suppReason:x.supported_reasoning_levels,tiers:x.service_tiers,speedTiers:x.additional_speed_tiers,shell:x.shell_type,upgrade:x.upgrade,desc:(x.description||'').slice(0,120)},null,0));
}
