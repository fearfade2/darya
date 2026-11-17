const test=require('node:test');
const assert=require('node:assert/strict');
const proxy=require('../web/api/proxy.js');
process.env.ALISA_PRIMARY_BACKEND_URL='https://primary.example.com';
function response(){return {headers:{},setHeader(k,v){this.headers[k]=v;},status(n){this.code=n;return this;},send(v){this.body=v;return this;},json(v){this.body=v;return this;},end(){return this;}};}

test('a lost payment response is never retried on another route',async()=>{
  const original=global.fetch;
  process.env.ALISA_BACKEND_URL='https://example-alisa.trycloudflare.com';
  const calls=[];
  global.fetch=async(url,options)=>{calls.push({url,options});if(calls.length===1)return new Response('offline',{status:503});throw new Error('lost acknowledgment');};
  try{
    const res=response();
    await proxy({method:'POST',url:'/api/gifts/send',headers:{cookie:'alisa_session=private',origin:'https://alisa-example.vercel.app'},body:{contact_id:10,gift_id:'1'}},res);
    assert.equal(res.code,502);
    assert.equal(calls.length,2);
    assert.equal(calls[1].url,'https://example-alisa.trycloudflare.com/api/gifts/send');
    assert.equal(calls[1].options.headers.Cookie,'alisa_session=private');
    assert.equal(calls[1].options.headers.Origin,'https://alisa-example.vercel.app');
  }finally{global.fetch=original;}
});

test('authentication cookies and query strings survive the proxy',async()=>{
  const original=global.fetch;
  const calls=[];
  global.fetch=async(url,options)=>{calls.push({url,options});return calls.length===1?new Response('{}'):new Response('{"ok":true}',{headers:{'Content-Type':'application/json','Set-Cookie':'alisa_session=new; HttpOnly; Secure; SameSite=Strict'}});};
  try{
    const res=response();
    await proxy({method:'GET',url:'/api/auth?check=1',headers:{}},res);
    assert.equal(res.code,200);
    assert.ok(calls[1].url.endsWith('/api/auth?check=1'));
    assert.equal(calls[1].options.body,undefined);
    assert.deepEqual(res.headers['Set-Cookie'],['alisa_session=new; HttpOnly; Secure; SameSite=Strict']);
    assert.equal(res.headers['Cache-Control'],'no-store');
  }finally{global.fetch=original;}
});

test('nested API routes reach the backend after a Vercel rewrite',async()=>{
  const original=global.fetch;const calls=[];
  global.fetch=async(url)=>{calls.push(url);return new Response('{}');};
  try{
    const res=response();
    await proxy({method:'POST',url:'/api/proxy?route=ai%2Fmodels',query:{route:'ai/models'},headers:{},body:{}},res);
    assert.equal(res.code,200);assert.ok(calls[1].endsWith('/api/ai/models'));
  }finally{global.fetch=original;}
});
