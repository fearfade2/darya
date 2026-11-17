const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const fs=require('node:fs');const assert=require('node:assert/strict');
(async()=>{
 const base=process.argv[2]||'http://127.0.0.1:8793';const directory=process.argv[3];
 const password=fs.readFileSync(directory+'/ui-data/first-login.txt','utf8').split('\n')[1];
 const browser=await chromium.launch({executablePath:'/usr/bin/chromium',headless:process.env.INSTABOX_HEADLESS==='1',args:['--no-sandbox','--disable-quic']});
 const context=await browser.newContext({viewport:{width:1440,height:1080}});const page=await context.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));
 let feed=null;let post={id:1,status:'draft',created:Date.now()/1000,text:'Новое аниме выйдет осенью.\n\nИсточники:\nhttps://t.me/anime_news/101',sources:[{title:'Анонс сезона',url:'https://t.me/anime_news/101'}]};const captured=[];
 try{
  await page.goto(base+'/api/telegram/panel');await page.locator('#login-screen').waitFor({state:'visible'});
  await page.locator('#login-password').fill(password);await page.locator('#login-form button').click();await page.locator('#app-shell').waitFor({state:'visible'});
  await page.route('**/api/publications',route=>route.fulfill({json:{enabled:true,feeds:feed?[feed]:[],posts:feed?[post]:[]}}));
  await page.route('**/api/publications/feeds',async route=>{
   const config=route.request().postDataJSON();captured.push(config);feed={id:1,name:config.name,config,enabled:true,next_check:Date.now()/1000+1800};
   await route.fulfill({json:{saved:true,feed_id:1}});
  });
  await page.route('**/api/publications/posts/1',async route=>{post.text=route.request().postDataJSON().text;await route.fulfill({json:{saved:true}});});
  await page.route('**/api/publications/publish',async route=>{assert.deepEqual(route.request().postDataJSON(),{post_id:1});post.status='sent';await route.fulfill({json:{accepted:true,post_id:1}});});
  await page.route('**/api/publications/feeds/1',async route=>{feed.enabled=false;await route.fulfill({json:{removed:true}});});
  await page.locator('[data-page="publications"]').click();await page.locator('#pub-status').filter({hasText:'Мониторинг включён'}).waitFor();
  await page.locator('#pub-name').fill('Новости аниме');await page.locator('#pub-topic').fill('Анонсы новых сезонов');await page.locator('#pub-destination').fill('@my_channel');
  await page.locator('#pub-channels').fill('@anime_news');await page.locator('#pub-web').fill('https://news.example/anime');await page.locator('#pub-rss').fill('https://news.example/feed.xml');await page.locator('#pub-searches').fill('новости аниме');
  await page.locator('#pub-mode').selectOption('draft');await page.locator('#pub-feed-form button[type="submit"]').click();
  await page.locator('#pub-feeds').filter({hasText:'Новости аниме'}).waitFor();
  assert.equal(captured[0].destination,'@my_channel');assert.equal(captured[0].sources.length,4);assert.equal(captured[0].mode,'draft');
  await page.locator('#pub-posts button').filter({hasText:'Править'}).click();await page.locator('#pub-posts textarea').fill('Обновлённый анонс сезона.\nhttps://t.me/anime_news/101');
  await page.locator('#pub-posts button').filter({hasText:'Сохранить правки'}).click();await page.locator('#pub-posts').filter({hasText:'Обновлённый анонс'}).waitFor();
  await page.locator('#pub-posts button').filter({hasText:'Опубликовать'}).click();await page.locator('#pub-posts h3').filter({hasText:'Опубликовано'}).waitFor();
  await page.screenshot({path:directory+'/publications-desktop.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});
  for(const selector of ['#pub-settings','#pub-feed-form','#pub-feeds','#pub-posts']){await page.locator(selector).scrollIntoViewIfNeeded();assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);}
  await page.screenshot({path:directory+'/publications-mobile.png',fullPage:true});
  await page.locator('#pub-feeds button').filter({hasText:'Остановить'}).click();await page.locator('#pub-feeds').filter({hasText:'Задание остановлено'}).waitFor();
  assert.deepEqual(errors,[]);console.log(JSON.stringify({passed:true,source_types:4,create:true,edit:true,publish:true,stop:true,desktop:true,mobile:true}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
