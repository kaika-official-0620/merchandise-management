// Execute the actual template scripts against a small browser-state fixture.
// No browser automation, external calls, real accounts or credentials.
const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const fixtures = [
  {name:'email', file:'email_confirm.html', key:'kaika-email-proof-test', button:'confirm-email',
   fields:['email-proof'], fragment:'#token='+'a'.repeat(48), old:{token:'b'.repeat(48)}, invalid:'#token=bad'},
  {name:'line', file:'line_link.html', key:'kaika-line-link-test', button:'line-confirm',
   fields:['line-invitation','line-token'], fragment:'#invitation='+'a'.repeat(48)+'&linkToken=fixture-link-token-12345',
   old:{invitation:'b'.repeat(48),linkToken:'previous-link-token-12345'}, invalid:'#invitation=bad&linkToken=fixture-link-token-12345'},
];
function execute(f, {fragment='', saved=null, authenticated=true, storageThrows=false}={}) {
  const template=fs.readFileSync(path.join(__dirname,'../templates/workspace_home',f.file),'utf8');
  const script=template.match(/<script>([\s\S]*?)<\/script>/)[1];
  const now=1792954800000, data=new Map(saved===null?[]:[[f.key,typeof saved==='string'?saved:JSON.stringify(saved)]]);
  const handlers={}, elements={};
  if (authenticated) {
    elements[f.button]={disabled:true,form:{addEventListener:(name,fn)=>{handlers[name]=fn;}}};
    for (const id of f.fields) elements[id]={value:''};
  }
  const state={url:null}, location={hash:fragment,pathname:'/confirmation'};
  vm.runInNewContext(script,{
    URLSearchParams, Date:{now:()=>now}, location,
    window:{addEventListener:(name,fn)=>{handlers[name]=fn;}},
    history:{replaceState:(_state,_title,url)=>{state.url=url;location.hash='';}},
    document:{getElementById:id=>elements[id]||null},
    sessionStorage:{setItem:(key,value)=>{if(storageThrows)throw Error('disabled');data.set(key,value);},
      getItem:key=>{if(storageThrows)throw Error('disabled');return data.get(key)||null;},
      removeItem:key=>{if(storageThrows)throw Error('disabled');data.delete(key);}}
  });
  return {data,elements,handlers,state,now,location};
}
for (const f of fixtures) {
  test(f.name+': valid link survives anonymous landing and login in the same tab',()=>{
    const landing=execute(f,{fragment:f.fragment,authenticated:false});
    assert.ok(landing.data.has(f.key));assert.equal(landing.state.url,'/confirmation');
    const signed=execute(f,{saved:landing.data.get(f.key)});
    assert.equal(signed.elements[f.button].disabled,false);
    assert.equal(signed.elements[f.fields[0]].value,'a'.repeat(48));
    signed.handlers.submit();assert.equal(signed.data.has(f.key),false);
  });
  test(f.name+': explicit invalid link never reuses an earlier valid proof',()=>{
    const result=execute(f,{fragment:f.invalid,saved:{...f.old,expires:1792954800000+600000}});
    assert.equal(result.elements[f.button].disabled,true);
    assert.equal(result.elements[f.fields[0]].value,'');
    assert.equal(result.data.has(f.key),false);
  });
  test(f.name+': unknown fragment does not revive an earlier proof',()=>{
    const result=execute(f,{fragment:'#unexpected=1',saved:{...f.old,expires:1792954800000+600000}});
    assert.equal(result.elements[f.button].disabled,true);
  });
  test(f.name+': expired cached proof is disabled and removed',()=>{
    const result=execute(f,{saved:{...f.old,expires:1792954800000-1}});
    assert.equal(result.elements[f.button].disabled,true);assert.equal(result.data.has(f.key),false);
  });
  test(f.name+': malformed cached JSON is discarded',()=>{
    const result=execute(f,{saved:'{"broken"'});
    assert.equal(result.elements[f.button].disabled,true);assert.equal(result.data.has(f.key),false);
  });
  test(f.name+': fresh valid proof replaces previous cached proof',()=>{
    const result=execute(f,{fragment:f.fragment,saved:{...f.old,expires:1792954800000+600000}});
    assert.equal(result.elements[f.fields[0]].value,'a'.repeat(48));
  });
  test(f.name+': fresh link can be used when session storage is unavailable',()=>{
    const result=execute(f,{fragment:f.fragment,storageThrows:true});
    assert.equal(result.elements[f.button].disabled,false);
  });
  test(f.name+': same-page fragment change discards earlier proof and clears fields',()=>{
    const result=execute(f,{fragment:f.fragment});
    assert.equal(result.elements[f.button].disabled,false);
    result.location.hash=f.invalid;result.handlers.hashchange();
    assert.equal(result.elements[f.button].disabled,true);
    for(const id of f.fields)assert.equal(result.elements[id].value,'');
    assert.equal(result.data.has(f.key),false);
    result.location.hash=f.fragment;result.handlers.hashchange();
    assert.equal(result.elements[f.button].disabled,false);
    assert.equal(result.elements[f.fields[0]].value,'a'.repeat(48));
  });
}
