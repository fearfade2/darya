"""Keep the panel reachable when instabox's public HTTP gateway is unavailable."""
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


async def publish(url):
    token = os.environ['ALISA_VERCEL_TOKEN']
    project_id = os.environ['ALISA_VERCEL_PROJECT_ID']
    team_id = os.getenv('ALISA_VERCEL_TEAM_ID')
    params = {'teamId':team_id} if team_id else {}
    aliases = [alias.strip() for alias in os.getenv('ALISA_VERCEL_ALIASES','').split(',') if alias.strip()]
    async with httpx.AsyncClient(base_url='https://api.vercel.com',
          headers={'Authorization':'Bearer '+token}, params=params, timeout=45) as client:
        async def call(method, path, **kwargs):
            result = await client.request(method,path,**kwargs)
            if result.status_code >= 400:
                raise RuntimeError(f'Vercel {method}: HTTP {result.status_code}')
            return result.json()
        envs = (await call('GET',f'/v9/projects/{project_id}/env'))['envs']
        current = next((env for env in envs if env['key']=='ALISA_BACKEND_URL'),None)
        value = {'key':'ALISA_BACKEND_URL','value':url,'type':'plain','target':['production']}
        if current:
            await call('PATCH',f'/v9/projects/{project_id}/env/{current["id"]}',json=value)
        else:
            await call('POST',f'/v10/projects/{project_id}/env',json=value)
        deployments = (await call('GET','/v6/deployments',params=params | {'projectId':project_id,'target':'production','limit':10}))['deployments']
        latest = next(item for item in deployments if item.get('state',item.get('readyState'))=='READY')
        project = await call('GET',f'/v9/projects/{project_id}')
        deployment = await call('POST','/v13/deployments',json={
            'name':project['name'],'project':project_id,'deploymentId':latest['uid'],'target':'production'})
        for _ in range(45):
            state = await call('GET','/v13/deployments/'+deployment['id'])
            if state.get('readyState') == 'READY':
                for alias in aliases:
                    await call('POST',f'/v2/deployments/{deployment["id"]}/aliases',json={'alias':alias})
                (ROOT/'data/tunnel-state.json').write_text(json.dumps({'url':url,'deployment_id':deployment['id'],'updated':time.time()}))
                print('Panel HTTPS route updated',flush=True)
                return
            if state.get('readyState') in {'ERROR','CANCELED'}:
                raise RuntimeError('Vercel deployment did not complete')
            await asyncio.sleep(2)
        raise RuntimeError('Vercel deployment timed out')


async def main():
    for name in ('ALISA_VERCEL_TOKEN','ALISA_VERCEL_PROJECT_ID'):
        if not os.getenv(name):
            raise SystemExit(f'{name} is missing')
    config = ROOT/'data/cloudflared-empty.yml'
    config.write_text('{}\n')
    process = await asyncio.create_subprocess_exec(str(ROOT/'bin/cloudflared'),
       'tunnel','--config',str(config),'--no-autoupdate','--protocol','http2','--url','http://127.0.0.1:8790',
       stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
    url = None
    try:
        while line := await process.stdout.readline():
            text = line.decode(errors='replace')
            match = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com',text)
            if match and match.group() != url:
                url = match.group()
                print('HTTPS fallback connected',flush=True)
                (ROOT/'data/tunnel-url.txt').write_text(url)
                for attempt in range(4):
                    try:
                        await publish(url)
                        break
                    except (httpx.HTTPError,RuntimeError,KeyError,StopIteration) as error:
                        print((str(error) if isinstance(error,RuntimeError) else type(error).__name__) + ': route update pending',flush=True)
                        await asyncio.sleep(10*(attempt+1))
                else:
                    raise RuntimeError('Panel routing update failed; restarting the tunnel')
            elif 'ERR' in text:
                print(text.strip()[:400],flush=True)
    finally:
        if process.returncode is None:
            process.terminate()
        await process.wait()


asyncio.run(main())
