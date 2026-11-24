// SPDX-License-Identifier: Apache-2.0
// Plain Node 24 implementation of docs/plugin-http-contract.md; no dependencies.
import {readFileSync} from 'node:fs';
const manifest = {name:'template',version:'0.1.0',channels:[{name:'template.chat',description:'Invented JSONL chat events',fields:[{name:"rating",type:"integer",description:"Invented event score"}],is_reference:false,is_outside:false}],settings:{input:{env:'TEMPLATE_INPUT',required:true}},secrets:{secret:{env:'TEMPLATE_SECRET',required:false}},run_mode:'poll',default_interval_s:3600,link_types:['part_of']};
const command=process.argv[2], dry=process.argv.includes('--dry-run');
async function request(method,path,data) {
  for(let attempt=0;;attempt++) {
    try {
      const response=await fetch(process.env.THROUGHLINE_URL+'/v1/'+path,{method,headers:{Authorization:'Bearer '+process.env.THROUGHLINE_TOKEN,'Content-Type':'application/json'},body:data===undefined?undefined:JSON.stringify(data)});
      if(response.status===404 && method==='GET') return {cursor:{}};
      if(!response.ok) { const error=new Error('HTTP '+response.status); error.retry=response.status===429||response.status>=500; throw error; }
      return await response.json();
    } catch(error) { if(attempt===4 || error.retry===false) throw error; await new Promise(resolve=>setTimeout(resolve,100*2**attempt)); }
  }
}
async function execute() {
  await request('POST','plugins/template/manifest',manifest);
  for(const channel of manifest.channels) await request('POST','channels',{...channel,plugin:'template'});
  const cursor=dry?{}:(await request('GET','plugins/template/cursor/default')).cursor;
  const events=readFileSync(process.env.TEMPLATE_INPUT,'utf8').trim().split(/\r?\n/).filter(Boolean).map(JSON.parse);
  const counts={}, dates=[];
  for(let i=cursor.offset||0;i<events.length;i++) {
    const event=events[i];
    const message={channel:'template.chat',external_id:event.id,thread:{external_id:'fake-thread'},happened_at:event.at,content:event.text,fields:{rating:event.score||0},payload:event};
    if(process.env.TEMPLATE_SECRET && JSON.stringify(event).includes(process.env.TEMPLATE_SECRET)) throw new Error('Declared secret found in generated record');
    const result=await request('POST','ingest/messages',{plugin:'template',messages:[message],dry_run:dry});
    if(result.rejected.length) throw new Error('Rejected batch');
    counts[message.channel]=(counts[message.channel]||0)+1; dates.push(event.at);
    if(!dry) {
      await request('POST','links',{type:'part_of',from:{channel:'template.chat',external_id:event.id},to:{kind:'work',external_ref:'fake-story',name:'Invented Orchard Story'},method:'source'});
      await request('POST','ingest/track',{points:[{at:event.at,lat:12.0,lon:34.0,source:'template'}]});
      await request('PUT','plugins/template/cursor/default',{offset:i+1});
    }
  }
  if(!dry) await request('POST','plugins/template/heartbeat',{interval_s:3600,messages_ingested:dates.length});
  dates.sort((a,b)=>new Date(a)-new Date(b)); console.log(JSON.stringify({counts,start:dates[0]||null,end:dates.at(-1)||null,estimated_model_cost:0}));
}
try {
  if(command==='manifest') console.log(JSON.stringify(manifest));
  else if(['backfill','sync'].includes(command)) {
    do { await execute(); if(command==='backfill'||dry||process.argv.includes('--once')) break; await new Promise(resolve=>setTimeout(resolve,3600000)); } while(true);
  } else throw new Error('Expected manifest, backfill, or sync');
} catch(error) { let message=String(error); for(const value of [process.env.TEMPLATE_SECRET,process.env.THROUGHLINE_TOKEN]) if(value) message=message.replaceAll(value,'[REDACTED]'); console.error(message); if(!dry && process.env.THROUGHLINE_URL) await request('POST','plugins/template/heartbeat',{error:message}).catch(()=>{}); process.exitCode=1; }
