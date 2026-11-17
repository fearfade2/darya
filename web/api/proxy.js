module.exports = async function proxy(req, res) {
  const requestURL = new URL(req.url, 'http://local');
  const route = typeof req.query?.route === 'string' ? req.query.route : '';
  const path = requestURL.pathname === '/api/proxy' && route ? '/api/' + route : requestURL.pathname;
  if (!path.startsWith('/api/')) return res.status(404).end();
  if (path.split('/').some(part => part === '.' || part === '..')) return res.status(400).end();
  let primary;
  try {
    const url = new URL(process.env.ALISA_PRIMARY_BACKEND_URL);
    if (url.protocol === 'https:' && !url.username && !url.password && url.pathname === '/' && !url.search && !url.hash) {
      primary = url.origin;
    }
  } catch {}
  let backend = process.env.ALISA_BACKEND_URL;
  if (primary) {
    try {
      const health = await fetch(primary + '/api/health', {signal: AbortSignal.timeout(2500), redirect:'manual'});
      if (health.status === 200) backend = primary;
    } catch {}
  }
  if (!backend || !/^https:\/\/[a-z0-9-]+\.trycloudflare\.com$/.test(backend) && backend !== primary) {
    return res.status(503).json({detail:'Сервер пока не отвечает. Повтори через минуту'});
  }
  const headers = {'Content-Type':'application/json'};
  if (req.headers.cookie) headers.Cookie = req.headers.cookie;
  if (req.headers.origin) headers.Origin = req.headers.origin;
  requestURL.searchParams.delete('route');
  const query = requestURL.search;
  const body = ['GET','HEAD'].includes(req.method) ? undefined : JSON.stringify(req.body ?? {});
  try {
    // Select a healthy route first; never retry a mutation after sending it.
    const response = await fetch(backend + path + query, {method:req.method, headers, body,
      redirect:'manual', signal:AbortSignal.timeout(55000)});
    res.setHeader('Cache-Control','no-store');
    res.setHeader('Content-Type', response.headers.get('content-type') || 'application/json');
    const disposition = response.headers.get('content-disposition');
    if (disposition) res.setHeader('Content-Disposition', disposition);
    const cookies = response.headers.getSetCookie();
    if (cookies.length) res.setHeader('Set-Cookie', cookies);
    res.status(response.status).send(Buffer.from(await response.arrayBuffer()));
  } catch {
    res.status(502).json({detail:'Ответ сервера не получен. Проверь журнал перед повторной отправкой'});
  }
};
