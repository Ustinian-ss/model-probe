const {execSync} = require('child_process');
const fs = require('fs');

// Get all stats
const out = execSync('sqlite3 "C:/Users/28102/.cc-switch/cc-switch.db" "SELECT model, COUNT(*), SUM(CASE WHEN status_code BETWEEN 200 AND 299 THEN 1 ELSE 0 END), ROUND(AVG(latency_ms),0), MAX(created_at) FROM proxy_request_logs GROUP BY model"', {encoding:'utf8'});
const lines = out.trim().split('\n');
const rows = lines.map(l => { const [m,n,ok,lat,last] = l.split('|'); return {model:m, n:+n, ok:+ok, lat:+lat, last:+last}; });

// Get catalog
const cat = JSON.parse(fs.readFileSync('F:/dpsk harness/dpdata/models.json','utf8')).models;
const slugs = cat.map(x => x.slug);

// Build alias map: for each slug, possible request model variants
// Based on observation: aqua prefixes some with 'build/' or 'gitee/'; otherwise bare or with vendor prefix
const aliasMap = {
  'moonshotai/kimi-k3': ['moonshotai/kimi-k3'],
  'openai/gpt-oss-120b': ['openai/gpt-oss-120b','gpt-oss-120b'],
  'minimaxai/minimax-m3': ['minimaxai/minimax-m3'],
  'openai/gpt-oss-20b': ['openai/gpt-oss-20b'],
  'deepseek-ai/deepseek-v4-flash-0731': ['deepseek-ai/deepseek-v4-flash-0731','deepseek-ai/deepseek-v4-flash','deepseek-v4-flash','acu/deepseek-v4-flash','build/deepseek-v4-flash-0731'],
  'meta/llama-3.3-70b-instruct': ['meta/llama-3.3-70b-instruct'],
  'nvidia/nemotron-3-nano-30b-a3b': ['nvidia/nemotron-3-nano-30b-a3b'],
  'nvidia/nemotron-nano-12b-v2-vl': ['nvidia/nemotron-nano-12b-v2-vl'],
  'nvidia/nemotron-3-nano-omni-30b-a3b-reasoning': ['nvidia/nemotron-3-nano-omni-30b-a3b-reasoning','nvidia/nemotron-3-ultra-550b-a55b','nemotron-3-ultra-550b-a55b','build/nemotron-3-ultra-550b-a55b'],
  'google/diffusiongemma-26b-a4b-it': ['google/diffusiongemma-26b-a4b-it','diffusiongemma-26b-a4b-it','build/diffusiongemma-26b-a4b-it'],
  'DeepSeek-R1-Distill-Qwen-14B': ['DeepSeek-R1-Distill-Qwen-14B'],
  'poolside/laguna-xs-2.1': ['poolside/laguna-xs-2.1'],
  'deepseek-ai/deepseek-v4-pro-0813': ['deepseek-ai/deepseek-v4-pro-0813','deepseek-ai/deepseek-v4-pro','deepseek-v4-pro'],
  'GLM-ASR': ['GLM-ASR'],
};

const byKey = {};
for (const r of rows) byKey[r.model] = r;

const agg = cat.map(m => {
  const aliases = aliasMap[m.slug] || [m.slug];
  let n=0, ok=0, latSum=0, last=0;
  for (const a of aliases) {
    const x = byKey[a]; if (!x) continue;
    n += x.n; ok += x.ok; latSum += x.n*x.lat; last = Math.max(last, x.last);
  }
  const success = n ? (ok/n*100) : null;
  const avgLat = n ? Math.round(latSum/n) : null;
  return {slug:m.slug, display:m.display_name, n, ok, success, avgLat, last, vis:m.visibility, suppApi:m.supported_in_api};
});

agg.sort((a,b)=> (b.n||0)-(a.n||0));
console.log('slug | display | n | ok | success% | avgLat(ms) | lastSeen');
for (const a of agg) {
  const dt = a.last ? new Date(a.last*1000).toISOString().slice(0,16).replace('T',' ') : '—';
  console.log(`${a.slug} | ${a.display} | ${a.n||0} | ${a.ok||0} | ${a.success!=null?a.success.toFixed(1):'—'} | ${a.avgLat!=null?a.avgLat:'—'} | ${dt}`);
}
